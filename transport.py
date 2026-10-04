"""Loopback-only native Codex Responses transport.

The upstream is deliberately fixed.  This process never reads Codex credentials:
the native client's Authorization and account headers are forwarded to ChatGPT.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import zlib

try:
    from compression import zstd
except ImportError:  # Python before 3.14: compressed aliases fail closed.
    zstd = None

from core import Router, cli_route_id, normalize_alias, proxy_compatible, visible_roles


UPSTREAM_HOST = "chatgpt.com"
UPSTREAM_BASE = "/backend-api/codex"
MAX_BODY = 32 * 1024 * 1024
MAX_SSE_LINE = 1024 * 1024
READ_TIMEOUT = 30
UPSTREAM_TIMEOUT = 120
HOP_HEADERS = frozenset(
    ("connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
     "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length")
)
BLOCKED_REQUEST_HEADERS = frozenset(("cookie", "proxy-authorization", "x-forwarded-for",
                                     "x-forwarded-host", "x-forwarded-proto", "forwarded"))


def _client(headers) -> str:
    value = headers.get("X-Jev-Client") or headers.get("Originator") or headers.get("User-Agent", "")
    value = value.lower()[:128]
    if "desktop" in value or "chatgpt" in value:
        return "desktop"
    if "codex" in value or "cli" in value:
        return "cli"
    return "unknown"


def _sol_slug(catalog_path: Path, config_path: Path | None = None) -> str:
    try:
        return visible_roles(json.loads(catalog_path.read_text()))["sol"]["slug"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    if config_path is not None:
        try:
            slug = json.loads(config_path.read_text()).get("fallback_model")
            if isinstance(slug, str) and re.fullmatch(r"gpt-\d+(?:\.\d+)*-sol", slug):
                return slug
        except (OSError, ValueError, AttributeError):
            pass
    return ""


def _rewrite(body: bytes, router: Router, client: str, session_id: str | None) -> tuple[bytes, dict | None]:
    """Return exact original bytes for concrete models; rewrite only alias model/effort."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return body, None
    if not isinstance(payload, dict):
        return body, None
    selected_model = payload.get("model")
    alias = normalize_alias(selected_model)
    mode_override = "auto" if alias == "effortlane-auto" else "shadow" if alias == "effortlane-shadow" else None
    try:
        decision = router.decide(payload, client=client, session_id=session_id,
                                 native_selection=False, mode_override=mode_override)
    except Exception:
        if not alias:
            return body, None
        decision = {"model": _sol_slug(router.catalog_path, getattr(router, "config_path", None)), "effort": "medium", "mode": "shadow",
                    "reason": "router_error", "proposed_model": None, "proposed_effort": None}
    if not alias:
        decision["_alias"] = False
        decision["client"] = client
        return body, decision
    # This transport runs after Codex selected Sol's model_messages and tool policy.
    # Route elsewhere only when the catalog proves the native harness identical.
    sol = _sol_slug(router.catalog_path, getattr(router, "config_path", None))
    if not sol:
        return b"", {"model": None, "effort": None, "mode": "off", "reason": "cannot_route"}
    decision["_alias"] = True
    decision["client"] = client
    compatible = decision.get("model") == sol
    if not compatible:
        try:
            roles = visible_roles(json.loads(router.catalog_path.read_text()))
            base = roles["sol"]
            target = next(role for role in roles.values() if role["slug"] == decision.get("model"))
            compatible = proxy_compatible(target, base) and decision.get("effort") in target["efforts"]
        except (OSError, ValueError, KeyError, StopIteration):
            compatible = False
    if not compatible:
        decision["proposed_model"] = decision.get("model")
        decision["proposed_effort"] = decision.get("effort")
        decision["model"] = sol
        decision["effort"] = "medium"
        decision["reason"] = "requires_native_model_selection"
    payload["model"] = decision["model"]
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
        payload["reasoning"] = reasoning
    reasoning["effort"] = decision.get("effort") or "medium"
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), decision


def _decode_bounded(body: bytes, encoding: str) -> bytes | None:
    """Decode native request compression without allowing expansion past MAX_BODY."""
    if encoding in ("", "identity"):
        return body
    try:
        if encoding in ("gzip", "deflate"):
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
            result = dec.decompress(body, MAX_BODY + 1)
            if len(result) > MAX_BODY or not dec.eof or dec.unconsumed_tail:
                return None
            return result
        if encoding == "zstd" and zstd is not None:
            dec = zstd.ZstdDecompressor()
            result = dec.decompress(body, MAX_BODY + 1)
            if len(result) > MAX_BODY or not dec.eof:
                return None
            return result
    except Exception:
        return None
    return None


class _UsageScan:
    """Observe terminal SSE metadata while forwarding bytes without modification."""

    def __init__(self) -> None:
        self.line = bytearray()
        self.oversize = False
        self.usage: dict | None = None
        self.status = "error"
        self.completed_event = False
        self.finished = False

    def feed(self, chunk: bytes) -> None:
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if len(self.line) + len(part) > MAX_SSE_LINE:
                self.line.clear()
                self.oversize = True
            elif not self.oversize:
                self.line.extend(part)
            if index < len(parts) - 1:
                self._line()

    def _line(self) -> None:
        if self.oversize:
            self.oversize = False
            self.line.clear()
            return
        data = bytes(self.line).strip()
        self.line.clear()
        if not data and self.completed_event:
            self.finished = True
            return
        if not data.startswith(b"data:") or len(data) > MAX_SSE_LINE:
            return
        try:
            event = json.loads(data[5:])
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "response.completed":
            self.completed_event = True
            self.status = "ok"
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                self.usage = response["usage"]
        elif kind in ("response.failed", "response.incomplete"):
            self.status = "error"


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32
    max_inflight = 16

    def handle_error(self, request, client_address) -> None:
        # BaseServer prints a traceback; upstream errors can contain sensitive text.
        pass

    def __init__(self, address: tuple[str, int], capability: str, router: Router) -> None:
        if address[0] != "127.0.0.1":
            raise ValueError("loopback bind required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", capability):
            raise ValueError("invalid capability")
        self.capability = capability
        self.router = router
        self.slots = threading.BoundedSemaphore(self.max_inflight)
        super().__init__(address, RouterHandler)


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Effortlane"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(READ_TIMEOUT)

    def log_message(self, *args) -> None:
        return

    def _send(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def _allowed(self) -> bool:
        if self.client_address[0] != "127.0.0.1" or self.headers.get("Origin") is not None:
            return False
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        host = hosts[0]
        parsed = urlsplit("//" + host)
        try:
            return parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == self.server.server_port and not parsed.path
        except ValueError:
            return False

    def _endpoint(self) -> str | None:
        path = self.path
        if "?" in self.path:
            return None
        prefixes = ["/" + self.server.capability, "/" + self.server.capability + "/cli"]
        route_token = self._cli_route_token()
        if route_token:
            prefixes.append("/" + self.server.capability + "/cli/" + route_token)
        for prefix in prefixes:
            for endpoint in ("/responses", "/responses/compact"):
                if hmac.compare_digest(path, prefix + endpoint):
                    return endpoint
        return None

    def _cli_route_token(self) -> str | None:
        path = self.path.split("?", 1)[0]
        match = re.fullmatch(r"/" + re.escape(self.server.capability) +
                             r"/cli/([0-9a-f]{16})/(?:responses(?:/compact)?|models)", path)
        return match.group(1) if match else None

    def _cli_path(self) -> bool:
        return self.path.startswith("/" + self.server.capability + "/cli/")

    def do_GET(self) -> None:
        if not self._allowed():
            return self._send(403)
        if self.path == "/health":
            return self._send(200, b'{"ok":true}')
        parts = urlsplit(self.path)
        prefixes = ["/" + self.server.capability, "/" + self.server.capability + "/cli"]
        route_token = self._cli_route_token()
        if route_token:
            prefixes.append("/" + self.server.capability + "/cli/" + route_token)
        if any(hmac.compare_digest(parts.path, prefix + "/models") for prefix in prefixes):
            if parts.query and not re.fullmatch(r"client_version=[A-Za-z0-9._-]{1,64}", parts.query):
                return self._send(400)
            return self._get_models(parts.query)
        if self._endpoint() == "/responses" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self._send(426)
        self._send(404)

    def _get_models(self, query: str) -> None:
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._send(401)
        upstream = None
        headers_sent = False
        try:
            upstream = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=UPSTREAM_TIMEOUT)
            path = UPSTREAM_BASE + "/models" + (("?" + query) if query else "")
            upstream.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            upstream.putheader("Host", UPSTREAM_HOST)
            for name, value in self.headers.items():
                key = name.lower()
                if key not in HOP_HEADERS and key not in BLOCKED_REQUEST_HEADERS and key != "accept-encoding":
                    upstream.putheader(name, value)
            upstream.putheader("Accept-Encoding", "identity")
            upstream.endheaders()
            response = upstream.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            self.close_connection = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException, ValueError):
            if not headers_sent:
                try:
                    self._send(502)
                except OSError:
                    pass
        finally:
            if upstream is not None:
                upstream.close()

    def do_POST(self) -> None:
        if not self._allowed():
            return self._send(403)
        endpoint = self._endpoint()
        if endpoint is None:
            return self._send(404)
        if not self.server.slots.acquire(blocking=False):
            return self._send(503)
        try:
            self._post(endpoint)
        finally:
            self.server.slots.release()

    def _post(self, endpoint: str) -> None:
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._send(401)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            return self._send(400)
        try:
            length = int(lengths[0])
        except ValueError:
            return self._send(400)
        if length < 1:
            return self._send(400)
        if length > MAX_BODY:
            return self._send(413)
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            return self._send(400)
        if len(body) != length:
            return self._send(400)
        encoding = self.headers.get("Content-Encoding", "").lower().strip()
        decoded = _decode_bounded(body, encoding)
        if decoded is None:
            return self._send(415)
        client = "cli" if self._cli_path() else _client(self.headers)
        route_token = self._cli_route_token() if client == "cli" else None
        session_id = "cli-" + route_token if route_token else self.headers.get("Thread-Id") or self.headers.get("Session-Id")
        rewritten, decision = _rewrite(decoded, self.server.router, client, session_id)
        if decision is not None:
            decision["client"] = client
            if route_token:
                decision["route_id"] = cli_route_id(route_token)
        if decision is not None and decision.get("model") is None:
            return self._send(503)
        was_alias = decision is not None and decision.get("_alias") is True
        if was_alias:
            body = rewritten
        upstream = None
        scan = _UsageScan()
        headers_sent = False
        try:
            upstream = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=UPSTREAM_TIMEOUT)
            upstream.putrequest("POST", UPSTREAM_BASE + endpoint, skip_host=True, skip_accept_encoding=True)
            upstream.putheader("Host", UPSTREAM_HOST)
            for name, value in self.headers.items():
                key = name.lower()
                if key not in HOP_HEADERS and key not in BLOCKED_REQUEST_HEADERS and key != "x-jev-client" and key != "accept-encoding" and not (was_alias and key == "content-encoding"):
                    upstream.putheader(name, value)
            upstream.putheader("Accept-Encoding", "identity")
            upstream.putheader("Content-Length", str(len(body)))
            upstream.endheaders(body)
            response = upstream.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            self.close_connection = True
            response_encoding = response.getheader("Content-Encoding", "identity").lower()
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if response_encoding in ("", "identity"):
                    scan.feed(chunk)
                    # Native Codex also ends on the completed SSE event. The
                    # upstream connection may remain open after that event.
                    if scan.finished:
                        break
            if decision is not None:
                try:
                    self.server.router.record_usage(decision, scan.usage, scan.status if response.status == 200 else "error")
                except Exception:
                    pass
        except (OSError, http.client.HTTPException, ValueError):
            if not headers_sent:
                if decision is not None:
                    try:
                        self.server.router.record_usage(decision, None, "error")
                    except Exception:
                        pass
                try:
                    self._send(502)
                except OSError:
                    pass
            elif decision is not None:
                try:
                    self.server.router.record_usage(decision, scan.usage, "error")
                except Exception:
                    pass
        finally:
            if upstream is not None:
                upstream.close()


def create_server(root: Path) -> RouterServer:
    config = json.loads((root / "config.json").read_text())
    capability_path = Path(config.get("capability_file", root / "capability"))
    mode = capability_path.stat().st_mode
    if mode & 0o077:
        raise ValueError("capability file must be owner-only")
    capability = capability_path.read_text().strip()
    router = Router(
        config_path=root / "config.json",
        catalog_path=Path(config.get("native_catalog_path", root / "native-models.json")),
        state_path=root / "state" / "leases.json",
        telemetry_path=Path(config.get("telemetry_file", root / "state" / "telemetry.jsonl")),
    )
    port = int(config.get("port", 43191))
    return RouterServer(("127.0.0.1", port), capability, router)


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback Codex Responses router")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    server = create_server(args.root)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
