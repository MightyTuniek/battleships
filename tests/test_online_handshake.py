"""T4: TCP handshake — success, failures, burning, one-peer rule."""

import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, ".")

import battleships as bs

RULES = "rules-hash-abc"
HOST, GUEST = "Host", "Guest"


def run_host_handshake(sock, secret, out, **kw):
    try:
        out["info"] = bs.host_handshake(sock, secret, HOST, RULES, **kw)
    except Exception as exc:
        out["exc"] = exc


def run_guest_handshake(sock, secret, out, rules=RULES, game_ver=1):
    try:
        out["info"] = bs.guest_handshake(sock, secret, GUEST, rules, game_ver)
    except Exception as exc:
        out["exc"] = exc


class HandshakeTest(unittest.TestCase):
    def test_success_same_session_and_keys(self):
        secret = bs.new_secret()
        a, b = socket.socketpair()
        ho, go = {}, {}
        th = threading.Thread(target=run_host_handshake, args=(a, secret, ho))
        tg = threading.Thread(target=run_guest_handshake, args=(b, secret, go))
        th.start()
        tg.start()
        th.join(10)
        tg.join(10)
        self.assertIn("info", ho)
        self.assertIn("info", go)
        self.assertEqual(ho["info"]["session_id"], go["info"]["session_id"])
        self.assertEqual(ho["info"]["send_key"], go["info"]["recv_key"])
        self.assertEqual(ho["info"]["recv_key"], go["info"]["send_key"])
        self.assertEqual(ho["info"]["k_resume"], go["info"]["k_resume"])
        self.assertEqual(go["info"]["peer_name"], HOST)
        self.assertEqual(ho["info"]["peer_name"], GUEST)
        a.close()
        b.close()

    def test_wrong_secret_both_fail(self):
        a, b = socket.socketpair()
        ho, go = {}, {}
        th = threading.Thread(target=run_host_handshake,
                              args=(a, bs.new_secret(), ho))
        tg = threading.Thread(target=run_guest_handshake,
                              args=(b, bs.new_secret(), go))
        th.start()
        tg.start()
        th.join(15)
        tg.join(15)
        self.assertIsInstance(ho.get("exc"), bs.HandshakeFailed)
        self.assertIsInstance(go.get("exc"), bs.HandshakeFailed)
        a.close()
        b.close()

    def test_impostor_host_rejected_at_challenge(self):
        # Guest with wrong secret rejects the challenge proof.
        a, b = socket.socketpair()
        ho, go = {}, {}
        th = threading.Thread(target=run_host_handshake,
                              args=(a, bs.new_secret(), ho))
        tg = threading.Thread(target=run_guest_handshake,
                              args=(b, bs.new_secret(), go))
        th.start()
        tg.start()
        th.join(15)
        tg.join(15)
        self.assertIsInstance(go.get("exc"), bs.HandshakeFailed)
        a.close()
        b.close()

    def _bad_auth_attempt(self, listener):
        """Mimic a guest up to AUTH, then send a bad proof."""
        raw = socket.create_connection(("127.0.0.1", listener.port),
                                       timeout=5)
        try:
            bs.write_json_line(raw, {"t": "HELLO", "v": bs.PROTOCOL_VERSION,
                                     "game_ver": 1, "nonce_g": bs.online_b64e(
                                         bs.new_nonce(bs.NONCE_BYTES_TCP))})
            chall = bs.read_json_line(raw, 5)
            assert chall["t"] == "CHALLENGE"
            bs.write_json_line(raw, {"t": "AUTH", "proof_g": "AA=="})
        finally:
            raw.close()

    def test_listener_failure_then_success(self):
        secret = bs.new_secret()
        listener = bs.HostListener(secret, HOST, RULES)
        try:
            box = {}
            t = threading.Thread(target=lambda: box.setdefault(
                "r", self._serve(listener)), daemon=True)
            t.start()
            self._bad_auth_attempt(listener)
            deadline = time.time() + 5
            while listener.failures < 1 and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(listener.failures, 1)
            self.assertFalse(listener.burned)
            # host keeps listening: correct code still works
            sock, info = bs.online_dial("127.0.0.1", listener.port, secret,
                                        GUEST, RULES)
            t.join(10)
            conn, hinfo = box["r"]
            self.assertEqual(hinfo["session_id"], info["session_id"])
            sock.close()
            conn.close()
        finally:
            listener.close()

    def _serve(self, listener):
        try:
            return listener.serve_once(timeout=10)
        except Exception as exc:
            return exc

    def test_three_failures_burn_code(self):
        secret = bs.new_secret()
        listener = bs.HostListener(secret, HOST, RULES)
        try:
            for _ in range(3):
                box = {}
                t = threading.Thread(target=lambda: box.setdefault(
                    "r", self._serve(listener)), daemon=True)
                t.start()
                self._bad_auth_attempt(listener)
                t.join(10)
            self.assertEqual(listener.failures, 3)
            self.assertTrue(listener.burned)
            with self.assertRaises(bs.HandshakeFailed):
                bs.online_dial("127.0.0.1", listener.port, secret, GUEST,
                               RULES, timeout=3)
        finally:
            listener.close()

    def test_second_connection_closed_immediately(self):
        secret = bs.new_secret()
        listener = bs.HostListener(secret, HOST, RULES)
        try:
            box = {}
            t = threading.Thread(target=lambda: box.setdefault(
                "r", self._serve(listener)), daemon=True)
            t.start()
            sock, info = bs.online_dial("127.0.0.1", listener.port, secret,
                                        GUEST, RULES)
            t.join(10)
            conn, _hinfo = box["r"]
            # second connect while session active
            box2 = {}
            t2 = threading.Thread(target=lambda: box2.setdefault(
                "r", self._serve(listener)), daemon=True)
            t2.start()
            time.sleep(0.3)
            raw2 = socket.create_connection(("127.0.0.1", listener.port),
                                            timeout=5)
            raw2.settimeout(3)
            try:
                data = raw2.recv(1)
            except OSError:
                data = b""
            self.assertEqual(data, b"")
            raw2.close()
            # first session undisturbed
            self.assertTrue(listener.active)
            t2.join(5)
            sock.close()
            conn.close()
        finally:
            listener.close()

    def test_stalled_handshake_times_out(self):
        old = bs.HANDSHAKE_TIMEOUT_S
        bs.HANDSHAKE_TIMEOUT_S = 0.3
        listener = bs.HostListener(bs.new_secret(), HOST, RULES)
        try:
            raw = socket.create_connection(("127.0.0.1", listener.port),
                                           timeout=5)
            with self.assertRaises(bs.HandshakeFailed):
                listener.serve_once(timeout=5)
            raw.close()
        finally:
            bs.HANDSHAKE_TIMEOUT_S = old
            listener.close()

    def test_oversize_handshake_line_closes(self):
        secret = bs.new_secret()
        listener = bs.HostListener(secret, HOST, RULES)
        try:
            box = {}
            t = threading.Thread(target=lambda: box.setdefault(
                "r", self._serve(listener)), daemon=True)
            t.start()
            raw = socket.create_connection(("127.0.0.1", listener.port),
                                           timeout=5)
            raw.sendall(b"y" * 2000)
            raw.settimeout(3)
            try:
                data = raw.recv(1)
            except OSError:
                data = b""
            self.assertEqual(data, b"")
            raw.close()
            t.join(10)
            self.assertEqual(listener.failures, 1)
        finally:
            listener.close()

    def test_rules_mismatch(self):
        secret = bs.new_secret()
        a, b = socket.socketpair()
        ho, go = {}, {}
        th = threading.Thread(target=run_host_handshake, args=(a, secret, ho))
        tg = threading.Thread(target=run_guest_handshake,
                              args=(b, secret, go),
                              kwargs={"rules": "different"})
        th.start()
        tg.start()
        th.join(15)
        tg.join(15)
        self.assertIsInstance(go.get("exc"), bs.RulesMismatch)
        self.assertIsInstance(ho.get("exc"),
                              (bs.RulesMismatch, bs.HandshakeFailed,
                               bs.TransportClosed))
        a.close()
        b.close()

    def test_version_mismatch(self):
        secret = bs.new_secret()
        a, b = socket.socketpair()
        ho, go = {}, {}
        th = threading.Thread(target=run_host_handshake, args=(a, secret, ho))
        tg = threading.Thread(target=run_guest_handshake,
                              args=(b, secret, go),
                              kwargs={"game_ver": 999})
        th.start()
        tg.start()
        th.join(10)
        tg.join(10)
        self.assertIsInstance(ho.get("exc"), bs.VersionMismatch)
        self.assertIsInstance(go.get("exc"), bs.VersionMismatch)
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
