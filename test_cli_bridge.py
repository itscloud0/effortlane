import io
from contextlib import redirect_stderr
from pathlib import Path
import socket
import struct
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

import cli_bridge


def client_frame(data: bytes, opcode: int = 1, fin: bool = True) -> bytes:
    mask = b"test"
    size = len(data)
    length = bytes([0x80 | size]) if size < 126 else b"\xfe" + struct.pack("!H", size)
    return bytes([(0x80 if fin else 0) | opcode]) + length + mask + bytes(
        value ^ mask[index & 3] for index, value in enumerate(data))


class WebSocketTests(unittest.TestCase):
    def test_debug_prefix_uses_effortlane_brand(self):
        output = io.StringIO()
        with (patch.dict("os.environ", {"JEV_BRIDGE_DEBUG": "1"}),
              redirect_stderr(output)):
            cli_bridge._debug("connected")
        self.assertEqual(output.getvalue(), "[Effortlane bridge] connected\n")
        self.assertNotIn("Jev", output.getvalue())

    def test_background_server_cannot_write_into_foreground_terminal(self):
        child = Mock(stdin=io.BytesIO(), stdout=io.BytesIO())
        connection = MagicMock()
        with (patch.object(cli_bridge.subprocess, 'Popen', return_value=child) as launch,
              patch.object(cli_bridge, 'Adapter'),
              patch.object(cli_bridge, 'cli_catalog_path', return_value=Path('/tmp/catalog.json'))):
            cli_bridge._serve_connection(connection, io.BytesIO(), Path('/tmp'), Path('/native'))
        self.assertEqual(launch.call_args.kwargs['stderr'], cli_bridge.subprocess.DEVNULL)
        self.assertEqual(launch.call_args.kwargs['stdout'], cli_bridge.subprocess.PIPE)

    def test_selected_alias_accepts_native_cli_model_syntaxes(self):
        for args, expected in ((['-m', 'effortlane-auto'], 'effortlane-auto'),
                               (['--model=effortlane-shadow'], 'effortlane-shadow'),
                               (['-c', 'model="effortlane-auto"'], 'effortlane-auto'),
                               (['--config=model="effortlane-shadow"'], 'effortlane-shadow'),
                               (['-m', 'gpt-6-sol'], None)):
            self.assertEqual(cli_bridge.selected_alias(args), expected)

    def test_bridge_failure_strips_alias_override(self):
        self.assertEqual(cli_bridge.fallback_args(["-m", "effortlane-auto", "resume", "--last"], "gpt-6-sol"),
                         ["-m", "gpt-6-sol", "resume", "--last"])
        self.assertEqual(cli_bridge.fallback_args(["-c", 'model="effortlane-shadow"'], "gpt-6-sol"),
                         ["-m", "gpt-6-sol"])
        self.assertEqual(cli_bridge.fallback_args(['--config=model="effortlane-auto"'], "gpt-6-sol"),
                         ["-m", "gpt-6-sol"])

    def test_legacy_aliases_normalize_and_never_reach_fallback_executor(self):
        for legacy, canonical in (("jev-auto", "effortlane-auto"), ("jev-shadow", "effortlane-shadow")):
            for args in (["-m", legacy], ["--model=" + legacy],
                         ["-c", 'model="' + legacy + '"'], ['--config=model="' + legacy + '"']):
                self.assertEqual(cli_bridge.selected_alias(args), canonical)
                self.assertEqual(cli_bridge.fallback_args([*args, "resume", "--last"], "gpt-6-sol"),
                                 ["-m", "gpt-6-sol", "resume", "--last"])

    def test_masked_text_and_fragmentation(self):
        wire = client_frame(b'{"method":', fin=False) + client_frame(b'"initialize"}', opcode=0)
        self.assertEqual(cli_bridge.read_message(io.BytesIO(wire)), b'{"method":"initialize"}')

    def test_rejects_unmasked_or_oversize(self):
        with self.assertRaisesRegex(ValueError, "invalid WebSocket frame"):
            cli_bridge.read_message(io.BytesIO(b"\x81\x02hi"))
        with self.assertRaisesRegex(ValueError, "too large"):
            cli_bridge.read_message(io.BytesIO(b"\x81\xff" + struct.pack("!Q", cli_bridge.MAX_MESSAGE + 1)))

    def test_ping_returns_pong(self):
        class Sink:
            data = b""

            def sendall(self, data):
                self.data += data

        sink = Sink()
        payload = client_frame(b"alive", opcode=9) + client_frame(b"ok")
        self.assertEqual(cli_bridge.read_message(io.BytesIO(payload), sink), b"ok")
        self.assertEqual(sink.data, b"\x8a\x05alive")

    def test_handshake_requires_loopback_host(self):
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        class Sink:
            data = b""

            def sendall(self, data):
                self.data += data

        good = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:1234\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode()
        sink = Sink()
        cli_bridge._handshake(io.BytesIO(good), sink)
        self.assertIn(b"101 Switching Protocols", sink.data)
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            cli_bridge._handshake(io.BytesIO(good), Sink(), "secret")
        authorized = good.replace(b"\r\n\r\n", b"\r\nAuthorization: Bearer secret\r\n\r\n")
        cli_bridge._handshake(io.BytesIO(authorized), Sink(), "secret")
        with self.assertRaisesRegex(ValueError, "invalid WebSocket upgrade"):
            cli_bridge._handshake(io.BytesIO(good.replace(b"127.0.0.1", b"example.com")), Sink())

    def test_stray_connection_does_not_consume_tui_bridge(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            result = []

            def accept():
                connection, stream = cli_bridge._accept_authorized(listener, "test-token", timeout=2)
                result.append(connection)
                stream.close()

            worker = threading.Thread(target=accept)
            worker.start()
            try:
                with socket.create_connection(listener.getsockname()) as stray:
                    stray.sendall(b"GET /bad HTTP/1.1\r\n\r\n")
                with socket.create_connection(listener.getsockname()) as client:
                    client.settimeout(2)
                    client.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{listener.getsockname()[1]}\r\n"
                                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                                    "Authorization: Bearer test-token\r\n\r\n").encode())
                    self.assertIn(b"101 Switching Protocols", client.recv(256))
            finally:
                worker.join(timeout=3)
                for connection in result:
                    connection.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)

    def test_native_exit_before_connection_has_diagnostic_without_restart(self):
        output = io.StringIO()
        aliases = []
        def mark_ready(*args, **kwargs):
            aliases.append(kwargs["args"][-1])
            return Mock(start=lambda: kwargs["args"][5].set())

        with (patch.object(cli_bridge.threading, "Thread", side_effect=mark_ready),
              patch.object(cli_bridge.subprocess, "Popen") as popen,
              patch.object(cli_bridge.subprocess, "call") as direct,
              redirect_stderr(output)):
            popen.return_value.wait.return_value = 1
            self.assertEqual(cli_bridge.run(Path("/tmp"), Path("/native"), ["-m", "effortlane-auto", "resume"]), 1)
        self.assertIsNone(popen.call_args.kwargs["stderr"])
        direct.assert_not_called()
        self.assertEqual(aliases, ["effortlane-auto"])
        self.assertIn("Effortlane TUI bridge: Codex exited before authentication", output.getvalue())
        self.assertIn("last stage: starting accept thread", output.getvalue())
        self.assertNotIn("Jev", output.getvalue())

    def test_exit_after_connection_does_not_restart_native_tui(self):
        def mark_connected(*args, **kwargs):
            return Mock(start=lambda: (kwargs["args"][4].set(), kwargs["args"][5].set()))

        with (patch.object(cli_bridge.threading, "Thread", side_effect=mark_connected),
              patch.object(cli_bridge.subprocess, "Popen") as popen,
              patch.object(cli_bridge.subprocess, "call") as direct):
            popen.return_value.wait.return_value = 1
            self.assertEqual(cli_bridge.run(Path("/tmp"), Path("/native"), ["resume"]), 1)
        direct.assert_not_called()

    def test_accept_thread_signals_ready_before_native_launch(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            ready = threading.Event()
            result = []

            def accept():
                try:
                    connection, stream = cli_bridge._accept_authorized(
                        listener, "test-token", timeout=2, ready=ready)
                    stream.close()
                    connection.close()
                except TimeoutError:
                    result.append("timeout")

            worker = threading.Thread(target=accept)
            worker.start()
            self.assertTrue(ready.wait(timeout=1))
            worker.join(timeout=3)
            self.assertEqual(result, ["timeout"])

    def test_picker_can_open_second_connection_while_main_tui_is_active(self):
        release = threading.Event()
        second = threading.Event()
        served = []

        def handler(sock, stream, root, native):
            with sock, stream:
                served.append(1)
                if len(served) == 2:
                    second.set()
                release.wait(timeout=2)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            stopped = threading.Event()
            with patch.object(cli_bridge, "_serve_connection", side_effect=handler):
                server = threading.Thread(target=cli_bridge.serve_one,
                                          args=(listener, Path("/tmp"), Path("/native"), "test-token"),
                                          kwargs={"stopped": stopped})
                server.start()
                try:
                    for _ in range(2):
                        with socket.create_connection(listener.getsockname(), timeout=2) as client:
                            client.settimeout(2)
                            client.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{listener.getsockname()[1]}\r\n"
                                            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                                            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                                            "Authorization: Bearer test-token\r\n\r\n").encode())
                            self.assertIn(b"101 Switching Protocols", client.recv(256))
                    self.assertTrue(second.wait(timeout=1))
                finally:
                    release.set()
                    stopped.set()
                    server.join(timeout=3)
                self.assertFalse(server.is_alive())


if __name__ == "__main__":
    unittest.main()
