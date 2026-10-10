"""T5: session layer — heartbeat, resume, buffer, bye, randomized kill."""

import queue
import random
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, ".")

import battleships as bs


# Fix LoopTransport: use explicit linked inbox (property conflict avoided).
class MemTransport:
    def __init__(self):
        self.inbox = queue.Queue()
        self.linked = None
        self.closed = False
        self.peer = ("mem", 0)

    def link(self, other):
        self.linked = other

    def send(self, frame):
        if self.closed or self.linked is None:
            raise bs.TransportClosed("closed")
        self.linked.inbox.put(frame)

    def recv(self, timeout=None):
        try:
            frame = self.inbox.get(timeout=timeout)
        except Exception:
            raise bs.TransportClosed("closed")
        if frame is None:
            raise bs.TransportClosed("closed")
        return frame

    def close(self):
        self.closed = True
        try:
            self.inbox.put(None)
        except Exception:
            pass


def mem_pair():
    a, b = MemTransport(), MemTransport()
    a.link(b)
    b.link(a)
    return a, b


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class SessionTest(unittest.TestCase):
    def test_dead_goes_reconnecting_then_lost(self):
        ta, tb = mem_pair()
        states = []
        clk = FakeClock()
        s = bs.Session(ta, session_id="s1", clock=clk, on_state=states.append)
        try:
            self.assertEqual(s.state, "CONNECTED")
            clk.advance(bs.DEAD_AFTER_S + 1)
            s.tick()
            self.assertEqual(s.state, "RECONNECTING")
            self.assertIn("RECONNECTING", states)
            clk.advance(bs.RESUME_WINDOW_S + 1)
            s.tick()
            self.assertEqual(s.state, "LOST")
            with self.assertRaises(bs.SessionLost):
                s.recv_game(timeout=1)
        finally:
            s.close()

    def test_resume_retransmits_only_unacked_no_duplicates(self):
        ta, tb = mem_pair()
        clk = FakeClock()
        host = bs.Session(ta, session_id="s", clock=clk)
        guest = bs.Session(tb, session_id="s", clock=clk)
        try:
            for i in range(1, 16):
                host.send_game({"n": i})
            # kill instantly; whatever arrived stays, the rest is unacked
            k = guest.export_resume()["last_recv"]
            na, nb = mem_pair()
            host.attach(na)
            guest.attach(nb)
            host.retransmit_from(k)
            got = [guest.recv_game(timeout=5)["n"] for _ in range(15 - k)]
            self.assertEqual(got, list(range(k + 1, 16)))
            # duplicate delivery of an old envelope is dropped
            dup = bs.make_envelope("game", 1, 0, {"n": -1})
            guest._handle_env(dup)
            with self.assertRaises(bs.SessionLost):
                guest.recv_game(timeout=0.3)
            # once the guest acks everything, host buffer drains
            guest.send_game({"t": "ack"})
            self.assertEqual(host.recv_game(timeout=5), {"t": "ack"})
            self.assertEqual(host.unacked(), [])
        finally:
            host.close()
            guest.close()

    def test_resume_wrong_key_or_session_refused(self):
        old = bs.HANDSHAKE_TIMEOUT_S
        bs.HANDSHAKE_TIMEOUT_S = 0.5
        try:
            self._resume_refused(key_a=b"wrong-key-0123456789abcdef00",
                                 key_b=b"right-key-0123456789abcdef00",
                                 sess_a="sess", sess_b="sess")
            k = b"0123456789abcdef0123456789abcdef"[:32]
            self._resume_refused(key_a=k, key_b=k,
                                 sess_a="sess-A", sess_b="sess-B")
        finally:
            bs.HANDSHAKE_TIMEOUT_S = old

    def _resume_refused(self, key_a, key_b, sess_a, sess_b):
        a, b = socket.socketpair()
        try:
            box = {}

            def run_host():
                try:
                    box["r"] = bs.resume_host_side(a, key_a, sess_a, 0)
                except Exception as exc:
                    box["e"] = exc

            t = threading.Thread(target=run_host, daemon=True)
            t.start()
            with self.assertRaises(bs.HandshakeFailed):
                bs.resume_guest_side(b, key_b, sess_b, 0)
            t.join(8)
            self.assertIsInstance(box.get("e"), bs.HandshakeFailed)
        finally:
            a.close()
            b.close()

    def test_buffer_overflow(self):
        old = bs.OUTBOUND_BUFFER_MAX
        bs.OUTBOUND_BUFFER_MAX = 5
        ta, tb = mem_pair()
        s = bs.Session(ta, session_id="s")
        try:
            for i in range(5):
                s.send_game({"n": i})
            with self.assertRaises(bs.SessionLost):
                s.send_game({"overflow": True})
        finally:
            bs.OUTBOUND_BUFFER_MAX = old
            s.close()

    def test_bye_means_peer_left(self):
        ta, tb = mem_pair()
        a = bs.Session(ta, session_id="s")
        b = bs.Session(tb, session_id="s")
        try:
            a.send_bye()
            with self.assertRaises(bs.SessionLost) as ctx:
                b.recv_game(timeout=5)
            self.assertEqual(ctx.exception.reason, "PEER_LEFT")
        finally:
            a.close()
            b.close()

    def test_randomized_kill_resume_exactly_once(self):
        rng = random.Random(20261010)
        runs = 60
        for run in range(runs):
            kill_at = rng.randrange(1, 50)
            secret = bs.new_secret()
            sa, sb = socket.socketpair()
            ho, go = {}, {}

            def rh():
                try:
                    ho["i"] = bs.host_handshake(sa, secret, "H", "rules")
                except Exception as exc:
                    ho["e"] = exc

            def rg():
                try:
                    go["i"] = bs.guest_handshake(sb, secret, "G", "rules")
                except Exception as exc:
                    go["e"] = exc

            th = threading.Thread(target=rh)
            tg = threading.Thread(target=rg)
            th.start()
            tg.start()
            th.join(10)
            tg.join(10)
            self.assertIn("i", ho, "run %d host: %r" % (run, ho.get("e")))
            self.assertIn("i", go, "run %d guest: %r" % (run, go.get("e")))
            k_resume = ho["i"]["k_resume"]
            sess = ho["i"]["session_id"]
            ta = bs.TcpTransport(sa, ho["i"]["send_key"], ho["i"]["recv_key"])
            tb = bs.TcpTransport(sb, go["i"]["send_key"], go["i"]["recv_key"])
            hclk, gclk = FakeClock(), FakeClock()
            hs = bs.Session(ta, session_id=sess, clock=hclk)
            gs = bs.Session(tb, session_id=sess, clock=gclk)
            try:
                for i in range(kill_at):
                    hs.send_game({"n": i})
                for i in range(kill_at):
                    self.assertEqual(gs.recv_game(timeout=10), {"n": i},
                                     "run %d pre-kill" % run)
                for i in range(kill_at, 50):
                    hs.send_game({"n": i})
                # kill the TCP link abruptly
                ta.close()
                tb.close()
                time.sleep(0.1)
                # reconnect + resume handshake over fresh sockets
                ra, rb = socket.socketpair()
                ro = {}

                def rr_h():
                    try:
                        ro["h"] = bs.resume_host_side(
                            ra, k_resume, sess, hs.export_resume()["last_recv"])
                    except Exception as exc:
                        ro["he"] = exc

                def rr_g():
                    try:
                        ro["g"] = bs.resume_guest_side(
                            rb, k_resume, sess, gs.export_resume()["last_recv"])
                    except Exception as exc:
                        ro["ge"] = exc

                th2 = threading.Thread(target=rr_h)
                tg2 = threading.Thread(target=rr_g)
                th2.start()
                tg2.start()
                th2.join(10)
                tg2.join(10)
                self.assertIn("h", ro, "run %d resume host: %r"
                              % (run, ro.get("he")))
                self.assertIn("g", ro, "run %d resume guest: %r"
                              % (run, ro.get("ge")))
                na = bs.TcpTransport(ra, ro["h"]["send_key"],
                                     ro["h"]["recv_key"])
                nb = bs.TcpTransport(rb, ro["g"]["send_key"],
                                     ro["g"]["recv_key"])
                hs.attach(na)
                gs.attach(nb)
                hs.retransmit_from(ro["h"]["guest_last_recv"])
                gs.retransmit_from(ro["g"]["host_last_recv"])
                # guest must now hold kill_at..49 exactly once in order
                rest = [gs.recv_game(timeout=10) for _ in range(50 - kill_at)]
                self.assertEqual([m["n"] for m in rest],
                                 list(range(kill_at, 50)),
                                 "run %d post-resume" % run)
            finally:
                hs.close()
                gs.close()


if __name__ == "__main__":
    unittest.main()
