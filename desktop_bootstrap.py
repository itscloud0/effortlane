#!/usr/bin/env python3
"""Keep native Codex directly parented by Desktop while routing stdio separately."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading

from rpc_adapter import Adapter, _relay, native_server_command


def run(native: Path, root: Path, command: list[str]) -> int:
    if not native.is_absolute() or not native.is_file() or not os.access(native, os.X_OK):
        print(f"Effortlane Desktop: native Codex missing or not executable: {native}", file=sys.stderr)
        return 127
    command = native_server_command(root, command)
    listen = next((command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "--listen"), None)
    listen = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--listen=")), listen)
    subcommand = 0
    while subcommand < len(command) and command[subcommand] in ("-c", "--config"):
        subcommand += 2
    if (subcommand >= len(command) or command[subcommand] != "app-server"
            or listen not in (None, "stdio://") or "--listen-tcp" in command):
        os.execv(str(native), [str(native), *command])

    native_input, adapter_output = os.pipe()
    adapter_input, native_output = os.pipe()
    # macOS may abort a Python child between fork and exec when Objective-C
    # libraries have initialized. posix_spawn runs no Python in that gap.
    for fd in (adapter_output, adapter_input):
        os.set_inheritable(fd, True)
    try:
        os.posix_spawn(sys.executable,
                       [sys.executable, str(Path(__file__).resolve()), "--sidecar",
                        "--root", str(root), "--to-native-fd", str(adapter_output),
                        "--from-native-fd", str(adapter_input)], os.environ.copy())
    except OSError:
        for fd in (native_input, adapter_output, adapter_input, native_output):
            os.close(fd)
        os.execv(str(native), [str(native), *command])
    # This PID is Desktop's direct child. The signed native binary replaces
    # Python here; codex_app sees node <- codex <- ChatGPT.
    os.close(adapter_output)
    os.close(adapter_input)
    os.dup2(native_input, 0)
    os.dup2(native_output, 1)
    os.close(native_input)
    os.close(native_output)
    os.execv(str(native), [str(native), *command])


def sidecar(root: Path, to_native_fd: int, from_native_fd: int) -> int:
    """Relay on inherited Desktop stdio without parenting the signed executor."""
    try:
        adapter = Adapter(root)
        client, server = adapter.client, adapter.server
    except Exception:
        # Routing must never make Desktop's app-server unavailable.
        client = server = lambda raw: raw
    input_stream = os.fdopen(to_native_fd, "wb", buffering=0)
    output_stream = os.fdopen(from_native_fd, "rb", buffering=0)

    def input_pump() -> None:
        _relay(sys.stdin.buffer, input_stream, client)
        input_stream.close()

    threading.Thread(target=input_pump, daemon=True).start()
    _relay(output_stream, sys.stdout.buffer, server)
    output_stream.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sidecar", action="store_true")
    parser.add_argument("--native", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--to-native-fd", type=int)
    parser.add_argument("--from-native-fd", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    opts = parser.parse_args(argv)
    if opts.sidecar:
        if opts.to_native_fd is None or opts.from_native_fd is None:
            parser.error("sidecar requires both pipe descriptors")
        result = sidecar(opts.root, opts.to_native_fd, opts.from_native_fd)
        # The input relay may still be blocked on Desktop's stdin after Codex
        # closes stdout. Python finalization would race that daemon thread and
        # abort while closing the buffered reader; the OS closes FDs on exit.
        os._exit(result)
    if opts.native is None:
        parser.error("--native is required")
    return run(opts.native, opts.root,
               opts.command[1:] if opts.command[:1] == ["--"] else opts.command)


if __name__ == "__main__":
    raise SystemExit(main())
