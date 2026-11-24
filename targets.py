"""Watch targets: what the sniper is hunting, and how much of it to buy.

The list lives in a JSON file so it survives restarts and so the Telegram
commands and the poll loop share one source of truth. Every mutation goes
through TargetStore, which holds a lock — the Telegram thread edits the list
while the poll loop is reading it.
"""
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("targets")

# Fields the poller fills in; they are persisted so /status still says something
# useful in the first seconds after a restart.
RUNTIME_FIELDS = ("in_stock", "owned", "last_checked", "last_change",
                  "last_error", "checks", "resolved")


class ConfigView(object):
    """cfg module with a handful of per-target values shadowed over it.

    whmcs.PulsedMedia reads its target from `self.cfg`, so binding a target is
    just swapping in one of these. Anything not overridden — timeouts, mode,
    dry-run, credentials — still comes from the global config.
    """

    def __init__(self, base, overrides):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_over", overrides)

    def __getattr__(self, name):
        over = object.__getattribute__(self, "_over")
        if name in over and over[name] is not None:
            return over[name]
        return getattr(object.__getattribute__(self, "_base"), name)


def normalise_names(value):
    """A str, a separated str, or a list -> an ordered list of unique names.

    Case-insensitive de-duplication, because Pulsed Media treats the usernames
    as case-insensitive and two pool entries differing only in case would be
    one name that looks like two.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    out, seen = [], set()
    for name in value:
        name = (name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out


class Target(object):
    def __init__(self, pid, name="", url="", category_url="", want=None, buy=1,
                 max_total=None, cycle=None, username=None, usernames=None,
                 interval=None, enabled=True, done=False, added_at=None,
                 orders=None, siblings=None, **runtime):
        self.pid = int(pid)
        self.name = name
        self.url = url
        self.category_url = category_url
        self.want = want              # absolute: stop once the account holds this many
        self.buy = int(buy)           # how many MORE to buy, until `want` is fixed
        self.max_total = max_total
        self.cycle = cycle
        # A pool, not one name. Pulsed Media will not take the same seedbox
        # username twice, so buying N boxes needs N names up front - otherwise
        # the target orders once and then blocks on its own order forever.
        # `username=` is still accepted so an old targets.json loads unchanged.
        self.usernames = normalise_names(
            usernames if usernames is not None else username)
        self.pending_username = None  # runtime: the name the next order uses
        self.interval = interval      # seconds between checks; None = PM_POLL_SECONDS
        self.next_check = 0.0         # runtime only: when this target is next due
        self.enabled = bool(enabled)
        self.done = bool(done)
        self.added_at = added_at or time.time()
        self.orders = list(orders or [])
        self.siblings = list(siblings or [])

        # runtime
        self.in_stock = runtime.get("in_stock")
        self.owned = runtime.get("owned")
        self.last_checked = runtime.get("last_checked")
        self.last_change = runtime.get("last_change")
        self.last_error = runtime.get("last_error")
        self.checks = runtime.get("checks") or 0
        self.resolved = False         # re-proved against the catalog every boot

    # -- serialisation -------------------------------------------------------
    def to_dict(self):
        d = {
            "pid": self.pid, "name": self.name, "url": self.url,
            "category_url": self.category_url, "want": self.want, "buy": self.buy,
            "max_total": self.max_total, "cycle": self.cycle,
            "usernames": list(self.usernames), "interval": self.interval,
            "enabled": self.enabled, "done": self.done, "added_at": self.added_at,
            "orders": self.orders, "siblings": self.siblings,
        }
        for f in RUNTIME_FIELDS:
            if f != "resolved":
                d[f] = getattr(self, f)
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

    # -- config binding ------------------------------------------------------
    def cfg_view(self, base):
        return ConfigView(base, {
            "PID": self.pid,
            "PRODUCT_NAME": self.name,
            "CATEGORY_URL": self.category_url or None,
            "TARGET_TOTAL": self.want,
            "MAX_TOTAL": self.max_total,
            "BILLING_CYCLE": self.cycle,
            # The poll loop pins `pending_username` to the free name it picked
            # before it binds for an order, so the custom field is filled with
            # the name actually being bought - not just the first of the pool.
            "USERNAME": self.pending_username or (self.usernames[0]
                                                  if self.usernames else None),
        })

    # -- presentation --------------------------------------------------------
    def state(self):
        if self.done:
            return "done"
        if not self.enabled:
            return "paused"
        if not self.resolved:
            return "resolving"
        if self.last_error:
            return "error"
        if self.in_stock:
            return "IN STOCK"
        return "watching"

    def every(self, cfg):
        """Seconds between checks for this target."""
        return max(2, int(self.interval or cfg.POLL_SECONDS))

    def goal(self):
        if self.want is None:
            return "+%d" % self.buy
        return "%s/%d" % ("?" if self.owned is None else self.owned, self.want)


def username_pool(t, cfg):
    """Every name this target may create a box under, in preference order.

    An empty pool falls back to the global PM_USERNAME. That name belongs to a
    box the account already has, so it reads as claimed and the target refuses
    to order until real names are given - which is the intended behaviour.
    """
    if t.usernames:
        return list(t.usernames)
    g = (getattr(cfg, "USERNAME", "") or "").strip()
    return [g] if g else []


def effective_username(t, cfg):
    """The name this target would order with next, or '' if it has none."""
    if t.pending_username:
        return t.pending_username
    pool = username_pool(t, cfg)
    return pool[0] if pool else ""


def claimed_usernames(store, cfg, exclude_pid=None):
    """lowercased username -> what is already using it.

    Pulsed Media will not take the same seedbox username twice, so ordering a
    second box under one already in use produces a failed or conflicted order
    that has still spent the credit and taken the stock.

    A target's own past orders always claim their names - that is what walks
    the pool forward - but `exclude_pid` exempts the rest of its own pool, so a
    target is never blocked by names it is still holding for its next buys.

    A name listed by two targets belongs to whichever comes first in the watch
    list, and is resolved that way for both of them. Deciding it per-caller
    would have each target see the other as the holder and neither could ever
    order it. /watch and /user reject a duplicate anyway; this is the tie-break
    for a hand-edited targets.json.
    """
    out = {}
    if getattr(cfg, "USERNAME", ""):
        out[cfg.USERNAME.strip().lower()] = ("your existing box (PM_USERNAME=%s)"
                                             % cfg.USERNAME)
    for t in store.all():
        for order in t.orders:
            used = (order.get("username") or "").strip().lower()
            if used:
                out[used] = "the box ordered as %s (%s)" % (order.get("order"), t.name)

    owner = {}
    for t in store.all():
        for name in t.usernames:
            owner.setdefault(name.lower(), t)
    for name, t in owner.items():
        if t.pid != exclude_pid:
            out.setdefault(name, "target %d (%s)" % (t.pid, t.name))
    return out


def free_usernames(t, store, cfg):
    """This target's own names that nothing has claimed yet, in order."""
    claimed = claimed_usernames(store, cfg, exclude_pid=t.pid)
    return [n for n in username_pool(t, cfg) if n.lower() not in claimed]


def next_username(t, store, cfg):
    """The name the next order must use, or None if the pool is used up."""
    free = free_usernames(t, store, cfg)
    return free[0] if free else None


def remaining_wanted(t):
    """How many more boxes this target still intends to buy."""
    if t.want is None:
        return max(1, t.buy)
    return max(0, t.want - (t.owned or 0))


def username_shortfall(t, store, cfg):
    """How many more names /user needs before the target can finish.

    Reported while there is still time to fix it, rather than at the drop.
    """
    return max(0, remaining_wanted(t) - len(free_usernames(t, store, cfg)))


def username_conflict(t, store, cfg):
    """None while the target still has a free name, else why it has none."""
    pool = username_pool(t, cfg)
    if not pool:
        return "nothing - no username is set at all (PM_USERNAME is empty)"
    if free_usernames(t, store, cfg):
        return None
    claimed = claimed_usernames(store, cfg, exclude_pid=t.pid)
    return ", ".join("%s is used by %s" % (n, claimed.get(n.lower(), "something"))
                     for n in pool)


class TargetStore(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self._targets = []
        self.load()

    # -- persistence ---------------------------------------------------------
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        with self.lock:
            self._targets = []
            for d in raw.get("targets", []):
                try:
                    self._targets.append(Target.from_dict(d))
                except (TypeError, ValueError) as exc:
                    log.warning("Skipping unreadable target %r: %s", d, exc)

    def save(self):
        with self.lock:
            data = {"targets": [t.to_dict() for t in self._targets]}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not persist targets: %s", exc)

    # -- access --------------------------------------------------------------
    def all(self):
        with self.lock:
            return list(self._targets)

    def active(self):
        """Targets the poller should still be checking, in poll order."""
        with self.lock:
            return [t for t in self._targets if t.enabled and not t.done]

    def get(self, pid):
        pid = int(pid)
        with self.lock:
            for t in self._targets:
                if t.pid == pid:
                    return t
        return None

    def add(self, target):
        with self.lock:
            if any(t.pid == target.pid for t in self._targets):
                return False
            self._targets.append(target)
        self.save()
        return True

    def remove(self, pid):
        pid = int(pid)
        with self.lock:
            before = len(self._targets)
            self._targets = [t for t in self._targets if t.pid != pid]
            changed = len(self._targets) != before
        if changed:
            self.save()
        return changed

    def move_to_front(self, pid):
        pid = int(pid)
        with self.lock:
            hit = [t for t in self._targets if t.pid == pid]
            if not hit:
                return False
            self._targets = hit + [t for t in self._targets if t.pid != pid]
        self.save()
        return True

    def seed_from_env(self, cfg):
        """First run: carry the single env-configured target into the list."""
        with self.lock:
            if self._targets:
                return False
        self.add(Target(
            pid=cfg.PID,
            name=cfg.PRODUCT_NAME,
            category_url=cfg.CATEGORY_URL,
            want=cfg.TARGET_TOTAL,
            buy=1,
            max_total=cfg.MAX_TOTAL or None,
            cycle=cfg.BILLING_CYCLE,
        ))
        log.info("Seeded targets.json from the environment (pid %s).", cfg.PID)
        return True
