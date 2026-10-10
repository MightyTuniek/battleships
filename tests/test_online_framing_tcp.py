"""T3: framing codec + TCP transport over loopback."""

import os
import random
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, ".")

import battleships as bs


def make_pair():
    a, b = socket.socketpair()
    k1, k2 = os.urandom(32), os.urandom(32)
    ta = bs.TcpTransport(a, k1, k2)
    tb = bs.TcpTransport(b, k2, k1)
    return ta, tb


class FramingTest(unittest.TestCase):
    def test_seal_open_roundtrip(self):
        key = os.urandom(32)
        for ctr in (0, 1, 2**64 - 1):
            body = os.urandom(100)
            self.assertEqual(bs.open_frame(key, ctr, bs.seal(key, ctr, body)),
                             body)

    def test_tamper_counter_truncation_rejected(self):
        key = os.urandom(32)
        pkt = bytearray(bs.seal(key, 7, b"hello world"))
        pkt[6] ^= 0xFF
        with self.assertRaises(bs.ProtocolError):
            bs.open_frame(key, 7, bytes(pkt))
        with self.assertRaises(bs.ProtocolError):
            bs.open_frame(key, 8, bs.seal(key, 7, b"hello"))
        with self.assertRaises(bs.ProtocolError):
            bs.open_frame(key, 7, bs.seal(key, 7, b"hello")[:-1])

    def test_length_cap_before_alloc(self):
        key = os.urandom(32)
        bad = struct.pack(">I", bs.MAX_FRAME_BYTES + 1) + b"x" * 4
        with self.assertRaises(bs.ProtocolError):
            bs.open_frame(key, 0, bad)
        with self.assertRaises(bs.ProtocolError):
            bs.seal(key, 0, b"x" * (bs.MAX_FRAME_BYTES + 1))

    def test_fuzz_never_hangs(self):
        rng = random.Random(7)
        dec = bs.FrameDecoder(os.urandom(32))
        for i in range(10000):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(71)))
            try:
                dec.feed(data)
            except bs.ProtocolError:
                dec = bs.FrameDecoder(os.urandom(32))
            except Exception as exc:
                self.fail("unexpected %r" % exc)
            if i % 500 == 499:
                dec = bs.FrameDecoder(os.urandom(32))


class TcpTransportTest(unittest.TestCase):
    def test_loopback_1000_frames_in_order(self):
        ta, tb = make_pair()
        try:
            for i in range(1000):
                ta.send(b"frame-%04d" % i)
            for i in range(1000):
                self.assertEqual(tb.recv(timeout=10), b"frame-%04d" % i)
        finally:
            ta.close()
            tb.close()

    def test_concurrent_senders_no_interleave(self):
        ta, tb = make_pair()
        try:
            n_threads, per = 50, 20
            errors = []

            def worker(tid):
                try:
                    for i in range(per):
                        ta.send(b"%02d:%04d" % (tid, i))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(t,)) for t in
                       range(n_threads)]
            for t in threads:
                t.start()
            got = [tb.recv(timeout=15) for _ in range(n_threads * per)]
            for t in threads:
                t.join()
            self.assertFalse(errors)
            self.assertEqual(len(got), n_threads * per)
            self.assertEqual(len(set(got)), n_threads * per)
            by_sender = {}
            for frame in got:
                tid, i = frame.split(b":")
                by_sender.setdefault(tid, []).append(int(i))
            for tid, seqs in by_sender.items():
                self.assertEqual(seqs, sorted(seqs))
                self.assertEqual(len(seqs), per)
        finally:
            ta.close()
            tb.close()

    def test_close_raises(self):
        ta, tb = make_pair()
        ta.close()
        tb.close()
        with self.assertRaises(bs.TransportClosed):
            tb.recv(timeout=2)


if __name__ == "__main__":
    unittest.main()
