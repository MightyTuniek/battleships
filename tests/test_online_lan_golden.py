"""T1: transport boundary. Golden LAN bytes + loopback adapter exchange."""

import json
import socket
import sys
import unittest

sys.path.insert(0, ".")

import battleships as bs


class LanGoldenTest(unittest.TestCase):
    def test_golden_bytes_match_lan_recipe(self):
        # Independent recipe (stdlib primitives, not via LanTransport).
        key = b"K" * 32
        obj = {"type": "shot", "pos": [1, 2]}
        expected = (b'{"mac":"718d89b8f10d7638e19fe04a333e696edcf43d41495'
                    b'2b2b864aa4ffc484ce19b","pos":[1,2],"type":"shot"}\n')
        self.assertEqual(bs.LanTransport.encode_frame(obj, key), expected)
        # Cross-check: identical to the LAN module's own wire functions.
        import io

        class FakeSock:
            def __init__(self):
                self.buf = io.BytesIO()

            def sendall(self, data):
                self.buf.write(data)

            def getpeername(self):
                return ("127.0.0.1", 1)

        s = FakeSock()
        bs.send_json_obj(s, obj, key=key)
        self.assertEqual(s.buf.getvalue(), expected)

    def test_decode_roundtrip_and_bad_mac(self):
        key = b"K" * 32
        obj = {"type": "chat", "text": "hello"}
        raw = bs.LanTransport.encode_frame(obj, key)
        self.assertEqual(bs.LanTransport.decode_frame(raw, key), obj)
        tampered = raw.replace(b"hello", b"hellp")
        self.assertIsNone(bs.LanTransport.decode_frame(tampered, key))
        self.assertIsNone(bs.LanTransport.decode_frame(b"not json\n", key))

    def test_loopback_obj_exchange(self):
        a, b = socket.socketpair()
        try:
            key = b"Q" * 32
            ta = bs.LanTransport(a, key)
            tb = bs.LanTransport(b, key)
            try:
                for i in range(20):
                    ta.send_obj({"n": i, "type": "ping"})
                got = []
                for _ in range(20):
                    raw = tb.recv()
                    got.append(json.loads(raw.decode())["n"])
                self.assertEqual(got, list(range(20)))
            finally:
                ta.close()
                tb.close()
        finally:
            pass


if __name__ == "__main__":
    unittest.main()
