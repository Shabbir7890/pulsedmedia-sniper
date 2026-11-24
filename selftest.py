#!/usr/bin/env python3
"""Verify the scraper against the live site. No credentials, no orders placed.

Checks that:
  * the catalog page still exposes the target product and its quantity badge
  * the quantity parser agrees with the raw HTML
  * an in-stock product still renders #frmConfigureProduct with the fields we post

Run:  python selftest.py
"""
import logging
import re
import sys

from bs4 import BeautifulSoup

import config as cfg
from whmcs import PulsedMedia, serialize_form

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stdout)
log = logging.getLogger("selftest")

ok = True


def check(label, condition, detail=""):
    global ok
    print("  [%s] %s%s" % ("PASS" if condition else "FAIL", label,
                           (" — " + detail) if detail else ""))
    if not condition:
        ok = False


def main():
    pm = PulsedMedia(cfg)

    print("\n1. Catalog page")
    r = pm.get(cfg.CATEGORY_URL)
    check("HTTP 200", r.status_code == 200, "got %s" % r.status_code)
    soup = BeautifulSoup(r.text, "html.parser")

    block = soup.find(id="product%d" % cfg.PID)
    check("product#%d present" % cfg.PID, block is not None)
    if block is None:
        return finish()

    name_el = soup.find(id="product%d-name" % cfg.PID)
    name = name_el.get_text(strip=True) if name_el else "?"
    print("      name: %s" % name)
    check("name matches PM_PRODUCT_NAME",
          cfg.PRODUCT_NAME.split("-")[-1].strip().lower() in name.lower(),
          "configured %r" % cfg.PRODUCT_NAME)

    qty, in_stock = pm.check_stock()
    print("      parsed quantity: %s -> in_stock=%s" % (qty, in_stock))
    raw = block.find(class_="qty")
    check("quantity badge found", raw is not None,
          raw.get_text(" ", strip=True) if raw else "absent (stock control off?)")

    print("\n2. Every product on the page (sanity view)")
    for b in soup.select("div.product"):
        pid = (b.get("id") or "").replace("product", "")
        n = b.find(id="product%s-name" % pid)
        q = b.find(class_="qty")
        print("      pid %-5s %-45s %s" % (
            pid,
            (n.get_text(strip=True) if n else "?")[:45],
            (q.get_text(" ", strip=True) if q else "no badge"),
        ))

    print("\n3. Order form shape (uses an in-stock product if the target is empty)")
    probe_pid = cfg.PID if in_stock else _first_in_stock(soup, cfg.PID)
    if probe_pid is None:
        print("      SKIP — nothing in stock right now to inspect the form with.")
        return finish()

    print("      probing pid %s" % probe_pid)
    r = pm.get(pm.url("cart.php?a=add&pid=%s" % probe_pid), allow_redirects=True)
    s2 = BeautifulSoup(r.text, "html.parser")
    form = s2.find(id="frmConfigureProduct")
    check("#frmConfigureProduct present", form is not None)
    if form is None:
        return finish()

    pairs = serialize_form(form)
    names = [k for k, _ in pairs]
    check("billingcycle field present", "billingcycle" in names)
    cycles = [o.get("value") for o in form.select("select[name=billingcycle] option")]
    print("      billing cycles: %s" % ", ".join(cycles))
    check("PM_BILLING_CYCLE is valid", cfg.BILLING_CYCLE in cycles,
          "configured %r" % cfg.BILLING_CYCLE)

    custom = [n for n in names if n.startswith("customfield[")]
    print("      custom fields: %s" % (", ".join(custom) or "none"))
    for fid, label in PulsedMedia._customfield_labels(form).items():
        required = bool(form.find(id="customfield%s" % fid) and
                        re.search(r"\*", str(form.find("label", attrs={"for": "customfield%s" % fid}) or "")))
        configured = cfg.CUSTOMFIELDS.get(fid) or (cfg.USERNAME if "username" in label.lower() else "")
        print("        id %s  %-40s required=%-5s configured=%r"
              % (fid, label[:40], required, configured))

    pm.empty_cart()
    return finish()


def _first_in_stock(soup, skip_pid):
    for b in soup.select("div.product"):
        pid = (b.get("id") or "").replace("product", "")
        if not pid.isdigit() or int(pid) == skip_pid:
            continue
        q = b.find(class_="qty")
        if q is None:
            return pid
        m = re.search(r"([\d,]+)\s*Available", q.get_text(" ", strip=True), re.I)
        if m and int(m.group(1).replace(",", "")) > 0:
            return pid
    return None


def finish():
    print("\n%s\n" % ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED — see above"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
