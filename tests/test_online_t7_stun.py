"""T7: STUN client and NAT check (spec 8.1). Loopback + fake servers only."""
import socket
import struct
import sys
import threading

sys.path.insert(0, ".")

import battleships as bs

# RFC 5769 section 2.2: sample IPv4 response, mapped 192.0.2.1:32853.
RFC5769_V4 = bytes.fromhex(
    "0101003c"
    "2112a442"
    "b7e7a701bc34d686fa87dfae"
    "8022000b7465737420766563746f7220"
    "002000080001a147e112a643"
    "000800142b91f599fd9e90c38c7489f92af9ba53f06be7d7"
    "80280004c07d4c96")
RFC5769_TXID = bytes.fromhex("b7e7a701bc34d686fa87dfae")

# Same envelope, IPv6 family (0x0002): v1 client must skip it -> None.
RFC5769_V6 = bytes.fromhex(
    "01010048"
    "2112a442"
    "b7e7a701bc34d686fa87dfae"
    "8022000b7465737420766563746f7220"
    "002000140002a147"
    "0113a9faa5d3f179bc25f4b5bed2b9d9"
    "00080014a382954e4be67bf11784c97c8292c275bfe3ed41"
    "80280004c8fb0b4c")


def _mapped_response(txid, ip="203.0.113.7", port=45678):
    """Minimal binding success with XOR-MAPPED-ADDRESS only."""
    cookie = 0x2112A442
    xport = port ^ (cookie >> 16)
    xip = struct.unpack(">I", socket.inet_aton(ip))[0] ^ cookie
    attr = struct.pack(">HH", 0x0020, 8) + struct.pack(
        ">BBHI", 0, 1, xport, xip)
    return (struct.pack(">HHI", 0x0101, len(attr), cookie) + txid + attr)


class FakeStun:
    """Loopback STUN server. drop_first=N ignores N requests; report
    overrides the mapped endpoint (mirror of sender by default)."""

    def __init__(self, drop_first=0, report=None):
        self.drop_first = drop_first
        self.report = report
        self.seen = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._closed = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def addr(self):
        return "127.0.0.1:%d" % self.port

    def _loop(self):
        while not self._closed:
            try:
                data, src = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 20:
                continue
            mtype, _mlen, cookie = struct.unpack(">HHI", data[:8])
            if mtype != 0x0001 or cookie != 0x2112A442:
                continue
            txid = data[8:20]
            self.seen += 1
            if self.seen <= self.drop_first:
                continue
            if self.report is not None:
                ip, port = self.report
            else:
                ip, port = src[0], src[1]
            cookie_v = 0x2112A442
            xport = port ^ (cookie_v >> 16)
            try:
                xip = struct.unpack(">I", socket.inet_aton(ip))[0] ^ cookie_v
            except OSError:
                continue
            attr = struct.pack(">HH", 0x0020, 8) + struct.pack(
                ">BBHI", 0, 1, xport, xip)
            resp = (struct.pack(">HHI", 0x0101, len(attr), cookie_v)
                    + txid + attr)
            try:
                self._sock.sendto(resp, src)
            except OSError:
                return

    def close(self):
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    import unittest

    class StunTest(unittest.TestCase):
        def test_request_format(self):
            pkt, txid = bs.stun_request()
            self.assertEqual(len(pkt), 20)
            self.assertEqual(len(txid), 12)
            mtype, mlen, cookie = struct.unpack(">HHI", pkt[:8])
            self.assertEqual(mtype, 0x0001)
            self.assertEqual(mlen, 0)
            self.assertEqual(cookie, 0x2112A442)
            self.assertEqual(pkt[8:20], txid)
            pkt2, txid2 = bs.stun_request()
            self.assertNotEqual(txid, txid2)

        def test_rfc5769_ipv4_vector(self):
            self.assertEqual(bs.parse_stun_response(RFC5769_V4,
                                                    RFC5769_TXID),
                             ("192.0.2.1", 32853))

        def test_rfc5769_ipv6_skipped(self):
            self.assertIsNone(bs.parse_stun_response(RFC5769_V6,
                                                     RFC5769_TXID))

        def test_mapped_address_fallback(self):
            txid = b"0123456789ab"
            attr = (struct.pack(">HH", 0x0001, 8)
                    + struct.pack(">BBH4s", 0, 1, 4567,
                                  socket.inet_aton("198.51.100.9")))
            pkt = (struct.pack(">HHI", 0x0101, len(attr), 0x2112A442)
                   + txid + attr)
            self.assertEqual(bs.parse_stun_response(pkt, txid),
                             ("198.51.100.9", 4567))

        def test_bad_packets_ignored(self):
            txid = b"0123456789ab"
            good = _mapped_response(txid)
            self.assertIsNotNone(bs.parse_stun_response(good, txid))
            self.assertIsNone(bs.parse_stun_response(b"short", txid))
            self.assertIsNone(bs.parse_stun_response(good, b"wrong-txid!"))
            bad_cookie = (struct.pack(">HHI", 0x0101, 12, 0xDEADBEEF)
                          + txid + good[20:])
            self.assertIsNone(bs.parse_stun_response(bad_cookie, txid))
            bad_type = (struct.pack(">HHI", 0x0111, 12, 0x2112A442)
                        + txid + good[20:])
            self.assertIsNone(bs.parse_stun_response(bad_type, txid))

        def test_query_retries_then_succeeds(self):
            srv = FakeStun(drop_first=2)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                res = bs.stun_query(sock, srv.addr, timeout=0.2, retries=2)
                self.assertIsNotNone(res)
                self.assertEqual(res[0], "127.0.0.1")
                self.assertGreaterEqual(srv.seen, 3)
            finally:
                sock.close()
                srv.close()

        def test_query_gives_up_and_rotates(self):
            dead = FakeStun(drop_first=10**9)
            live = FakeStun()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.assertIsNone(bs.stun_query(sock, dead.addr, timeout=0.1,
                                                retries=1))
                res = bs.stun_query(sock, live.addr, timeout=0.5, retries=1)
                self.assertIsNotNone(res)
            finally:
                sock.close()
                dead.close()
                live.close()

        def test_same_endpoint_is_punchable(self):
            a = FakeStun()
            b = FakeStun()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                mapped, punchable, _reason = bs.stun_check(
                    sock, [a.addr, b.addr], timeout=0.5, retries=1)
                self.assertTrue(punchable)
                self.assertIsNotNone(mapped)
            finally:
                sock.close()
                a.close()
                b.close()

        def test_different_ports_are_symmetric(self):
            a = FakeStun()
            b = FakeStun(report=("127.0.0.1", 59999))
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                mapped, punchable, reason = bs.stun_check(
                    sock, [a.addr, b.addr], timeout=0.5, retries=1)
                self.assertFalse(punchable)
                self.assertIn("symmetric", reason.lower())
                self.assertIsNotNone(mapped)
            finally:
                sock.close()
                a.close()
                b.close()

        def test_no_server_means_not_punchable(self):
            dead = FakeStun(drop_first=10**9)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                mapped, punchable, _reason = bs.stun_check(
                    sock, [dead.addr], timeout=0.1, retries=0)
                self.assertFalse(punchable)
                self.assertIsNone(mapped)
            finally:
                sock.close()
                dead.close()

        def test_parse_server_forms(self):
            self.assertEqual(bs.stun_parse_server("stun.example.com:19302"),
                             ("stun.example.com", 19302))
            host, port = bs.stun_parse_server("stun.example.com")
            self.assertEqual(host, "stun.example.com")
            self.assertEqual(port, bs.STUN_DEFAULT_PORT)
            with self.assertRaises(ValueError):
                bs.stun_parse_server("")

    unittest.main(verbosity=2)
