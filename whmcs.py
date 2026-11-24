"""Minimal WHMCS storefront client for the Pulsed Media order flow.

Flow reverse-engineered from the live site:

  1. GET  index.php/store/<category>            -> catalog, per-product
                                                   <span class="qty">N Available</span>
  2. GET  cart.php?a=add&pid=<pid>              -> 302 to the product config page,
                                                   stages the product in the session
  3. POST cart.php  ajax=1&a=confproduct&...    -> commits it to the cart.
                                                   EMPTY body == success,
                                                   non-empty == <li> validation errors
  4. GET  cart.php?a=checkout                   -> checkout form (#frmCheckout)
  5. POST cart.php?a=checkout                   -> places the order
"""
import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("whmcs")

OUT_OF_STOCK_RE = re.compile(r"out\s*of\s*stock", re.I)
QTY_RE = re.compile(r"([\d,]+)\s*Available", re.I)
PRICE_RE = re.compile(r"([\d]+(?:[.,][\d]{1,2})?)")

# Months each WHMCS billing cycle bills for, so PM_MAX_TOTAL can be read as a
# per-month ceiling: the cart total (whole term, upfront) / months.
CYCLE_MONTHS = {
    "monthly": 1, "quarterly": 3, "semiannually": 6,
    "annually": 12, "biennially": 24, "triennially": 36,
}


class OrderError(RuntimeError):
    """Raised when a step of the order flow fails in a way worth reporting."""


class CaptchaRequired(OrderError):
    pass


def _soup(resp):
    return BeautifulSoup(resp.text, "html.parser")


def serialize_form(form):
    """Approximate the jQuery .serialize() of a BeautifulSoup <form>."""
    data = []
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        if el.name == "input":
            kind = (el.get("type") or "text").lower()
            if kind in ("submit", "button", "image", "reset", "file"):
                continue
            if kind in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    data.append((name, el.get("value", "on")))
                continue
            data.append((name, el.get("value", "")))
        elif el.name == "select":
            options = el.find_all("option")
            chosen = [o for o in options if o.has_attr("selected")]
            if not chosen and options:
                chosen = [options[0]]
            for opt in chosen:
                data.append((name, opt.get("value", opt.get_text(strip=True))))
        else:
            data.append((name, el.get_text()))
    return data


def _replace(pairs, key, value):
    """Set key=value, dropping any existing entries for that key."""
    out = [(k, v) for k, v in pairs if k != key]
    out.append((key, value))
    return out


class PulsedMedia:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": cfg.USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.base_cfg = cfg
        self.product_url = None
        self.product_name = None
        self.other_names = []
        # Reuse one pooled TLS connection so the hot path pays no handshake cost.
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self.s.mount("https://", adapter)
        if cfg.COOKIE:
            self._load_cookie(cfg.COOKIE)

    def _load_cookie(self, raw):
        """Accept 'NAME=value', several '; '-separated pairs, or a bare value."""
        raw = raw.strip()
        host = urlparse(self.cfg.BASE).hostname or "pulsedmedia.com"
        pairs = [p.strip() for p in raw.split(";") if p.strip()]
        if not any("=" in p for p in pairs):
            # Bare session id — assume this install's WHMCS session cookie name.
            self.s.cookies.set(self.cfg.COOKIE_NAME, raw, domain=host, path="/")
            return
        for part in pairs:
            if "=" in part:
                k, v = part.split("=", 1)
                self.s.cookies.set(k.strip(), v.strip(), domain=host, path="/")

    def set_cookie(self, raw):
        """Swap in a fresh session cookie without restarting the process.

        Old WHMCS cookies are cleared first: a stale one left alongside the new
        value gets sent too, and WHMCS then picks whichever it likes.
        """
        # clear() needs all three of domain/path/name - passing name alone
        # raises ValueError, so iterate the cookie objects, not the key names.
        for cookie in list(self.s.cookies):
            if cookie.name.startswith("WHMCS"):
                try:
                    self.s.cookies.clear(cookie.domain, cookie.path, cookie.name)
                except (KeyError, ValueError):
                    pass
        self._load_cookie(raw)
        self.cfg = self.base_cfg
        return self.is_logged_in()

    # -- targeting -----------------------------------------------------------
    def bind(self, target):
        """Point the product-specific methods at one watch target.

        Every method below reads its product from `self.cfg`, so a target is
        bound by shadowing the handful of values that differ. Globals -- mode,
        dry run, timeouts, credentials -- still come from the config module.
        The poll loop is single-threaded, so exactly one target is bound at a
        time and the guards always see the product they are guarding.
        """
        self.cfg = target.cfg_view(self.base_cfg)
        # Never inherit the previous target's URL: an unresolved target must
        # fail loudly rather than quietly poll (and stage) the wrong product.
        self.product_url = target.url or None
        self.product_name = target.name
        self.other_names = list(target.siblings or [])
        return self

    def unbind(self):
        self.cfg = self.base_cfg

    # -- plumbing ------------------------------------------------------------
    def url(self, path):
        return "%s/%s" % (self.cfg.BASE, path.lstrip("/"))

    def get(self, url, **kw):
        kw.setdefault("timeout", self.cfg.TIMEOUT)
        return self.s.get(url, **kw)

    def post(self, url, data, **kw):
        kw.setdefault("timeout", self.cfg.TIMEOUT)
        kw.setdefault(
            "headers",
            {"X-Requested-With": "XMLHttpRequest", "Referer": self.url("cart.php")},
        )
        return self.s.post(url, data=data, **kw)

    @staticmethod
    def token_of(soup):
        el = soup.find("input", attrs={"name": "token"})
        return el.get("value") if el else None

    # -- auth ----------------------------------------------------------------
    def is_logged_in(self):
        r = self.get(self.url("clientarea.php"), allow_redirects=True)
        if "logout.php" in r.text or "action=logout" in r.text:
            return True
        # WHMCS bounces anonymous visitors back to the login page.
        return "index.php/login" not in r.url and 'name="password"' not in r.text

    def login_form_has_captcha(self):
        """Check whether this install gates the login form behind reCAPTCHA."""
        soup = _soup(self.get(self.url("index.php/login")))
        form = soup.select_one("form.login-form") or soup.find(
            "form", attrs={"action": re.compile(r"login")}
        )
        return bool(form and self._has_captcha(form))

    def login(self):
        """Establish an authenticated session.

        PM_COOKIE is the primary path: Pulsed Media puts a reCAPTCHA v2 checkbox
        on the login form, so a username/password POST cannot succeed unattended.
        """
        if self.cfg.COOKIE:
            if self.is_logged_in():
                log.info("Authenticated via PM_COOKIE.")
                return True
            raise OrderError(
                "PM_COOKIE did not authenticate — the session has expired or the "
                "value is wrong. Grab a fresh %s cookie from your browser."
                % self.cfg.COOKIE_NAME
            )

        if not (self.cfg.EMAIL and self.cfg.PASSWORD):
            raise OrderError("No usable credentials: set PM_COOKIE, or PM_EMAIL/PM_PASSWORD.")

        if self.login_form_has_captcha():
            raise CaptchaRequired(
                "The login form is protected by a reCAPTCHA checkbox, so "
                "PM_EMAIL/PM_PASSWORD cannot log in unattended. Log in with your "
                "browser and set PM_COOKIE to the %s cookie value instead "
                "(see README)." % self.cfg.COOKIE_NAME
            )

        page = self.get(self.url("index.php/login"))
        token = self.token_of(_soup(page))
        self.s.post(
            self.url("index.php/login"),
            data={
                "token": token or "",
                "username": self.cfg.EMAIL,
                "password": self.cfg.PASSWORD,
                "rememberme": "on",
            },
            timeout=self.cfg.TIMEOUT,
            headers={"Referer": self.url("index.php/login")},
        )
        if self.is_logged_in():
            log.info("Logged in as %s", self.cfg.EMAIL)
            return True
        raise OrderError("Login failed — check PM_EMAIL / PM_PASSWORD.")

    def keepalive(self):
        """Touch the client area so the PHP session does not idle out."""
        try:
            return self.is_logged_in()
        except requests.RequestException:
            return True  # network blip, not a logout

    def checkout_form_has_captcha(self):
        """Inspect the real (logged-in) checkout form. Needs a non-empty cart."""
        soup = _soup(self.get(self.url("cart.php?a=checkout"), allow_redirects=True))
        form = soup.find(id="frmCheckout") or soup.find("form", attrs={"name": "orderfrm"})
        if form is None:
            return None
        return self._has_captcha(form)

    # -- stock ---------------------------------------------------------------
    def check_stock(self):
        """Return (available, in_stock).

        `available` is None when WHMCS publishes no quantity for the product,
        which happens if stock control is off. That is treated as in stock.
        """
        r = self.get(self.cfg.CATEGORY_URL)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        block = soup.find(id="product%d" % self.cfg.PID)
        if block is None:
            raise OrderError(
                "Product #%d not found on %s (page layout changed?)"
                % (self.cfg.PID, self.cfg.CATEGORY_URL)
            )
        qty_el = block.find(class_="qty")
        if qty_el is None:
            return None, True
        m = QTY_RE.search(qty_el.get_text(" ", strip=True))
        if not m:
            return None, True
        qty = int(m.group(1).replace(",", ""))
        return qty, qty > 0

    # -- fast path -----------------------------------------------------------
    def resolve_product(self):
        """Map PM_PID to its storefront URL, and sanity-check the name.

        Done once at startup so the hot path never has to look anything up, and
        so a wrong PM_PID is caught before the bot is ever armed.
        """
        r = self.get(self.cfg.CATEGORY_URL)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        block = soup.find(id="product%d" % self.cfg.PID)
        if block is None:
            raise OrderError("Product #%d is not on %s" % (self.cfg.PID, self.cfg.CATEGORY_URL))

        name_el = soup.find(id="product%d-name" % self.cfg.PID)
        name = name_el.get_text(" ", strip=True) if name_el else ""
        link = block.find("a", id="product%d-order-button" % self.cfg.PID) or block.find(
            "a", href=re.compile(r"/store/")
        )
        if not link or not link.get("href"):
            raise OrderError("No order link found for product #%d" % self.cfg.PID)

        expected = self.cfg.PRODUCT_NAME.strip().lower()
        tail = expected.split("-")[-1].strip()
        if expected and name and expected not in name.lower() and tail not in name.lower():
            raise OrderError(
                "PM_PID %d resolves to %r, which does not match PM_PRODUCT_NAME %r. "
                "Refusing to start rather than risk ordering the wrong product."
                % (self.cfg.PID, name, self.cfg.PRODUCT_NAME)
            )

        self.product_url = requests.compat.urljoin(self.cfg.CATEGORY_URL, link["href"])
        self.product_name = name

        # Remember every OTHER product in this category, so verify_cart() can
        # spot contamination if anything unexpected ends up in the basket.
        self.other_names = []
        for b in soup.select("div.product"):
            other_pid = (b.get("id") or "").replace("product", "")
            if not other_pid.isdigit() or int(other_pid) == self.cfg.PID:
                continue
            el = soup.find(id="product%s-name" % other_pid)
            if el:
                self.other_names.append(el.get_text(" ", strip=True))
        log.info("pid %d -> %s", self.cfg.PID, name)
        log.info("watch url: %s", self.product_url)
        return self.product_url, name

    def poll_in_stock(self):
        """One 0-byte HEAD. 302 == in stock AND the product is now staged.

        WHMCS redirects the storefront URL straight to the configure step when
        stock exists, and renders an Out of Stock page (200) when it does not.
        HEAD still runs the same PHP, so the redirect also stages the product --
        detection and staging in a single request.
        """
        r = self.s.head(
            self.product_url, allow_redirects=False, timeout=self.cfg.TIMEOUT
        )
        if r.status_code in (301, 302, 303, 307, 308):
            target = r.headers.get("Location", "")
            if "confproduct" in target or "cart.php" in target:
                return True
            log.warning("Unexpected redirect target while polling: %s", target)
            return False
        return False

    def load_staged_form(self):
        """Fetch the staged configure form and prove it is the right product."""
        r = self.get(self.url("cart.php?a=confproduct&i=0"), allow_redirects=True)
        r.raise_for_status()
        if OUT_OF_STOCK_RE.search(r.text) and "frmConfigureProduct" not in r.text:
            raise OrderError("Stock vanished before the form could be loaded.")
        soup = _soup(r)
        form = soup.find(id="frmConfigureProduct")
        if form is None:
            raise OrderError("No configure form at cart.php?a=confproduct&i=0")

        page = soup.get_text(" ", strip=True).lower()
        tail = self.cfg.PRODUCT_NAME.split("-")[-1].strip().lower()
        if tail and tail not in page:
            raise OrderError(
                "The staged product does not look like %r — refusing to configure it."
                % self.cfg.PRODUCT_NAME
            )
        return soup

    def verify_cart(self):
        """Cart must hold exactly one line, and it must be the target product."""
        r = self.get(self.url("cart.php?a=view"))
        r.raise_for_status()
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        low = text.lower()
        tail = self.cfg.PRODUCT_NAME.split("-")[-1].strip().lower()

        hits = low.count(tail) if tail else 0
        if hits == 0:
            raise OrderError(
                "Target product is not in the cart — aborting before checkout."
            )
        if hits > 1:
            raise OrderError(
                "Cart appears to hold %d copies of the target — aborting (want 1)." % hits
            )

        # Any other product from the category in the cart is a red flag.
        others = []
        for other in getattr(self, "other_names", []):
            o = other.strip().lower()
            if o and o != self.cfg.PRODUCT_NAME.strip().lower() and o in low:
                others.append(other)
        if others:
            raise OrderError(
                "Cart also contains: %s — aborting rather than ordering extras."
                % ", ".join(others)
            )
        log.info("Cart verified: 1 x %s", self.cfg.PRODUCT_NAME)

    # -- ordering ------------------------------------------------------------
    def empty_cart(self):
        try:
            self.get(self.url("cart.php?a=empty"))
        except requests.RequestException:
            pass

    def stage_product(self):
        """GET cart.php?a=add&pid=... and return the config page soup."""
        r = self.get(self.url("cart.php?a=add&pid=%d" % self.cfg.PID), allow_redirects=True)
        r.raise_for_status()
        if OUT_OF_STOCK_RE.search(r.text) and "frmConfigureProduct" not in r.text:
            raise OrderError("Product went out of stock before it could be staged.")
        soup = _soup(r)
        if soup.find(id="frmConfigureProduct") is None:
            raise OrderError("Config form not present on the product page.")
        return soup

    @staticmethod
    def _customfield_labels(form):
        labels = {}
        for el in form.find_all(["input", "select", "textarea"]):
            m = re.match(r"customfield\[(\d+)\]", el.get("name") or "")
            if not m:
                continue
            lbl = form.find("label", attrs={"for": el.get("id")})
            labels[m.group(1)] = lbl.get_text(" ", strip=True) if lbl else m.group(1)
        return labels

    def configure_product(self, soup):
        """Commit the staged product into the cart. Empty response == success."""
        form = soup.find(id="frmConfigureProduct")
        pairs = serialize_form(form)
        pairs = _replace(pairs, "a", "confproduct")
        pairs = _replace(pairs, "ajax", "1")
        if self.cfg.BILLING_CYCLE:
            # Cycle availability cannot be read while a product is out of stock,
            # so it is checked here, on the real form, before committing. Without
            # this an unavailable cycle comes back as a wall of WHMCS validation
            # HTML instead of something you can act on.
            select = form.find("select", attrs={"name": "billingcycle"})
            if select is not None:
                offered = [o.get("value") for o in select.find_all("option")
                           if o.get("value")]
                if offered and self.cfg.BILLING_CYCLE not in offered:
                    raise OrderError(
                        "%s does not offer %r billing - it offers %s. Change it "
                        "with /cycle %s <term>."
                        % (self.cfg.PRODUCT_NAME, self.cfg.BILLING_CYCLE,
                           ", ".join(offered), self.cfg.PID))
            pairs = _replace(pairs, "billingcycle", self.cfg.BILLING_CYCLE)

        # Custom fields: explicit ids win, then the USERNAME convenience var.
        labels = self._customfield_labels(form)
        for fid, value in self.cfg.CUSTOMFIELDS.items():
            pairs = _replace(pairs, "customfield[%s]" % fid, value)
        if self.cfg.USERNAME:
            for fid, label in labels.items():
                if "username" in label.lower() and fid not in self.cfg.CUSTOMFIELDS:
                    pairs = _replace(pairs, "customfield[%s]" % fid, self.cfg.USERNAME)

        # Never order more than one, whatever the form defaults to.
        for key in ("qty", "quantity"):
            if any(k == key for k, _ in pairs):
                pairs = _replace(pairs, key, "1")

        current = dict(pairs)
        blank = [
            "%s (id %s)" % (label, fid)
            for fid, label in labels.items()
            if not current.get("customfield[%s]" % fid)
        ]
        if blank:
            log.warning("Custom fields left blank: %s", ", ".join(blank))

        r = self.post(self.url("cart.php"), pairs)
        r.raise_for_status()
        errors = r.text.strip()
        if errors:
            text = BeautifulSoup(errors, "html.parser").get_text(" | ", strip=True)
            raise OrderError("Product configuration rejected: %s" % text[:400])
        log.info("Product added to cart.")

    def cart_total(self):
        """Best-effort cart subtotal, for logging."""
        soup = _soup(self.get(self.url("cart.php?a=view")))
        el = soup.find(id="totalCartPrice") or soup.select_one(".cart-total, .total-due")
        return el.get_text(" ", strip=True) if el else ""

    def checkout(self):
        """Fetch the checkout page, apply account credit, and place the order."""
        # WHMCS sets AffiliateID through aff.php. Omit gid so the referral
        # cannot redirect into a different product/category or stage an item.
        affiliate = getattr(self.cfg, "AFFILIATE_ID", None)
        if affiliate:
            r = self.get(self.url("aff.php"), params={"aff": affiliate},
                         allow_redirects=False)
            r.raise_for_status()
            host = urlparse(self.cfg.BASE).hostname
            matched = [c for c in self.s.cookies
                       if c.name in ("WHMCSAffiliateID", "AffiliateID")
                       and (c.domain.lstrip(".") == host
                            or host.endswith("." + c.domain.lstrip(".")))
                       and c.value == str(affiliate)
                       and not c.is_expired()]
            if not matched:
                log.warning("Referral link visited for affiliate %s, but tracking "
                            "cookie could not be verified; continuing checkout. "
                            "Affiliate commission is not confirmed.", affiliate)
            else:
                log.info("Affiliate tracking applied: %s", affiliate)
        r = self.get(self.url("cart.php?a=checkout"), allow_redirects=True)
        r.raise_for_status()
        soup = _soup(r)
        form = soup.find(id="frmCheckout") or soup.find("form", attrs={"name": "orderfrm"})
        if form is None:
            raise OrderError(
                "Checkout form not found — empty cart, or the session is logged out."
            )

        total_el = soup.find(id="totalCartPrice")
        total_text = total_el.get_text(" ", strip=True) if total_el else "?"
        total_value = None
        if total_el:
            m = PRICE_RE.search(total_text.replace(",", "."))
            if m:
                total_value = float(m.group(1))
        log.info("Checkout total due today: %s", total_text)

        # PM_MAX_TOTAL is a per-MONTH ceiling. A non-monthly cycle bills the
        # whole term upfront, so compare the per-month share, not the raw total.
        cycle = getattr(self.cfg, "BILLING_CYCLE", None) or "monthly"
        months = CYCLE_MONTHS.get(cycle, 1)
        per_month = None if total_value is None else total_value / months
        if self.cfg.MAX_TOTAL and per_month is not None and per_month > self.cfg.MAX_TOTAL:
            pid = getattr(self.cfg, "PID", "")
            term_note = ("" if months == 1 else
                         " (%s billed %s, so %.2f/month upfront)"
                         % (total_text, cycle, per_month))
            raise OrderError(
                "Aborting: %.2f/month is over the %s/month ceiling.%s "
                "Raise it with /max %s <eur>, or use a cheaper cycle."
                % (per_month, self.cfg.MAX_TOTAL, term_note, pid)
            )

        pairs = serialize_form(form)
        pairs = _replace(pairs, "checkout", "true")

        # "Apply <total> from my credit balance. No further payment will be due."
        if form.find("input", attrs={"name": "applycredit"}):
            pairs = _replace(pairs, "applycredit", "1")
        if self.cfg.PAYMENT_METHOD:
            pairs = _replace(pairs, "paymentmethod", self.cfg.PAYMENT_METHOD)
        if form.find("input", attrs={"name": "accepttos"}):
            pairs = _replace(pairs, "accepttos", "on")
        if self.cfg.ORDER_NOTES:
            pairs = _replace(pairs, "notes", self.cfg.ORDER_NOTES)
        if self.cfg.PROMO_CODE:
            pairs = _replace(pairs, "promocode", self.cfg.PROMO_CODE)

        if self._has_captcha(form):
            raise CaptchaRequired(
                "Checkout is captcha-protected for this session. The item is already "
                "in your cart — finish manually: %s" % self.url("cart.php?a=checkout")
            )

        if self.cfg.MODE == "cart":
            log.warning("CART MODE — item is in the cart, not submitting the order.")
            return {"cart_only": True, "total": total_text,
                    "url": self.url("cart.php?a=checkout")}

        if self.cfg.DRY_RUN:
            log.warning(
                "DRY RUN — not submitting. Would POST %d fields, total %s.",
                len(pairs),
                total_text,
            )
            return {"dry_run": True, "total": total_text}

        r = self.s.post(
            self.url("cart.php?a=checkout"),
            data=pairs,
            timeout=self.cfg.TIMEOUT,
            headers={"Referer": self.url("cart.php?a=checkout")},
            allow_redirects=True,
        )
        r.raise_for_status()
        return self._interpret_result(r, total_text)

    @staticmethod
    def _has_captcha(form):
        """True if WHMCS gated this form behind reCAPTCHA.

        WHMCS marks the submit button with `btn-recaptcha` and drops a
        `.recaptcha-container` div in the form. Note it reuses the id
        `google-recaptcha-domainchecker` for that div even on the login and
        checkout forms, so the id must NOT be used to rule captcha out.
        """
        if form.select_one(".btn-recaptcha"):
            return True
        if form.select_one(".recaptcha-container, .g-recaptcha"):
            return True
        return bool(form.find(attrs={"name": "g-recaptcha-response"}))

    @staticmethod
    def _interpret_result(r, total_text):
        body = r.text
        if "a=complete" in r.url or re.search(r"order\s+(confirmation|number)", body, re.I):
            m = re.search(r"Order Number[^\d]{0,40}(\d+)", body, re.I)
            return {
                "ok": True,
                "order_number": m.group(1) if m else None,
                "url": r.url,
                "total": total_text,
            }
        errs = BeautifulSoup(body, "html.parser").select(".alert-danger, .checkout-error")
        detail = " | ".join(e.get_text(" ", strip=True) for e in errs if e.get_text(strip=True))
        raise OrderError("Checkout did not confirm. %s" % (detail[:400] or r.url))

    # -- idempotency ---------------------------------------------------------
    def count_services(self):
        """How many services on the account match PRODUCT_NAME.

        This is the restart-safe guard: it is an absolute count read from the
        account, so a dyno restart (which wipes the /tmp state file) cannot make
        the bot lose track of what it has already bought. Returns None if the
        list could not be read, so callers can refuse to order rather than guess.
        """
        try:
            r = self.get(self.url("clientarea.php?action=services"))
            if r.status_code != 200:
                return None
            soup = BeautifulSoup(r.text, "html.parser")
        except requests.RequestException:
            return None

        needle = (self.cfg.PRODUCT_NAME or "").lower().strip()
        if not needle:
            return None
        tail = needle.split("-")[-1].strip()
        # Word-bounded, so "M10G Storage 8T" does not also count the rows for
        # "M10G Storage 8TB 2.0". Plain substring matching over-counts across
        # this catalogue, and over-counting silently cancels a wanted order.
        patterns = [re.compile(r"(?<!\w)%s(?!\w)" % re.escape(needle))]
        if len(tail) > 8 and tail != needle:
            patterns.append(re.compile(r"(?<!\w)%s(?!\w)" % re.escape(tail)))

        rows = soup.select("table tbody tr") or soup.select("table tr")
        count = 0
        for row in rows:
            text = row.get_text(" ", strip=True).lower()
            if text and any(p.search(text) for p in patterns):
                count += 1
        return count
