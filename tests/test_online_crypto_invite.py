"""T2: constants, errors, crypto vectors, invite codes, stdlib-only."""

import ast
import base64
import random
import struct
import sys
import unittest
import zlib

sys.path.insert(0, ".")

import battleships as bs


class ConstantsTest(unittest.TestCase):
    def test_exact_values(self):
        self.assertEqual(bs.PROTOCOL_VERSION, 1)
        self.assertEqual(bs.SECRET_BYTES, 10)
        self.assertEqual(bs.MAC_BYTES, 16)
        self.assertEqual(bs.NONCE_BYTES_TCP, 16)
        self.assertEqual(bs.NONCE_BYTES_UDP, 8)
        self.assertEqual(bs.MAX_HANDSHAKE_BYTES, 1024)
        self.assertEqual(bs.MAX_FRAME_BYTES, 65536)
        self.assertEqual(bs.HANDSHAKE_TIMEOUT_S, 10)
        self.assertEqual(bs.FRAME_READ_TIMEOUT_S, 10)
        self.assertEqual(bs.HANDSHAKE_FAIL_LIMIT, 3)
        self.assertEqual(bs.FAIL_DELAY_RANGE_S, (0.2, 0.5))
        self.assertEqual(bs.PING_INTERVAL_S, 5)
        self.assertEqual(bs.DEAD_AFTER_S, 20)
        self.assertEqual(bs.RESUME_WINDOW_S, 120)
        self.assertEqual(bs.OUTBOUND_BUFFER_MAX, 1000)
        self.assertEqual(bs.STUN_TIMEOUT_S, 1.5)
        self.assertEqual(bs.STUN_RETRIES, 2)
        self.assertTrue(3 <= len(bs.DEFAULT_STUN_SERVERS) <= 4)
        self.assertEqual(bs.PUNCH_INTERVAL_S, 0.25)
        self.assertEqual(bs.PUNCH_WINDOW_S, 15)
        self.assertEqual(bs.ADDR_MIGRATE_MIN_S, 5)
        self.assertEqual(bs.RUDP_MAGIC, 0xB5)
        self.assertEqual(bs.RUDP_MAX_PAYLOAD, 1100)
        self.assertEqual(bs.RUDP_MAX_FRAGMENTS, 64)
        self.assertEqual(bs.RUDP_WINDOW, 16)
        self.assertEqual(bs.RUDP_REORDER_BUFFER, 32)
        self.assertEqual(bs.RUDP_ACK_DELAY_S, 0.02)
        self.assertEqual(bs.RUDP_RTO_INITIAL_S, 0.4)
        self.assertEqual(bs.RUDP_RTO_MAX_S, 4)
        self.assertEqual(bs.RUDP_DEAD_AFTER_S, 30)
        self.assertEqual(bs.SSDP_ADDR, ("239.255.255.250", 1900))
        self.assertEqual(bs.SSDP_WAIT_S, 2)
        self.assertEqual(bs.UPNP_LEASE_S, 3600)
        self.assertEqual(bs.NAME_MAX_CHARS, 20)
        self.assertEqual(bs.CHAT_MAX_CHARS, 200)
        self.assertEqual(bs.CHAT_RATE_PER_S, 5)


class ErrorsTest(unittest.TestCase):
    def test_reasons(self):
        self.assertEqual(bs.HandshakeFailed("x").reason, "BAD_CODE")
        self.assertEqual(bs.BadInvite("x").reason, "BAD_CODE")
        self.assertEqual(bs.RulesMismatch("x").reason, "RULES_MISMATCH")
        self.assertEqual(bs.VersionMismatch("x").reason, "VERSION")
        self.assertEqual(bs.TransportClosed("x").reason, "LINK_LOST")
        self.assertEqual(bs.SessionLost("x").reason, "LINK_LOST")
        self.assertEqual(bs.ProtocolError("x").reason, "PROTOCOL_ERROR")
        for r in ("TIMEOUT", "BAD_CODE", "VERSION", "RULES_MISMATCH",
                  "PEER_LEFT", "LINK_LOST", "PROTOCOL_ERROR"):
            self.assertTrue(bs.message_for(r))


class CryptoTest(unittest.TestCase):
    def test_mac16_known_answer(self):
        tag = bs.mac16(b"key", b"The quick brown fox jumps over the lazy dog")
        self.assertEqual(tag.hex(), "f7bc83f430538424b13298e6aa6fb143")
        self.assertTrue(bs.verify_mac16(b"key", b"The quick brown fox jumps over the lazy dog", tag))
        self.assertFalse(bs.verify_mac16(b"key", b"tampered", tag))

    def test_hkdf_rfc5869_tc1(self):
        ikm = bytes([0x0B] * 22)
        salt = bytes(range(13))
        info = bytes([0xF0 + i for i in range(10)])
        self.assertEqual(
            bs.hkdf(ikm, salt, info).hex(),
            "3cb25f25faacd57a90434f64d0362f2a"
            "2d2d0a90cf1a5a4c5db02d56ecc4c5bf")

    def test_derive_keys_split(self):
        k_hg, k_gh, k_r = bs.derive_keys(b"S" * 10, b"H" * 16, b"G" * 16)
        self.assertEqual(len(k_hg), 32)
        self.assertNotEqual(k_hg, k_gh)
        self.assertNotEqual(k_hg, k_r)


class InviteTest(unittest.TestCase):
    def test_roundtrip(self):
        rng = random.Random(42)
        for _ in range(25):
            ip = "192.168.%d.%d" % (rng.randrange(256), rng.randrange(1, 255))
            port = rng.randrange(1, 65536)
            secret = bytes(rng.randrange(256) for _ in range(10))
            code = bs.encode_invite(ip, port, secret)
            self.assertEqual(len(code.replace("-", "")), 32)
            ver, flags, dip, dport, dsec = bs.decode_invite(code)
            self.assertEqual((ver, flags, dip, dport, dsec),
                             (1, 0, ip, port, secret))

    def test_normalization(self):
        code = bs.encode_invite("10.0.0.5", 9999, b"0123456789")
        variants = [code.lower(), code.replace("-", " "),
                    code.replace("-", ""),
                    code.replace("O", "0").replace("I", "1").replace("B", "8")]
        ref = bs.decode_invite(code)
        for v in variants:
            self.assertEqual(bs.decode_invite(v), ref)

    def test_one_flipped_char_rejected(self):
        code = bs.encode_invite("10.0.0.5", 9999, b"0123456789").replace("-", "")
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        for i in range(len(code)):
            for ch in alphabet:
                if ch == code[i]:
                    continue
                bad = code[:i] + ch + code[i + 1:]
                try:
                    bs.decode_invite(bad)
                except (bs.BadInvite, bs.VersionMismatch):
                    return
        self.fail("no single-char flip was rejected")

    def test_bad_inputs(self):
        with self.assertRaises(bs.BadInvite):
            bs.encode_invite("999.1.1.1", 80, b"0123456789")
        with self.assertRaises(bs.BadInvite):
            bs.encode_invite("127.0.0.1", 80, b"short")
        with self.assertRaises(bs.BadInvite):
            bs.decode_invite("!!!not-a-code!!!")
        # wrong protocol version -> VersionMismatch
        body = struct.pack(">BB4sH10s", 2, 0, bytes([1, 2, 3, 4]), 80,
                           b"0123456789")
        raw = body + struct.pack(">H", zlib.crc32(body) & 0xFFFF)
        with self.assertRaises(bs.VersionMismatch):
            bs.decode_invite(base64.b32encode(raw).decode())

    def test_secret_line(self):
        s = b"0123456789"
        self.assertEqual(bs.decode_secret_line(bs.encode_secret_line(s)), s)
        with self.assertRaises(bs.BadInvite):
            bs.decode_secret_line("ABC")


class StdlibOnlyTest(unittest.TestCase):
    def test_online_section_imports_stdlib_only(self):
        try:
            stdlib = set(sys.stdlib_module_names)
        except AttributeError:
            stdlib = set(("argparse math os random re sys socket threading json "
                           "time hashlib hmac queue secrets struct base64 zlib "
                           "ipaddress collections dataclasses typing readline "
                           "statistics ctypes".split()) | {"__future__"})
        with open("battleships.py", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    roots.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] not in ("",):
                    roots.add(node.module.split(".")[0])
        third = {r for r in roots if r not in stdlib}
        # Only the pre-existing optional UI theme lib may be third-party.
        self.assertTrue(third <= {"rich"}, "non-stdlib imports: %s" % third)


if __name__ == "__main__":
    unittest.main()
