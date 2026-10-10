"""T-menu: online fully usable from the game menu, no CLI exclusives."""
import socket
import sys
import threading

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import battleships as bs


class FakeArgs:
    def __init__(self, **kw):
        self.name = kw.get("name", None)
        self.port = kw.get("port", 0)
        self.bind = kw.get("bind", None)
        self.resume_timeout = kw.get("resume_timeout", None)
        self.net_debug = kw.get("net_debug", False)
        self.stun = kw.get("stun", [])
        self.no_upnp = kw.get("no_upnp", False)


def _host_accept_box(listener, cfg, box):
    try:
        box["r"] = bs._online_accept_session(listener, cfg)
    except Exception as exc:  # noqa: BLE001
        box["e"] = exc


def _loopback_pair(cfg_host, cfg_guest, rules):
    """Real TCP loopback host+guest via the shared menu/CLI core."""
    listener, _secret, code = bs._online_prepare_host(cfg_host, rules)
    box = {}
    t = threading.Thread(target=_host_accept_box,
                         args=(listener, cfg_host, box), daemon=True)
    t.start()
    try:
        session_g, info_g = bs._online_join_session(cfg_guest, code, rules)
    finally:
        t.join(10)
    assert "r" in box, "host accept failed: %r" % (box.get("e"),)
    session_h, info_h = box["r"]
    assert info_h["session_id"] == info_g["session_id"]
    listener.close()
    return (session_h, info_h), (session_g, info_g)


if __name__ == "__main__":
    import unittest

    class MenuParityTest(unittest.TestCase):
        def test_menu_entry_points_exist(self):
            for fn in ("online_menu", "online_host_menu",
                       "online_join_menu", "online_settings_menu",
                       "online_config_from_args", "online_join_error_text",
                       "_online_prepare_host", "_online_accept_session",
                       "_online_join_session", "_online_launch_game",
                       "_OnlineMenuClient", "OnlineConfig"):
                self.assertTrue(callable(getattr(bs, fn, None)), fn)

        def test_config_from_args_defaults(self):
            cfg = bs.online_config_from_args(FakeArgs())
            self.assertEqual(cfg.name, "Player")
            self.assertEqual(cfg.port, 0)
            self.assertEqual(cfg.bind, "0.0.0.0")
            self.assertIsNone(cfg.resume_timeout)
            self.assertFalse(cfg.net_debug)
            self.assertEqual(cfg.stun, [])
            self.assertFalse(cfg.no_upnp)

        def test_config_from_args_all_flags(self):
            cfg = bs.online_config_from_args(FakeArgs(
                name="Ada", port=41234, bind="127.0.0.1",
                resume_timeout=33, net_debug=True,
                stun=["a:3478", "b:3478"], no_upnp=True))
            self.assertEqual(cfg.name, "Ada")
            self.assertEqual(cfg.port, 41234)
            self.assertEqual(cfg.bind, "127.0.0.1")
            self.assertEqual(cfg.resume_timeout, 33)
            self.assertTrue(cfg.net_debug)
            self.assertEqual(cfg.stun, ["a:3478", "b:3478"])
            self.assertTrue(cfg.no_upnp)
            desc = "\n".join(cfg.describe())
            for needle in ("Ada", "41234", "127.0.0.1", "33",
                           "STUN", "UPnP", "Net debug"):
                self.assertIn(needle, desc)

        def test_error_text_covers_every_join_failure(self):
            self.assertIn("Version", bs.online_join_error_text(
                bs.VersionMismatch("x")))
            self.assertIn("board", bs.online_join_error_text(
                bs.RulesMismatch("x")).lower())
            self.assertIn("code", bs.online_join_error_text(
                bs.HandshakeFailed("nope")).lower())
            self.assertIn("refus", bs.online_join_error_text(
                ConnectionRefusedError("x")).lower())
            self.assertIn("VPN", bs.online_join_error_text(
                socket.timeout("x")))

        def test_menu_client_shim_interface(self):
            c = bs._OnlineMenuClient("Me")
            c.print_now("hi")
            c.add_chat("hi")
            c.handle_match_chat("Peer", "hello")
            c.print_chat_history()

        def test_bump_score(self):
            s = {"win": 0, "loss": 0}
            bs._online_bump_score(s, "win")
            bs._online_bump_score(s, "loss")
            bs._online_bump_score(s, "abandoned")
            self.assertEqual(s, {"win": 1, "loss": 1})

        def test_match_params_deterministic(self):
            a = bs.match_params_from_handshake("s", "H", "G", "single")
            b = bs.match_params_from_handshake("s", "H", "G", "single")
            self.assertEqual(a, b)
            self.assertIn(a[1], ("host", "guest"))

        def test_shared_core_loopback(self):
            rules = bs.canonical_rules_hash(bs.SIZE, bs.FLEET, "single")
            ch = bs.OnlineConfig(name="H", port=0, bind="127.0.0.1",
                                 resume_timeout=5)
            cg = bs.OnlineConfig(name="G", port=0, bind="127.0.0.1",
                                 resume_timeout=5)
            (sh, _ih), (sg, _ig) = _loopback_pair(ch, cg, rules)
            try:
                sh.send_game({"type": "chat", "text": "hi"})
                self.assertEqual(sg.recv_game(timeout=5)["text"], "hi")
            finally:
                sh.close()
                sg.close()

        def test_rules_mismatch_surfaces(self):
            rules_h = bs.canonical_rules_hash(bs.SIZE, bs.FLEET, "single")
            rules_g = bs.canonical_rules_hash(bs.SIZE, bs.FLEET, "salvo")
            ch = bs.OnlineConfig(name="H", port=0, bind="127.0.0.1",
                                 resume_timeout=5)
            cg = bs.OnlineConfig(name="G", port=0, bind="127.0.0.1",
                                 resume_timeout=5)
            listener, _secret, code = bs._online_prepare_host(ch, rules_h)
            box = {}
            t = threading.Thread(target=_host_accept_box,
                                 args=(listener, ch, box), daemon=True)
            t.start()
            try:
                with self.assertRaises(bs.RulesMismatch):
                    bs._online_join_session(cg, code, rules_g)
            finally:
                t.join(10)
                listener.close()

        def test_main_menu_lists_online(self):
            with open("battleships.py", encoding="utf-8") as f:
                src = f.read()
            self.assertIn('"Online Match"', src)
            self.assertIn("online_menu(online_cfg", src)

    unittest.main(verbosity=2)
