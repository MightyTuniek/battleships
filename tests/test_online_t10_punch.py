"""T10: hole punch procedure (spec 8.3). Fake sockets/relays; loopback UDP."""
import collections
import socket
import sys
import threading
import time

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import battleships as bs


class Relay:
    """NAT test double. Full-cone: stable public port per host, inbound
    admitted only from endpoints the host already sent to. Symmetric:
    the public port rotates on every send, so published (stale) ports
    never deliver."""

    def __init__(self, symmetric=False):
        self.symmetric = symmetric
        self._lock = threading.Lock()
        self._pub = {}
        self._next_port = 40000
        self._inbox = collections.defaultdict(collections.deque)
        self._sent_to = collections.defaultdict(set)
        self._rev = {}

    def register(self, name):
        with self._lock:
            if name not in self._pub:
                self._pub[name] = ("10.0.0.1", self._next_port)
                self._next_port += 1
            return self._pub[name]

    def _rotate(self, name):
        self._pub[name] = ("10.0.0.1", self._next_port)
        self._next_port += 1

    def send(self, name, data, dest):
        with self._lock:
            src = self._pub[name]
            if self.symmetric:
                self._rotate(name)
                src = self._pub[name]
            self._sent_to[name].add(dest)
            owner = self._rev.get(dest)
            if owner is None or owner == name:
                return
            if dest != self._pub[owner]:
                return
            if src not in self._sent_to[owner]:
                return
            self._inbox[owner].append((data, src))

    def recv(self, name, timeout):
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._inbox[name]:
                    return self._inbox[name].popleft()
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("timed out")
            time.sleep(min(left, 0.01))

    def publish(self, name):
        # What STUN would have reported (stale under symmetric after sends).
        return self.register(name)


class FakeSock:
    """socket-like handle routed through a Relay."""

    def __init__(self, relay, name):
        self._relay = relay
        self._name = name
        self._timeout = 1.0
        relay._rev[relay.register(name)] = name

    def sendto(self, data, dest):
        self._relay.send(self._name, data, dest)
        return len(data)

    def recvfrom(self, _n):
        return self._relay.recv(self._name, self._timeout)

    def settimeout(self, t):
        self._timeout = t

    def gettimeout(self):
        return self._timeout

    def close(self):
        pass


def _keys(nonce_h=b"HOSTNONC", nonce_g=b"GUESTNON"):
    secret = bs.new_secret()
    k_hg, k_gh, k_r = bs.punch_keys(secret, nonce_h, nonce_g)
    return secret, k_hg, k_gh, k_r


if __name__ == "__main__":
    import unittest

    class PunchTest(unittest.TestCase):
        def test_connect_behind_cone_nat(self):
            relay = Relay()
            hs = FakeSock(relay, "host")
            gs = FakeSock(relay, "guest")
            _s, k_hg, k_gh, _r = _keys()
            host_pub = relay.publish("host")
            guest_pub = relay.publish("guest")
            box = {}

            def run_host():
                try:
                    box["r"] = bs.punch_connect(
                        hs, [guest_pub], k_hg, k_gh, b"HOSTNONC",
                        peer_nonce=b"GUESTNON", window=5, interval=0.05)
                except Exception as exc:
                    box["e"] = exc

            t = threading.Thread(target=run_host, daemon=True)
            t.start()
            res_g = bs.punch_connect(gs, [host_pub], k_gh, k_hg, b"GUESTNON",
                                     peer_nonce=b"HOSTNONC", window=5,
                                     interval=0.05)
            t.join(10)
            self.assertIn("r", box)
            addr_h, _info_h = box["r"]
            addr_g, _info_g = res_g
            # Locked onto the relay's public endpoints, not loopback.
            self.assertEqual(addr_h[0], "10.0.0.1")
            self.assertEqual(addr_g[0], "10.0.0.1")

        def test_late_starter_still_connects(self):
            relay = Relay()
            hs = FakeSock(relay, "host")
            gs = FakeSock(relay, "guest")
            _s, k_hg, k_gh, _r = _keys()
            host_pub = relay.publish("host")
            guest_pub = relay.publish("guest")
            box = {}

            def run_host():
                try:
                    box["r"] = bs.punch_connect(
                        hs, [guest_pub], k_hg, k_gh, b"HOSTNONC",
                        window=6, interval=0.05)
                except Exception as exc:
                    box["e"] = exc

            t = threading.Thread(target=run_host, daemon=True)
            t.start()
            time.sleep(1.0)
            res_g = bs.punch_connect(gs, [host_pub], k_gh, k_hg, b"GUESTNON",
                                     window=6, interval=0.05)
            t.join(10)
            self.assertIn("r", box)
            self.assertIsNotNone(res_g)

        def test_symmetric_nat_fails_clearly(self):
            relay = Relay(symmetric=True)
            hs = FakeSock(relay, "host")
            gs = FakeSock(relay, "guest")
            _s, k_hg, k_gh, _r = _keys()
            host_pub = relay.publish("host")
            guest_pub = relay.publish("guest")
            with self.assertRaises(bs.HandshakeFailed) as ctx:
                bs.punch_connect(hs, [guest_pub], k_hg, k_gh, b"HOSTNONC",
                                 window=0.6, interval=0.05)
            self.assertIn("symmetric", str(ctx.exception).lower()
                          + getattr(ctx.exception, "reason", "").lower()
                          + "punch")
            with self.assertRaises(bs.HandshakeFailed):
                bs.punch_connect(gs, [host_pub], k_gh, k_hg, b"GUESTNON",
                                 window=0.6, interval=0.05)

        def test_wrong_key_never_connects(self):
            relay = Relay()
            hs = FakeSock(relay, "host")
            gs = FakeSock(relay, "guest")
            _s, k_hg, k_gh, _r = _keys()
            _s2, k2_hg, k2_gh, _r2 = _keys(b"XXXXXXXX", b"YYYYYYYY")
            host_pub = relay.publish("host")
            guest_pub = relay.publish("guest")
            box = {}

            def run_host():
                try:
                    box["r"] = bs.punch_connect(
                        hs, [guest_pub], k_hg, k_gh, b"HOSTNONC",
                        window=1.0, interval=0.05)
                except Exception as exc:
                    box["e"] = exc

            t = threading.Thread(target=run_host, daemon=True)
            t.start()
            with self.assertRaises(bs.HandshakeFailed):
                bs.punch_connect(gs, [host_pub], k2_gh, k2_hg, b"GUESTNON",
                                 window=1.0, interval=0.05)
            t.join(5)
            self.assertIn("e", box)

        def test_migration_and_unauth_ignored(self):
            old = bs.ADDR_MIGRATE_MIN_S
            bs.ADDR_MIGRATE_MIN_S = 0.2
            ka, kb = bs.new_secret(), bs.new_secret()
            sa = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sa.bind(("127.0.0.1", 0))
            sb.bind(("127.0.0.1", 0))
            pa, pb = sa.getsockname(), sb.getsockname()
            ta = bs.UdpTransport(sa, pb, ka, kb)
            tb = bs.UdpTransport(sb, pa, kb, ka)
            moved = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            moved.bind(("127.0.0.1", 0))
            evil = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            evil.bind(("127.0.0.1", 0))
            evil.settimeout(0.5)
            try:
                ta.send(b"ping0")
                self.assertEqual(tb.recv(timeout=5), b"ping0")
                self.assertEqual(tb.peer, pa)
                # Let the ack settle: migrating with an ack in flight
                # orphans it and the retransmit would migrate straight back.
                deadline = time.monotonic() + 5
                while ta._unacked and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(ta._unacked, {})
                # Authenticated packet from a new address migrates peer.
                pkt = bs._rudp_packet(bs.RUDP_TYPE_ACK, 0, 0, 0, 1, b"", ka)
                moved.sendto(pkt, pb)
                deadline = time.monotonic() + 3
                while tb.peer != moved.getsockname() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(tb.peer, moved.getsockname())
                # Rate limit: a second move inside the window is ignored.
                pkt2 = bs._rudp_packet(bs.RUDP_TYPE_ACK, 0, 0, 0, 1, b"", ka)
                evil.sendto(pkt2, pb)
                time.sleep(0.4)
                self.assertEqual(tb.peer, moved.getsockname())
                # Unauthenticated datagrams get no reply ever.
                evil.sendto(b"\xb5junk", pb)
                with self.assertRaises(socket.timeout):
                    evil.recvfrom(65535)
            finally:
                bs.ADDR_MIGRATE_MIN_S = old
                moved.close()
                evil.close()
                ta.close()
                tb.close()

        def test_candidates_from_offer_answer(self):
            secret = bs.new_secret()
            ocode, _o = bs.encode_offer(("203.0.113.9", 45678), secret,
                                        lan=("192.168.1.5", 45678))
            acode, _a = bs.encode_answer(ocode, ("198.51.100.4", 51234),
                                         secret)
            guest_cands = bs.punch_candidates(bs.decode_offer(ocode))
            self.assertIn(("203.0.113.9", 45678), guest_cands)
            self.assertIn(("192.168.1.5", 45678), guest_cands)
            host_cands = bs.punch_candidates(bs.decode_answer(acode))
            self.assertEqual(host_cands, [("198.51.100.4", 51234)])

        def test_session_id_deterministic(self):
            secret = bs.new_secret()
            a = bs.punch_session_id(secret, b"HOSTNONC", b"GUESTNON")
            b = bs.punch_session_id(secret, b"HOSTNONC", b"GUESTNON")
            self.assertEqual(a, b)
            self.assertEqual(len(a), 16)

        def test_e2e_game_over_punched_udp_via_proxy(self):
            from test_online_t9_rudp import LossyProxy
            relay = Relay()
            hs = FakeSock(relay, "host")
            gs = FakeSock(relay, "guest")
            secret = bs.new_secret()
            nonce_h, nonce_g = b"HOSTNONC", b"GUESTNON"
            k_hg, k_gh, k_r = bs.punch_keys(secret, nonce_h, nonce_g)
            host_pub = relay.publish("host")
            guest_pub = relay.publish("guest")
            box = {}

            def run_host():
                try:
                    box["r"] = bs.punch_connect(
                        hs, [guest_pub], k_hg, k_gh, nonce_h,
                        peer_nonce=nonce_g, window=5, interval=0.05)
                except Exception as exc:
                    box["e"] = exc

            t = threading.Thread(target=run_host, daemon=True)
            t.start()
            addr_g, _ig = bs.punch_connect(
                gs, [host_pub], k_gh, k_hg, nonce_g, peer_nonce=nonce_h,
                window=5, interval=0.05)
            t.join(10)
            self.assertIn("r", box)
            # Real UDP sockets through a mild lossy proxy from here on.
            proxy = LossyProxy(drop=0.1, dup=0.05, reorder=0.1, delay=0.05,
                               seed=3)
            try:
                sa = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sa.bind(("127.0.0.1", 0))
                sb.bind(("127.0.0.1", 0))
                proxy.target_a = sa.getsockname()
                proxy.target_b = sb.getsockname()
                tha = bs.UdpTransport(
                    sa, ("127.0.0.1", proxy.port_b), k_hg, k_gh)
                thb = bs.UdpTransport(
                    sb, ("127.0.0.1", proxy.port_a), k_gh, k_hg)
                sid = bs.punch_session_id(secret, nonce_h, nonce_g)
                sha = bs.Session(tha, session_id=sid)
                shb = bs.Session(thb, session_id=sid)
                sha.k_resume = k_r
                shb.k_resume = k_r
                try:
                    import random as _random
                    board_g = bs.Board()
                    board_g.place_randomly(bs.FLEET, rng=_random.Random(9))
                    log = []
                    for pos in ([0, 0], [1, 1], [2, 2], [3, 3], [9, 9],
                                [5, 5]):
                        sha.send_game({"type": "shot", "pos": list(pos)})
                        m = shb.recv_game(timeout=30)
                        self.assertEqual(m["pos"], list(pos))
                        hit, ship, sunk = board_g.fire(tuple(pos))
                        shb.send_game({"type": "shot_result", "hit": hit,
                                       "sunk_len": len(
                                           board_g.ship_cells[ship])
                                       if sunk else 0})
                        r = sha.recv_game(timeout=30)
                        log.append({"pos": list(pos), "hit": r["hit"],
                                    "sunk_len": r["sunk_len"]})
                    reveal = bs.make_board_reveal(board_g, "salty")
                    self.assertTrue(bs.verify_shot_log(reveal, log))
                finally:
                    sha.close()
                    shb.close()
                    tha.close()
                    thb.close()
            finally:
                proxy.close()

    unittest.main(verbosity=2)
