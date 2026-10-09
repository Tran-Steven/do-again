from __future__ import annotations

import json
import socket
import struct
import unittest
from unittest.mock import Mock, patch

from do_again.browser import cdp


class WebSocketTests(unittest.TestCase):
    def connection(self):
        client, server = socket.socketpair()
        client.settimeout(1)
        server.settimeout(1)
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        ws = cdp.WebSocket.__new__(cdp.WebSocket)
        ws.sock = client
        ws._buffer = bytearray()
        return ws, server

    def frame(self, opcode, payload, final=True):
        return bytes([(0x80 if final else 0) | opcode, len(payload)]) + payload

    def test_fragmented_utf8_response_with_interleaved_ping(self):
        ws, server = self.connection()
        payload = json.dumps({"result": "音楽"}, ensure_ascii=False).encode()
        split = payload.index("音".encode()) + 1
        server.sendall(self.frame(1, payload[:split], final=False) + self.frame(9, b"ping") + self.frame(0, payload[split:]))
        self.assertEqual(json.loads(ws.recv_text()), {"result": "音楽"})
        pong = server.recv(100)
        self.assertEqual(pong[0] & 0xF, 0xA)
        self.assertTrue(pong[1] & 0x80)

    def test_large_frame_limit_rejects_before_reading_payload(self):
        ws, server = self.connection()
        server.sendall(bytes([0x81, 127]) + struct.pack("!Q", 16 * 1024 * 1024 + 1))
        with self.assertRaises(cdp.CdpError):
            ws.recv_text()

    def test_orphan_continuation_frame_is_rejected(self):
        ws, server = self.connection()
        server.sendall(self.frame(0, b"fragment"))
        with self.assertRaises(cdp.CdpError):
            ws.recv_text()

    def test_event_stream_cannot_extend_call_deadline(self):
        ws = Mock()
        ws.recv_text.return_value = json.dumps({"method": "event"})
        with patch.object(cdp, "WebSocket", return_value=ws), patch.object(cdp.time, "monotonic", side_effect=[0, 0.5, 2]):
            with self.assertRaises(cdp.CdpError):
                cdp.call("ws://127.0.0.1/test", "Runtime.evaluate", timeout=1)
        ws.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
