"""Telegram notifications (optional — silently no-ops when unconfigured)."""
import logging

import requests

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, cfg):
        self.token = cfg.TG_TOKEN
        self.chat = cfg.TG_CHAT
        self.timeout = cfg.TIMEOUT

    @property
    def enabled(self):
        return bool(self.token and self.chat)

    def send(self, text):
        log.info("NOTIFY: %s", text.replace("\n", " / "))
        if not self.enabled:
            return
        try:
            requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % self.token,
                data={
                    "chat_id": self.chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            log.warning("Telegram notification failed: %s", exc)
