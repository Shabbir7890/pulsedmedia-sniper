#!/usr/bin/env python3
"""Answer the two questions that need YOUR account to settle. Places no orders.

    PM_COOKIE='WHMCSmNqPBCNDcH8d=...' python tools/probe_auth.py

Reports:
  1. whether the cookie actually authenticates you
  2. whether the LOGGED-IN checkout form is captcha-gated
     -> decides whether fully automatic ordering is possible at all
  3. the exact custom fields the target product will ask for

It briefly puts a cheap in-stock product in your cart to render a real logged-in
checkout page, then empties the cart again. Nothing is ever purchased.
"""
import logging
import os
import re
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as cfg  # noqa: E402
from whmcs import OrderError, PulsedMedia

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s",
                    stream=sys.stdout)


def cheapest_in_stock(pm):
    """Pick the cheapest product with stock, to use as a harmless probe."""
    soup = BeautifulSoup(pm.get(cfg.CATEGORY_URL).text, "html.parser")
    best = None
    for b in soup.select("div.product"):
        pid = (b.get("id") or "").replace("product", "")
        if not pid.isdigit():
            continue
        q = b.find(class_="qty")
        if q is not None:
            m = re.search(r"([\d,]+)\s*Available", q.get_text(" ", strip=True), re.I)
            if not m or int(m.group(1).replace(",", "")) == 0:
                continue
        price = b.find(class_="price")
        try:
            val = float(re.search(r"[\d.]+", price.get_text()).group(0))
        except (AttributeError, ValueError):
            val = 9e9
        name = b.find(id="product%s-name" % pid)
        if best is None or val < best[1]:
            best = (pid, val, name.get_text(strip=True) if name else pid)
    return best


def main():
    pm = PulsedMedia(cfg)

    print("\n=== 1. Authentication ===")
    if not cfg.COOKIE and not (cfg.EMAIL and cfg.PASSWORD):
        sys.exit("Set PM_COOKIE (recommended) or PM_EMAIL/PM_PASSWORD first.")
    print("  login form captcha-gated: %s" % pm.login_form_has_captcha())
    try:
        pm.login()
        print("  authenticated: YES")
    except OrderError as exc:
        print("  authenticated: NO — %s" % exc)
        sys.exit(1)

    print("\n=== 2. Target product custom fields ===")
    qty, in_stock = pm.check_stock()
    print("  pid %s stock: %s" % (cfg.PID, qty))
    probe_pid = None
    if in_stock:
        probe_pid = str(cfg.PID)
        print("  target is IN STOCK — reading its real form")
    else:
        cheap = cheapest_in_stock(pm)
        if not cheap:
            print("  nothing in stock to probe with; re-run later.")
            return
        probe_pid, price, name = cheap
        print("  target out of stock; probing with pid %s (%s, %.2f) instead"
              % (probe_pid, name, price))
        print("  NOTE: custom field ids differ per product, so the ids below are")
        print("        the probe's. The bot matches by LABEL, so this still tells")
        print("        you which labels it must fill.")

    pm.empty_cart()
    r = pm.get(pm.url("cart.php?a=add&pid=%s" % probe_pid), allow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find(id="frmConfigureProduct")
    if form is None:
        print("  could not load the config form")
        return
    for fid, label in PulsedMedia._customfield_labels(form).items():
        configured = cfg.CUSTOMFIELDS.get(fid) or (
            cfg.USERNAME if "username" in label.lower() else "")
        print("    id %-5s %-38s configured=%r" % (fid, label[:38], configured))

    print("\n=== 3. Logged-in checkout: captcha or not? ===")
    pm.configure_product(soup)
    gated = pm.checkout_form_has_captcha()
    if gated is None:
        print("  could not load the checkout form")
    elif gated:
        print("  CAPTCHA PRESENT on logged-in checkout.")
        print("  -> Fully automatic ordering is NOT possible.")
        print("  -> Use PM_MODE=cart: the bot detects stock, logs in, fills the")
        print("     form and leaves the item in your cart, then pings you to click")
        print("     Complete Order. That still removes every slow manual step.")
    else:
        print("  NO captcha on logged-in checkout.")
        print("  -> Fully automatic ordering WILL work. Use PM_MODE=order.")

    pm.empty_cart()
    print("\n  cart emptied — nothing was ordered.\n")


if __name__ == "__main__":
    main()
