"""T12: UX flow, messages, README (spec 10/11). Fakes only, no real net."""
import socket
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import battleships as bs
from test_online_t7_stun import FakeStun
from test_online_t11_upnp import FakeIGD


def _names(fn):
    return fn.__code__.co_names


if __name__ == "__main__":
    import unittest

    class UxTest(unittest.TestCase):
        def test_reason_table_covers_section_11(self):
            reasons = ["TIMEOUT", "BAD_CODE", "VERSION", "RULES_MISMATCH",
                       "PEER_LEFT", "LINK_LOST", "PROTOCOL_ERROR"]
            seen = set()
            for reason in reasons:
                msg = bs.message_for(reason)
                self.assertTrue(isinstance(msg, str) and msg.strip(), reason)
                seen.add(msg)
            self.assertEqual(len(seen), len(reasons))
            self.assertEqual(bs.message_for("NO_SUCH_REASON"),
                             bs.message_for("PROTOCOL_ERROR"))

        def test_code_kind_dispatch(self):
            secret = bs.new_secret()
            dcode = bs.encode_invite("203.0.113.9", 45678, secret)
            self.assertEqual(bs.online_code_kind(dcode), "direct")
            ocode, _o = bs.encode_offer(("203.0.113.9", 45678), secret)
            self.assertEqual(bs.online_code_kind(ocode), "offer")
            acode, _a = bs.encode_answer(ocode, ("198.51.100.4", 51234),
                                         secret)
            self.assertEqual(bs.online_code_kind(acode), "answer")
            with self.assertRaises(bs.BadInvite):
                bs.online_code_kind("not-a-code-at-all!!!")

        def test_host_options_plan(self):
            full = bs.plan_host_options(upnp_available=True,
                                        stun_punchable=True,
                                        user_direct=False)
            self.assertTrue(full["direct"][0])
            self.assertTrue(full["punch"][0])
            self.assertTrue(full["manual"][0])
            nopunch = bs.plan_host_options(upnp_available=True,
                                           stun_punchable=False,
                                           user_direct=False)
            self.assertTrue(nopunch["direct"][0])
            self.assertFalse(nopunch["punch"][0])
            vpn = bs.plan_host_options(upnp_available=False,
                                       stun_punchable=False,
                                       user_direct=True)
            self.assertTrue(vpn["direct"][0])
            self.assertTrue(vpn["manual"][0])
            nothing = bs.plan_host_options(upnp_available=False,
                                           stun_punchable=False,
                                           user_direct=False)
            self.assertFalse(nothing["direct"][0])
            self.assertFalse(nothing["punch"][0])
            self.assertTrue(nothing["manual"][0])

        def test_reachability_parallel_with_fakes(self):
            igd = FakeIGD()
            dead = FakeStun(drop_first=10 ** 9)
            live1 = FakeStun()
            live2 = FakeStun()
            try:
                t0 = time.monotonic()
                reach = bs.online_reachability(
                    stun_servers=[dead.addr, live1.addr, live2.addr],
                    ssdp_addr=igd.ssdp_addr, upnp_wait=0.8,
                    stun_timeout=0.5, stun_retries=0)
                elapsed = time.monotonic() - t0
                self.assertTrue(reach["upnp"]["found"])
                self.assertEqual(reach["upnp"]["external_ip"], "203.0.113.8")
                self.assertFalse(reach["upnp"]["cgnat"])
                self.assertTrue(reach["stun"]["punchable"])
                self.assertIsNotNone(reach["stun"]["mapped"])
                # Sequential would take 0.8 (upnp) + 0.5 (dead stun) = 1.3+.
                self.assertLess(elapsed, 1.15)
            finally:
                igd.close()
                dead.close()
                live1.close()
                live2.close()

        def test_reachability_all_dark(self):
            dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            dead.bind(("127.0.0.1", 0))
            dark = ("127.0.0.1", dead.getsockname()[1])
            dead.close()
            reach = bs.online_reachability(
                stun_servers=["127.0.0.1:1"], ssdp_addr=dark, upnp_wait=0.3,
                stun_timeout=0.2, stun_retries=0)
            self.assertFalse(reach["upnp"]["found"])
            self.assertFalse(reach["stun"]["punchable"])
            self.assertIsNone(reach["stun"]["mapped"])

        def test_udp_ready_exchange_names_and_rules(self):
            import socket as _socket
            import threading as _threading
            ka, kb = bs.new_secret(), bs.new_secret()
            sa = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sb = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sa.bind(("127.0.0.1", 0))
            sb.bind(("127.0.0.1", 0))
            pa, pb = sa.getsockname(), sb.getsockname()
            th = bs.UdpTransport(sa, pb, ka, kb)
            tg = bs.UdpTransport(sb, pa, kb, ka)
            try:
                rules = bs.canonical_rules_hash(bs.SIZE, bs.FLEET, "single")
                box = {}

                def run_host():
                    try:
                        box["peer"] = bs.udp_ready_exchange(
                            th, True, "Hosty", rules, "sess-1", timeout=5)
                    except Exception as exc:
                        box["e"] = exc

                t = _threading.Thread(target=run_host, daemon=True)
                t.start()
                peer_h = bs.udp_ready_exchange(tg, False, "GUESTY", rules,
                                               "sess-1", timeout=5)
                t.join(10)
                self.assertNotIn("e", box)
                self.assertEqual(box["peer"], "GUESTY")
                self.assertEqual(peer_h, "Hosty")
            finally:
                th.close()
                tg.close()

        def test_udp_ready_rules_mismatch(self):
            import socket as _socket
            import threading as _threading
            ka, kb = bs.new_secret(), bs.new_secret()
            sa = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sb = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sa.bind(("127.0.0.1", 0))
            sb.bind(("127.0.0.1", 0))
            pa, pb = sa.getsockname(), sb.getsockname()
            th = bs.UdpTransport(sa, pb, ka, kb)
            tg = bs.UdpTransport(sb, pa, kb, ka)
            try:
                rh = bs.canonical_rules_hash(bs.SIZE, bs.FLEET, "single")
                rg = bs.canonical_rules_hash(bs.SIZE, bs.FLEET, "salvo")
                box = {}

                def run_host():
                    try:
                        box["r"] = bs.udp_ready_exchange(
                            th, True, "H", rh, "s", timeout=5)
                    except Exception as exc:
                        box["e"] = exc

                t = _threading.Thread(target=run_host, daemon=True)
                t.start()
                with self.assertRaises(bs.RulesMismatch):
                    bs.udp_ready_exchange(tg, False, "G", rg, "s", timeout=5)
                t.join(10)
                self.assertIsInstance(box.get("e"), bs.RulesMismatch)
            finally:
                th.close()
                tg.close()

        def test_menu_wires_punch_flows(self):
            for fn in (bs._online_host_punch, bs._online_join_punch):
                self.assertIn("punch_connect", _names(fn))
            self.assertIn("encode_offer", _names(bs._online_host_punch))
            self.assertIn("encode_answer", _names(bs._online_join_punch))
            self.assertIn("_online_host_punch", _names(bs.online_host_menu))
            self.assertIn("_online_join_punch", _names(bs.online_join_menu))
            self.assertIn("online_reachability", _names(bs.online_host_menu))
            self.assertIn("_online_join_manual", _names(bs.online_join_menu))

        def test_readme_online_section(self):
            with open("README.md", encoding="utf-8") as f:
                src = f.read()
            self.assertIn("Online play", src)
            for needle in ("VPN", "UPnP", "hole punch", "invite code",
                           "Connection refused", "symmetric"):
                self.assertIn(needle, src)

    unittest.main(verbosity=2)
