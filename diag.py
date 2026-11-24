#!/usr/bin/env python3
"""One-off diagnostic: what does THIS host see when it talks to Pulsed Media?

Run on the bot's machine:  python diag.py
The printed public IP must match the IP of the browser the cookie came from.
Prints no secrets beyond a short cookie fingerprint.
"""
import re
import sys

import requests
from bs4 import BeautifulSoup

import config as cfg
from whmcs import PulsedMedia


def main():
    print("cookie configured:", bool(cfg.COOKIE),
          "| fingerprint:", (cfg.COOKIE or "")[-6:])

    try:
        ip = requests.get("https://api.ipify.org", timeout=15).text.strip()
        print("this host's public IP:", ip)
    except requests.RequestException as exc:
        print("could not determine public IP:", exc)

    pm = PulsedMedia(cfg)

    for label, url in [
        ("catalog (no auth needed)", cfg.CATEGORY_URL),
        ("clientarea (needs auth)", pm.url("clientarea.php")),
    ]:
        print("\n=== %s ===" % label)
        try:
            r = pm.get(url, allow_redirects=True)
        except requests.RequestException as exc:
            print("  REQUEST FAILED:", exc)
            continue
        body = r.text
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else "(no title)"
        print("  status:", r.status_code, "| bytes:", len(body))
        print("  final url:", r.url)
        print("  title:", title[:90])
        print("  server:", r.headers.get("Server"), "| cf-ray:", r.headers.get("CF-RAY"))

        flags = []
        low = body.lower()
        for needle, meaning in [
            ("cloudflare", "Cloudflare page"),
            ("just a moment", "Cloudflare JS challenge"),
            ("access denied", "access denied"),
            ("blocked", "blocked"),
            ("captcha", "captcha present"),
            ('name="password"', "LOGIN FORM = not authenticated"),
            ("logout", "logout link = AUTHENTICATED"),
        ]:
            if needle in low:
                flags.append(meaning)
        print("  flags:", ", ".join(flags) or "none")

        if r.status_code != 200 or "logout" not in low:
            text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
            print("  first 300 chars:", text[:300])

    print("\nis_logged_in() ->", pm.is_logged_in())


if __name__ == "__main__":
    sys.exit(main())
