"""Offline referral tests: no Telegram calls, live carts, or purchases."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from runtime import Runtime
from tgbot import TelegramBot
from whmcs import OrderError, PulsedMedia


class ReferralTests(unittest.TestCase):
    def client(self):
        return PulsedMedia(SimpleNamespace(BASE="https://pulsedmedia.com/clients",
                           USER_AGENT="test", COOKIE="", AFFILIATE_ID="3246"))

    def test_missing_cookie_does_not_block_checkout(self):
        pm = self.client()
        pm.get = Mock(return_value=SimpleNamespace(text="", raise_for_status=lambda: None))
        with self.assertLogs("whmcs", level="WARNING") as logs:
            with self.assertRaisesRegex(OrderError, "Checkout form not found"):
                pm.checkout()
        self.assertIn("continuing checkout", logs.output[0])
        self.assertEqual(pm.get.call_count, 2)
        first = pm.get.call_args_list[0]
        self.assertFalse(first.kwargs["allow_redirects"])
        self.assertEqual(first.kwargs["params"], {"aff": "3246"})

    def test_prefixed_cookie_is_recognized(self):
        pm = self.client()
        pm.s.cookies.set("WHMCSAffiliateID", "3246", domain=".pulsedmedia.com", path="/")
        pm.get = Mock(return_value=SimpleNamespace(text="", raise_for_status=lambda: None))
        with self.assertLogs("whmcs", level="INFO") as logs:
            with self.assertRaisesRegex(OrderError, "Checkout form not found"):
                pm.checkout()
        self.assertIn("Affiliate tracking applied: 3246", logs.output[0])

    def test_legacy_cookie_is_recognized(self):
        pm = self.client()
        pm.s.cookies.set("AffiliateID", "3246", domain="pulsedmedia.com", path="/")
        pm.get = Mock(return_value=SimpleNamespace(text="", raise_for_status=lambda: None))
        with self.assertLogs("whmcs", level="INFO") as logs:
            with self.assertRaisesRegex(OrderError, "Checkout form not found"):
                pm.checkout()
        self.assertIn("Affiliate tracking applied: 3246", logs.output[0])

    def test_successful_tracking_reaches_dry_run_without_purchase(self):
        pm = self.client()
        pm.cfg.MODE = "auto"
        pm.cfg.DRY_RUN = True
        pm.cfg.MAX_TOTAL = 0
        pm.cfg.BILLING_CYCLE = "monthly"
        pm.cfg.PAYMENT_METHOD = pm.cfg.ORDER_NOTES = pm.cfg.PROMO_CODE = ""
        def get(url, **kwargs):
            if url.endswith("aff.php"):
                pm.s.cookies.set("WHMCSAffiliateID", "3246", domain="pulsedmedia.com", path="/")
                body = ""
            else:
                body = '<form id="frmCheckout"><input name="token" value="test"></form><span id="totalCartPrice">9.99</span>'
            return SimpleNamespace(text=body, raise_for_status=lambda: None)
        pm.get = Mock(side_effect=get)
        pm.s.post = Mock()
        self.assertTrue(pm.checkout()["dry_run"])
        self.assertTrue(pm.get.call_args.args[0].endswith("cart.php?a=checkout"))
        pm.s.post.assert_not_called()

    def test_telegram_cannot_change_referral(self):
        bot = TelegramBot.__new__(TelegramBot)
        bot.cfg = SimpleNamespace(AFFILIATE_ID="3246")
        bot.rt = Runtime()
        bot.send = Mock()
        bot.cmd_ref(["789"], {})
        self.assertEqual(bot.rt.drain(), [])
        self.assertEqual(bot.cfg.AFFILIATE_ID, "3246")

if __name__ == "__main__":
    unittest.main()
