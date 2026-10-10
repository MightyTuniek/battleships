"""T8: offer and answer codes (spec 8.2). No network."""
import sys

sys.path.insert(0, ".")

import battleships as bs


def _flip_char(code):
    # Flip a middle character: safely inside body+crc (the last chars may
    # only cover zero padding, and the first byte is the version).
    stripped = [(i, ch) for i, ch in enumerate(code) if ch not in "- "]
    i, ch = stripped[len(stripped) // 2]
    for cand in "ABCDEFGHJKLMNPQRSTUVWXYZ234567":
        if cand != ch.upper():
            return code[:i] + cand + code[i + 1:]
    raise AssertionError("nothing to flip")


if __name__ == "__main__":
    import unittest

    class SignalTest(unittest.TestCase):
        def test_offer_round_trip_no_lan(self):
            secret = bs.new_secret()
            code, offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            back = bs.decode_offer(code)
            self.assertEqual(back["pub"], ("203.0.113.9", 45678))
            self.assertIsNone(back["lan"])
            self.assertEqual(back["secret"], secret)
            self.assertEqual(back["nonce_h"], offer["nonce_h"])
            self.assertEqual(back["flags"] & bs.FLAG_PUNCH, bs.FLAG_PUNCH)

        def test_offer_round_trip_with_lan(self):
            secret = bs.new_secret()
            code, _offer = bs.encode_offer(
                ("203.0.113.9", 45678), secret,
                lan=("192.168.1.5", 45678))
            back = bs.decode_offer(code)
            self.assertEqual(back["lan"], ("192.168.1.5", 45678))
            self.assertEqual(back["pub"], ("203.0.113.9", 45678))
            self.assertEqual(back["secret"], secret)

        def test_offer_typo_forgiveness(self):
            secret = bs.new_secret()
            code, _offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            messy = code.lower().replace("-", " ")
            back = bs.decode_offer(messy)
            self.assertEqual(back["pub"], ("203.0.113.9", 45678))

        def test_offer_typo_caught_by_crc(self):
            secret = bs.new_secret()
            code, _offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            with self.assertRaises(bs.BadInvite):
                bs.decode_offer(_flip_char(code))

        def test_answer_round_trip_and_tag_ok(self):
            secret = bs.new_secret()
            ocode, _offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            acode, answer = bs.encode_answer(
                ocode, ("198.51.100.4", 51234), secret,
                lan=("192.168.2.9", 51234))
            back = bs.decode_answer(acode)
            self.assertEqual(back["pub"], ("198.51.100.4", 51234))
            self.assertEqual(back["lan"], ("192.168.2.9", 51234))
            self.assertEqual(back["nonce_g"], answer["nonce_g"])
            self.assertTrue(bs.verify_answer_tag(ocode, acode, secret))

        def test_tampered_answer_endpoint_fails_tag(self):
            secret = bs.new_secret()
            ocode, _offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            acode, _answer = bs.encode_answer(
                ocode, ("198.51.100.4", 51234), secret)
            import struct as _struct
            import ipaddress as _ip
            back = bs.decode_answer(acode)
            # Attacker swaps the endpoint bytes but reuses the tag.
            evil_ep = _struct.pack(
                ">4sH", _ip.IPv4Address("203.0.113.99").packed, 1111)
            evil_body = back["body"][:2] + evil_ep + back["body"][8:]
            evil = dict(back, pub=("203.0.113.99", 1111), body=evil_body)
            self.assertFalse(bs.verify_answer_tag(ocode, evil, secret))

        def test_wrong_secret_fails_tag(self):
            secret = bs.new_secret()
            ocode, _offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            acode, _answer = bs.encode_answer(
                ocode, ("198.51.100.4", 51234), secret)
            self.assertFalse(bs.verify_answer_tag(
                ocode, acode, bs.new_secret()))

        def test_answer_typo_caught_by_crc(self):
            secret = bs.new_secret()
            ocode, _offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            acode, _answer = bs.encode_answer(
                ocode, ("198.51.100.4", 51234), secret)
            with self.assertRaises(bs.BadInvite):
                bs.decode_answer(_flip_char(acode))

        def test_same_secret_different_nonces_give_different_keys(self):
            secret = bs.new_secret()
            ocode1, _o1 = bs.encode_offer(("203.0.113.9", 45678), secret)
            ocode2, _o2 = bs.encode_offer(("203.0.113.9", 45678), secret)
            acode1, _a1 = bs.encode_answer(
                ocode1, ("198.51.100.4", 51234), secret)
            k1 = bs.punch_keys(secret, bs.decode_offer(ocode1)["nonce_h"],
                               bs.decode_answer(acode1)["nonce_g"])
            k2 = bs.punch_keys(secret, bs.decode_offer(ocode2)["nonce_h"],
                               bs.decode_answer(acode1)["nonce_g"])
            self.assertNotEqual(k1, k2)

        def test_keys_match_both_sides(self):
            secret = bs.new_secret()
            ocode, _offer = bs.encode_offer(("203.0.113.9", 45678), secret)
            acode, _answer = bs.encode_answer(
                ocode, ("198.51.100.4", 51234), secret)
            o, a = bs.decode_offer(ocode), bs.decode_answer(acode)
            kh = bs.punch_keys(secret, o["nonce_h"], a["nonce_g"])
            kg = bs.punch_keys(secret, o["nonce_h"], a["nonce_g"])
            self.assertEqual(kh, kg)
            self.assertEqual(len(kh), 3)

    unittest.main(verbosity=2)
