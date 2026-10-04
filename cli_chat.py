"""Small terminal client for native Codex app-server with pre-turn Effortlane routing."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time


from core import normalize_alias


MODELS = re.compile(r"(?:(?:effortlane|jev)-(?:auto|shadow)|gpt-\d+(?:\.\d+)*-[a-z0-9]+)\Z")
THREADS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
MAX_FRAME = 8 * 1024 * 1024


def display_model(model: str) -> str:
    return {"effortlane-auto": "Effortlane Auto", "effortlane-shadow": "Effortlane Shadow"}.get(normalize_alias(model), model)


class CodexClient:
    def __init__(self, root: Path, model: str, *, stdin=None, stdout=None, stderr=None):
        self.root = root
        self.model = normalize_alias(model) or model
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        manifest = json.loads((root / "manifest.json").read_text())
        native = Path(manifest["native_target"])
        if not native.is_file() or not os.access(native, os.X_OK):
            raise RuntimeError("native Codex binary missing or not executable")
        self.process = subprocess.Popen(
            [sys.executable, str(root / "rpc_adapter.py"), "--native", str(native),
             "--root", str(root), "--client", "cli", "--", "-c",
             'openai_base_url="https://chatgpt.com/backend-api/codex"',
             "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0)
        self.serial = 0
        self.thread_id: str | None = None
        self.turn_done = False
        self.turn_status = "unknown"
        self.streamed: set[str] = set()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def send(self, message: dict) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("Codex app-server stopped")
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def _read(self, deadline: float | None = None) -> dict:
        assert self.process.stdout is not None
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("Codex app-server stopped")
            timeout = 1.0 if deadline is None else min(1.0, max(0.0, deadline - time.monotonic()))
            if deadline is not None and timeout <= 0:
                raise TimeoutError("Codex app-server did not respond")
            if not select.select([self.process.stdout], [], [], timeout)[0]:
                continue
            raw = self.process.stdout.readline(MAX_FRAME + 1)
            if not raw:
                raise RuntimeError("Codex app-server closed its output")
            if len(raw) > MAX_FRAME:
                raise RuntimeError("Codex app-server frame too large")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict):
                return value

    def request(self, method: str, params: dict, timeout: float = 30) -> dict:
        self.serial += 1
        rid = self.serial
        self.send({"id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._read(deadline)
            if message.get("id") == rid and "method" not in message:
                if "error" in message:
                    error = message["error"]
                    summary = error.get("message", "request failed") if isinstance(error, dict) else "request failed"
                    raise RuntimeError(f"{method}: {str(summary)[:240]}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            self._event(message)

    def initialize(self, resume: str | None = None, *, last: bool = False) -> str:
        self.request("initialize", {"clientInfo": {"name": "jev-codex-cli", "version": "1.0"}, "capabilities": {}})
        self.send({"method": "initialized", "params": {}})
        if last:
            listed = self.request("thread/list", {"cwd": str(Path.cwd()), "limit": 1,
                                                  "sortKey": "updated_at", "sortDirection": "desc",
                                                  "useStateDbOnly": True})
            threads = listed.get("data")
            latest = threads[0] if isinstance(threads, list) and threads else None
            resume = latest.get("id") if isinstance(latest, dict) else None
            if not isinstance(resume, str) or not THREADS.fullmatch(resume):
                raise RuntimeError("no recent Codex thread found in this directory")
        if resume:
            result = self.request("thread/resume", {"threadId": resume, "model": self.model, "cwd": str(Path.cwd())})
        else:
            result = self.request("thread/start", {"model": self.model, "cwd": str(Path.cwd())})
        thread = result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not THREADS.fullmatch(thread_id):
            raise RuntimeError("Codex returned no valid thread ID")
        self.thread_id = thread_id
        print(f"thread {thread_id}", file=self.stderr)
        return thread_id

    def _approval(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        rid = message.get("id")
        if method in ("item/commandExecution/requestApproval", "execCommandApproval",
                      "item/fileChange/requestApproval", "applyPatchApproval"):
            subject = params.get("command") if isinstance(params.get("command"), str) else params.get("reason")
            if not isinstance(subject, str):
                subject = method
            print(f"\napproval requested: {subject[:2000]}", file=self.stderr)
            allowed = False
            if self.stdin.isatty():
                print("Allow once? [y/N] ", end="", file=self.stderr, flush=True)
                allowed = self.stdin.readline().strip().lower() in ("y", "yes")
            self.send({"id": rid, "result": {"decision": "accept" if allowed else "decline"}})
            return
        if method == "item/tool/requestUserInput":
            answers = {}
            for question in params.get("questions", []):
                if not isinstance(question, dict) or not isinstance(question.get("id"), str):
                    continue
                print(f"\n{str(question.get('question', 'Input'))[:1000]}", file=self.stderr)
                for option in question.get("options") or []:
                    if isinstance(option, dict):
                        print(f"  {str(option.get('label', ''))[:120]}", file=self.stderr)
                if self.stdin.isatty():
                    answer = getpass.getpass("answer: ") if question.get("isSecret") is True else input("answer: ")
                else:
                    answer = ""
                answers[question["id"]] = {"answers": [answer]}
            self.send({"id": rid, "result": {"answers": answers}})
            return
        print(f"\nunsupported Codex client request: {method}; declined", file=self.stderr)
        self.send({"id": rid, "error": {"code": -32601, "message": "unsupported terminal client request"}})

    def _event(self, message: dict) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        if "id" in message:
            self._approval(message)
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                self.streamed.add(str(params.get("itemId")))
                print(delta, end="", file=self.stdout, flush=True)
        elif method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage" and str(item.get("id")) not in self.streamed:
                text = item.get("text")
                if isinstance(text, str):
                    print(text, file=self.stdout, flush=True)
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.turn_status = str(turn.get("status") or "unknown")
            self.turn_done = True
            print("", file=self.stdout, flush=True)

    def turn(self, prompt: str) -> str:
        if not self.thread_id:
            raise RuntimeError("thread not initialized")
        self.turn_done = False
        self.streamed.clear()
        self.request("turn/start", {"threadId": self.thread_id, "model": self.model,
                                    "input": [{"type": "text", "text": prompt}]}, timeout=60)
        # The adapter has already selected and logged the concrete executor.
        display = display_model(self.model)
        try:
            from rpc_adapter import IntentStore
            entry = IntentStore(self.root / "state/desktop-intent.json").get(self.thread_id)
            if entry and entry.get("alias") == self.model:
                display = f"{entry.get('actual', 'Codex')}/{entry.get('effort', '?')}"
        except Exception:
            pass
        print(f"[{display}]", file=self.stderr)
        while not self.turn_done:
            self._event(self._read())
        return self.turn_status


def run(argv: list[str], root: Path) -> int:
    parser = argparse.ArgumentParser(prog="effortlane chat")
    previous = parser.add_mutually_exclusive_group()
    previous.add_argument("--resume", metavar="THREAD_UUID")
    previous.add_argument("--last", action="store_true", help="resume the most recently updated thread in this directory")
    parser.add_argument("--model", metavar="MODEL", default="effortlane-auto",
                        help="effortlane-auto, effortlane-shadow, or a concrete GPT model")
    parser.add_argument("--once", action="store_true", help="read one prompt from stdin, then exit")
    from manage import brand_cli_args
    args = parser.parse_args(brand_cli_args(argv))
    if not MODELS.fullmatch(args.model):
        parser.error("model must be effortlane-auto, effortlane-shadow, or a concrete GPT model")
    if args.resume and not THREADS.fullmatch(args.resume):
        parser.error("--resume requires a thread UUID")
    client = CodexClient(root, args.model)
    try:
        client.initialize(args.resume, last=args.last)
        print("Type a task; /model NAME changes the next turn, /exit quits.", file=sys.stderr)
        while True:
            if args.once:
                prompt = sys.stdin.read(20_001)
            else:
                prompt = input("effortlane> ")
            if not prompt or prompt.strip() == "/exit":
                break
            if not args.once and prompt.startswith("/model "):
                model = prompt.removeprefix("/model ").strip()
                model = normalize_alias(model) or model
                if MODELS.fullmatch(model):
                    client.model = model
                    print("model " + display_model(model), file=sys.stderr)
                else:
                    print("invalid model", file=sys.stderr)
                continue
            status = client.turn(prompt)
            if status not in ("completed", "unknown"):
                print(f"turn status: {status}", file=sys.stderr)
            if args.once:
                break
        return 0
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return 130
    finally:
        client.close()
