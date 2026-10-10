"""T9: RUDP and UDP transport (spec 8.4). Loopback only; lossy proxy in-test."""
import heapq
import random
import socket
import sys
import threading
import time

sys.path.insert(0, ".")

import battleships as bs


def make_pair():
    ka, kb = bs.new_secret(), bs.new_secret()
    sa = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sa.bind(("127.0.0.1", 0))
    sb.bind(("127.0.0.1", 0))
    pa, pb = sa.getsockname(), sb.getsockname()
    ta = bs.UdpTransport(sa, pb, ka, kb)
    tb = bs.UdpTransport(sb, pa, kb, ka)
    return ta, tb


class LossyProxy:
    """Bidirectional UDP relay with drop/dup/reorder/delay knobs."""

    def __init__(self, drop=0.0, dup=0.0, reorder=0.0, delay=0.0, seed=1):
        self.rng = random.Random(seed)
        self.drop, self.dup, self.reorder, self.delay = drop, dup, reorder, delay
        self.sa = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sa.bind(("127.0.0.1", 0))
        self.sb.bind(("127.0.0.1", 0))
        self.port_a = self.sa.getsockname()[1]
        self.port_b = self.sb.getsockname()[1]
        self.target_a = None
        self.target_b = None
        self._heap = []
        self._cond = threading.Condition()
        self._closed = False
        self.forwarded = 0
        for sock, other in ((self.sa, "a"), (self.sb, "b")):
            threading.Thread(target=self._recv_loop, args=(sock, other),
                             daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

    def _recv_loop(self, sock, which):
        sock.settimeout(0.1)
        while not self._closed:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if self.rng.random() < self.drop:
                continue
            target = self.target_b if which == "b" else self.target_a
            if target is None:
                continue
            due = time.monotonic() + self.delay
            if self.rng.random() < self.reorder:
                due += self.rng.random() * 0.15
            with self._cond:
                heapq.heappush(self._heap, (due, target, data))
                if self.rng.random() < self.dup:
                    heapq.heappush(
                        self._heap,
                        (due + self.rng.random() * 0.05, target, data))
                self._cond.notify()

    def _send_loop(self):
        while not self._closed:
            with self._cond:
                while not self._heap and not self._closed:
                    self._cond.wait(0.05)
                if self._closed:
                    return
                due, target, data = self._heap[0]
                wait = due - time.monotonic()
                if wait > 0:
                    self._cond.wait(min(wait, 0.05))
                    continue
                heapq.heappop(self._heap)
            try:
                # Emit from the destination's configured peer port, so the
                # source address matches and no migration triggers.
                (self.sb if target == self.target_a else self.sa).sendto(
                    data, target)
                self.forwarded += 1
            except OSError:
                pass

    def close(self):
        self._closed = True
        with self._cond:
            self._cond.notify_all()
        for s in (self.sa, self.sb):
            try:
                s.close()
            except OSError:
                pass


def proxy_pair(**knobs):
    proxy = LossyProxy(**knobs)
    ka, kb = bs.new_secret(), bs.new_secret()
    sa = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sa.bind(("127.0.0.1", 0))
    sb.bind(("127.0.0.1", 0))
    proxy.target_a = sa.getsockname()
    proxy.target_b = sb.getsockname()
    ta = bs.UdpTransport(sa, ("127.0.0.1", proxy.port_b), ka, kb)
    tb = bs.UdpTransport(sb, ("127.0.0.1", proxy.port_a), kb, ka)
    return proxy, ta, tb


if __name__ == "__main__":
    import unittest

    class RudpTest(unittest.TestCase):
        def test_loopback_order_exactly_once(self):
            ta, tb = make_pair()
            try:
                n = 100
                for i in range(n):
                    ta.send(("msg-%04d" % i).encode())
                got = [tb.recv(timeout=5) for _ in range(n)]
                self.assertEqual(got, [("msg-%04d" % i).encode()
                                       for i in range(n)])
            finally:
                ta.close()
                tb.close()

        def test_lossy_proxy_1000_mixed_sizes(self):
            proxy, ta, tb = proxy_pair(drop=0.2, dup=0.05, reorder=0.2,
                                       delay=0.2, seed=7)
            try:
                msgs = []
                for i in range(1000):
                    if i % 50 == 0:
                        msgs.append(bytes((i + j) & 0xFF for j in range(50000)))
                    else:
                        msgs.append(("p-%04d" % i).encode())
                for m in msgs:
                    ta.send(m)
                got = [tb.recv(timeout=30) for _ in range(1000)]
                self.assertEqual(got, msgs)
            finally:
                ta.close()
                tb.close()
                proxy.close()

        def test_unauthenticated_dropped_no_reply(self):
            ta, tb = make_pair()
            raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            raw.bind(("127.0.0.1", 0))
            raw.settimeout(0.5)
            try:
                a_addr = ta._sock.getsockname()
                raw.sendto(b"\xb5garbage-no-mac-at-all", a_addr)
                raw.sendto(bs.new_secret() * 40, a_addr)
                with self.assertRaises(socket.timeout):
                    raw.recvfrom(65535)
            finally:
                raw.close()
                ta.close()
                tb.close()

        def test_too_many_fragments_rejected(self):
            ta, tb = make_pair()
            try:
                with self.assertRaises(bs.ProtocolError):
                    ta.send(b"x" * (bs.RUDP_MAX_FRAGMENTS
                                    * bs.RUDP_MAX_PAYLOAD + 1))
            finally:
                ta.close()
                tb.close()

        def test_dead_after_total_loss(self):
            old = bs.RUDP_DEAD_AFTER_S
            bs.RUDP_DEAD_AFTER_S = 0.5
            blackhole = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            blackhole.bind(("127.0.0.1", 0))
            dead_port = blackhole.getsockname()[1]
            blackhole.close()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            k = bs.new_secret()
            t = bs.UdpTransport(s, ("127.0.0.1", dead_port), k, k)
            try:
                t.send(b"hello?")
                with self.assertRaises(bs.TransportClosed):
                    t.recv(timeout=10)
            finally:
                bs.RUDP_DEAD_AFTER_S = old
                t.close()

        def test_window_cap(self):
            blackhole = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            blackhole.bind(("127.0.0.1", 0))
            dead_port = blackhole.getsockname()[1]
            blackhole.close()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            k = bs.new_secret()
            t = bs.UdpTransport(s, ("127.0.0.1", dead_port), k, k)
            try:
                for i in range(100):
                    t.send(("w-%d" % i).encode())
                time.sleep(1.0)
                self.assertLessEqual(len(t._unacked), bs.RUDP_WINDOW)
                self.assertGreater(len(t._unacked), 0)
            finally:
                t.close()

        def test_session_smoke_over_udp(self):
            ta, tb = make_pair()
            sa = bs.Session(ta, session_id="rudp-smoke")
            sb = bs.Session(tb, session_id="rudp-smoke")
            try:
                for i in range(50):
                    sa.send_game({"n": i})
                    self.assertEqual(sb.recv_game(timeout=5), {"n": i})
                    sb.send_game({"ack": i})
                    self.assertEqual(sa.recv_game(timeout=5), {"ack": i})
            finally:
                sa.close()
                sb.close()

    unittest.main(verbosity=2)
