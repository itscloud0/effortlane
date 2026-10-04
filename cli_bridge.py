#!/usr/bin/env python3
"""Loopback app-server bridge for the unmodified Codex TUI.

Codex's --remote option speaks WebSocket.  The native app-server speaks the same
JSON-RPC protocol over stdio, where the existing Desktop adapter can route a
turn before Codex constructs its model request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

from rpc_adapter import Adapter, native_server_command
from manage import cli_catalog_path


MAX_MESSAGE = 64 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _debug(event: str) -> None:
    if os.environ.get("JEV_BRIDGE_DEBUG") == "1":
        print(f"[Effortlane bridge] {event}", file=sys.stderr, flush=True)


def _read_exact(stream, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def read_message(stream, sock: socket.socket | None = None) -> bytes | None:
    """Read one masked client text message, including continuation frames."""
    parts = bytearray()
    continuing = False
    while True:
        first, second = _read_exact(stream, 2)
        opcode, fin = first & 15, bool(first & 128)
        if first & 112 or not second & 128:
            raise ValueError("invalid WebSocket frame")
        length = second & 127
        if length == 126:
            length = struct.unpack("!H", _read_exact(stream, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(stream, 8))[0]
        if length > MAX_MESSAGE or len(parts) + length > MAX_MESSAGE:
            raise ValueError("WebSocket message too large")
        mask = _read_exact(stream, 4)
        data = _read_exact(stream, length)
        if opcode == 8:
            return None
        if opcode in (9, 10):
            if length > 125 or not fin:
                raise ValueError("invalid WebSocket control frame")
            if opcode == 9 and sock is not None:
                sock.sendall(frame(bytes(byte ^ mask[index & 3]
                                         for index, byte in enumerate(data)), opcode=10))
            continue
        if opcode != (0 if continuing else 1):
            raise ValueError("unexpected WebSocket opcode")
        parts.extend(byte ^ mask[index & 3] for index, byte in enumerate(data))
        continuing = True
        if fin:
            return bytes(parts)


def frame(data: bytes, opcode: int = 1) -> bytes:
    length = len(data)
    if length > MAX_MESSAGE:
        raise ValueError("WebSocket message too large")
    prefix = bytes([0x80 | opcode])
    if length < 126:
        prefix += bytes([length])
    elif length <= 65535:
        prefix += b"\x7e" + struct.pack("!H", length)
    else:
        prefix += b"\x7f" + struct.pack("!Q", length)
    return prefix + data


def _headers(stream) -> dict[str, str]:
    line = stream.readline(4097)
    if line != b"GET / HTTP/1.1\r\n":
        raise ValueError("invalid WebSocket request")
    headers = {}
    for _ in range(64):
        line = stream.readline(4097)
        if line == b"\r\n":
            return headers
        if not line.endswith(b"\r\n") or b":" not in line:
            raise ValueError("invalid WebSocket header")
        name, value = line.split(b":", 1)
        headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
    raise ValueError("too many WebSocket headers")


def _handshake(stream, sock: socket.socket, token: str | None = None) -> None:
    headers = _headers(stream)
    key = headers.get("sec-websocket-key", "")
    host = headers.get("host", "")
    if (headers.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in headers.get("connection", "").lower()
            or not re.fullmatch(r"127\.0\.0\.1:\d+", host)
            or len(key) != 24 or not re.fullmatch(r"[A-Za-z0-9+/]{22}==", key)):
        raise ValueError("invalid WebSocket upgrade")
    if token is not None and not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + token):
        raise ValueError("unauthorized WebSocket upgrade")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode("ascii"))


def _accept_authorized(listener: socket.socket, token: str, timeout: float = 20,
                       ready: threading.Event | None = None,
                       progress: dict | None = None) -> tuple[socket.socket, object]:
    """Ignore stray local connections until the authenticated TUI connects."""
    deadline = time.monotonic() + timeout
    if progress is not None:
        progress["stage"] = "waiting for connection"
    if ready is not None:
        ready.set()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex did not connect to the Effortlane bridge")
        listener.settimeout(remaining)
        try:
            connection, address = listener.accept()
        except socket.timeout as exc:
            raise TimeoutError("Codex did not connect to the Effortlane bridge") from exc
        _debug("accepted")
        if progress is not None:
            progress["stage"] = "reading WebSocket handshake"
        stream = connection.makefile("rb")
        try:
            if address[0] != "127.0.0.1":
                raise ValueError("non-loopback connection")
            connection.settimeout(min(remaining, 5))
            _handshake(stream, connection, token)
        except (OSError, UnicodeError, ValueError) as exc:
            _debug(f"handshake failed: {type(exc).__name__}")
            stream.close()
            connection.close()
            if progress is not None:
                progress["stage"] = "waiting after rejected handshake"
            continue
        connection.settimeout(None)
        if progress is not None:
            progress["stage"] = "authenticated"
        return connection, stream


def _serve_connection(connection: socket.socket, stream, root: Path, native: Path,
                      initial_alias: str | None = None) -> None:
    _debug("connected")
    with connection, stream:
        command = native_server_command(root,
                    ["-c", "model_catalog_json=" + json.dumps(str(cli_catalog_path(root, "native-models.json"))),
                     "app-server", "--listen", "stdio://"], cli_catalog_path(root, "native-models.json"))
        child = subprocess.Popen([str(native), *command],
                                 # Only the foreground TUI owns the terminal. Backend stderr may
                                 # contain raw command text and corrupt the input editor.
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        adapter = Adapter(root, client="cli", initial_alias=initial_alias,
                          catalog_path=cli_catalog_path(root, "native-models.json"))
        assert child.stdin is not None and child.stdout is not None
        stop = threading.Event()

        def upstream() -> None:
            try:
                while not stop.is_set():
                    raw = child.stdout.readline(MAX_MESSAGE + 1)
                    if not raw or len(raw) > MAX_MESSAGE:
                        break
                    try:
                        raw = adapter.server(raw)
                    except Exception:
                        pass
                    if os.environ.get("JEV_BRIDGE_DEBUG") == "1":
                        try:
                            _debug("server " + str(json.loads(raw).get("method", "response"))[:80])
                        except (ValueError, UnicodeError):
                            _debug("server invalid")
                    connection.sendall(frame(raw.rstrip(b"\r\n")))
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                stop.set()

        reader = threading.Thread(target=upstream, daemon=True)
        reader.start()
        try:
            while not stop.is_set():
                raw = read_message(stream, connection)
                if raw is None:
                    break
                try:
                    raw = adapter.client(raw)
                except Exception:
                    pass
                if os.environ.get("JEV_BRIDGE_DEBUG") == "1":
                    try:
                        _debug("client " + str(json.loads(raw).get("method", "response"))[:80])
                    except (ValueError, UnicodeError):
                        _debug("client invalid")
                child.stdin.write(raw.rstrip(b"\r\n") + b"\n")
                child.stdin.flush()
        except (EOFError, BrokenPipeError, OSError, UnicodeError, ValueError):
            pass
        finally:
            _debug("disconnected")
            stop.set()
            try:
                child.stdin.close()
            except OSError:
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def serve_one(listener: socket.socket, root: Path, native: Path, token: str,
              connected: threading.Event | None = None,
              ready: threading.Event | None = None,
              progress: dict | None = None,
              stopped: threading.Event | None = None,
              initial_alias: str | None = None) -> None:
    """Serve the main TUI and its independent session-picker connections."""
    stopped = stopped or threading.Event()
    slots = threading.BoundedSemaphore(4)
    first = True
    while not stopped.is_set():
        try:
            connection, stream = _accept_authorized(
                listener, token, timeout=20 if first else 1,
                ready=ready if first else None, progress=progress if first else None)
        except TimeoutError as exc:
            if first and not stopped.is_set():
                if progress is not None:
                    progress["stage"] = "connection timed out"
                return
            continue
        except OSError:
            return
        if first:
            first = False
            if connected is not None:
                connected.set()
        if not slots.acquire(blocking=False):
            _debug("connection limit reached")
            stream.close()
            connection.close()
            continue

        def worker(sock=connection, reader=stream) -> None:
            try:
                if initial_alias is None:
                    _serve_connection(sock, reader, root, native)
                else:
                    _serve_connection(sock, reader, root, native, initial_alias)
            finally:
                slots.release()

        threading.Thread(target=worker, daemon=True).start()


def fallback_args(args: list[str], model: str) -> list[str]:
    """Strip the synthetic alias so an unavailable bridge runs native Sol."""
    clean = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg in ("-m", "--model") and index + 1 < len(args) and args[index + 1] in ("jev-auto", "jev-shadow"):
            skip = True
            continue
        if arg in ("--model=jev-auto", "--model=jev-shadow"):
            continue
        if arg.startswith("--config=model=") and arg.partition("model=")[2].strip().strip("\"'") in ("jev-auto", "jev-shadow"):
            continue
        if arg in ("-c", "--config") and index + 1 < len(args):
            name, sep, value = args[index + 1].partition("=")
            if name == "model" and sep and value.strip().strip("\"'") in ("jev-auto", "jev-shadow"):
                skip = True
                continue
        clean.append(arg)
    return ["-m", model, *clean]


def selected_alias(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        value = None
        if arg in ("-m", "--model") and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--model="):
            value = arg.partition("=")[2]
        elif arg in ("-c", "--config") and index + 1 < len(args):
            key, sep, candidate = args[index + 1].partition("=")
            value = candidate if sep and key == "model" else None
        elif arg.startswith("--config=model="):
            value = arg.partition("model=")[2]
        if isinstance(value, str) and value.strip().strip("\"'") in ("jev-auto", "jev-shadow"):
            return value.strip().strip("\"'")
    return None


def run(root: Path, native: Path, args: list[str]) -> int:
    """Run the real TUI against a private per-process bridge; never replace it."""
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = listener.getsockname()[1]
            listener.settimeout(20)
            token = secrets.token_urlsafe(32)
            connected = threading.Event()
            ready = threading.Event()
            stopped = threading.Event()
            progress: dict = {"stage": "starting accept thread"}
            initial_alias = selected_alias(args)
            bridge = threading.Thread(target=serve_one,
                                      args=(listener, root, native, token, connected, ready, progress, stopped, initial_alias), daemon=True)
            bridge.start()
            if not ready.wait(timeout=5):
                raise TimeoutError("Effortlane bridge accept thread did not start")
            child = subprocess.Popen([str(native), "--remote", f"ws://127.0.0.1:{port}",
                                      "--remote-auth-token-env", "JEV_CODEX_BRIDGE_TOKEN", *args],
                                     stdin=None, stdout=None, stderr=None,
                                     env={**os.environ, "JEV_CODEX_BRIDGE_TOKEN": token})
            code = child.wait()
            stopped.set()
            if code and not connected.is_set():
                print(f"Effortlane TUI bridge: Codex exited before authentication; last stage: {progress['stage']}.",
                      file=sys.stderr, flush=True)
            return code
    except (OSError, ValueError) as exc:
        print(f"Effortlane TUI bridge unavailable: {exc}; starting native Sol.", file=sys.stderr)
        try:
            fallback = json.loads((root / "config.json").read_text()).get("fallback_model")
        except (OSError, ValueError, AttributeError):
            fallback = None
        if not isinstance(fallback, str) or not re.fullmatch(r"gpt-\d+(?:\.\d+)*-sol", fallback):
            fallback = "gpt-6-sol"
        return subprocess.call([str(native), *fallback_args(args, fallback)])
