"""Read-only crawl of the Pulsed Media storefront.

Used by the Telegram control commands (`/products`, `/watch`) so a product can
be looked up by id or by name without knowing which category page it lives on.

Everything here is anonymous: the storefront renders the same list logged out,
so this never touches the authenticated session the hot path depends on. That
matters — the crawl runs on the Telegram thread and must not race the poller.
"""
import logging
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("catalog")

QTY_RE = re.compile(r"([\d,]+)\s*Available", re.I)
CACHE_SECONDS = 120


class Product(object):
    __slots__ = ("pid", "name", "url", "category", "category_url", "qty", "price")

    def __init__(self, pid, name, url, category, category_url, qty, price):
        self.pid = pid
        self.name = name
        self.url = url
        self.category = category
        self.category_url = category_url
        self.qty = qty          # None when WHMCS publishes no stock figure
        self.price = price

    @property
    def in_stock(self):
        return self.qty is None or self.qty > 0

    def line(self):
        if self.qty is None:
            stock = "n/a"
        elif self.qty > 0:
            stock = "%d in stock" % self.qty
        else:
            stock = "OUT"
        bits = [str(self.pid), self.name, stock]
        if self.price:
            bits.append(self.price)
        return " · ".join(bits)


class Catalog(object):
    """Cached pid -> Product map, refreshed at most every CACHE_SECONDS."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.products = {}
        self.fetched_at = 0.0
        self.error = None
        self.lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": cfg.USER_AGENT,
                               "Accept-Language": "en-US,en;q=0.9"})

    # -- crawl ---------------------------------------------------------------
    def store_root(self):
        base = self.cfg.BASE.rstrip("/")
        return base + "/index.php/store/"

    def category_urls(self):
        """Every category slug linked from the store nav, in menu order."""
        r = self.s.get(self.store_root(), timeout=self.cfg.TIMEOUT)
        r.raise_for_status()
        root = urlparse(self.store_root()).path
        out = []
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            href = urljoin(r.url, a["href"])
            path = urlparse(href).path
            if not path.startswith(root):
                continue
            slug = path[len(root):].strip("/")
            # One segment = a category; two = an individual product page.
            if not slug or "/" in slug:
                continue
            if href not in out:
                out.append(href)
        return out

    def refresh(self, force=False):
        # The poll loop and the Telegram thread both read the catalogue; the
        # lock keeps one crawl from turning into two.
        with self.lock:
            return self._refresh(force)

    def _refresh(self, force):
        if not force and self.products and time.time() - self.fetched_at < CACHE_SECONDS:
            return self.products
        found = {}
        try:
            for cat_url in self.category_urls():
                found.update(self._scrape_category(cat_url))
        except requests.RequestException as exc:
            self.error = str(exc)
            log.warning("Catalog refresh failed: %s", exc)
            return self.products      # keep whatever we already had
        if found:
            self.products = found
            self.fetched_at = time.time()
            self.error = None
        return self.products

    def _scrape_category(self, cat_url):
        r = self.s.get(cat_url, timeout=self.cfg.TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        category = urlparse(r.url).path.rstrip("/").rsplit("/", 1)[-1]
        out = {}
        for block in soup.select("div.product"):
            pid = (block.get("id") or "").replace("product", "")
            if not pid.isdigit():
                continue
            pid = int(pid)
            name_el = soup.find(id="product%d-name" % pid)
            link = block.find("a", id="product%d-order-button" % pid)
            if not (name_el and link and link.get("href")):
                continue
            qty_el = block.find(class_="qty")
            qty = None
            if qty_el:
                m = QTY_RE.search(qty_el.get_text(" ", strip=True))
                if m:
                    qty = int(m.group(1).replace(",", ""))
            price_el = soup.select_one("#product%d-price .price" % pid)
            out[pid] = Product(
                pid=pid,
                name=name_el.get_text(" ", strip=True),
                url=urljoin(r.url, link["href"]),
                category=category,
                category_url=r.url,
                qty=qty,
                price=price_el.get_text(" ", strip=True) if price_el else "",
            )
        return out

    # -- lookup --------------------------------------------------------------
    def get(self, pid, force=False):
        self.refresh(force=force)
        return self.products.get(int(pid))

    def search(self, term):
        """Products whose name contains every whitespace-separated word."""
        self.refresh()
        words = [w for w in term.lower().split() if w]
        hits = []
        for p in self.products.values():
            low = p.name.lower()
            if all(w in low for w in words):
                hits.append(p)
        return sorted(hits, key=lambda p: (p.category, p.name))

    def by_category(self):
        self.refresh()
        groups = {}
        for p in self.products.values():
            groups.setdefault(p.category, []).append(p)
        for items in groups.values():
            items.sort(key=lambda p: p.name)
        return groups
