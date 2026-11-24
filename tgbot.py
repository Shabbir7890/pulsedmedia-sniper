"""Telegram control plane: ask the sniper what it is doing, and retarget it.

Runs as a daemon thread next to the poll loop. Commands that only read state or
edit the target list are served straight from this thread; anything needing the
authenticated WHMCS session is handed to the poll loop through Runtime.submit().
"""
import html
import json
import logging
import os
import threading
import time

import requests

from catalog import Catalog
from runtime import ago, duration
from targets import (Target, claimed_usernames, effective_username,
                     free_usernames, normalise_names, remaining_wanted,
                     username_conflict, username_shortfall)

log = logging.getLogger("tgbot")

API = "https://api.telegram.org/bot%s/%s"
MAX_MSG = 3900          # Telegram caps at 4096; leave room for the wrapper
ARM_CONFIRM_WINDOW = 120

# WHMCS billing-cycle option values, plus the shorthands people actually type.
# normalize_cycle() maps anything on the right to the canonical value on the left.
CYCLES = ("monthly", "quarterly", "semiannually", "annually",
          "biennially", "triennially")
# The way you actually say it: number of months.
MONTHS_TO_CYCLE = {
    "1": "monthly", "3": "quarterly", "6": "semiannually",
    "12": "annually", "24": "biennially", "36": "triennially",
}
CYCLE_TO_MONTHS = {v: k for k, v in MONTHS_TO_CYCLE.items()}
CYCLE_ALIASES = {
    "m": "monthly", "mo": "monthly", "month": "monthly",
    "q": "quarterly", "qtr": "quarterly", "quarter": "quarterly",
    "s": "semiannually", "semi": "semiannually", "semiannual": "semiannually",
    "halfyear": "semiannually", "halfyearly": "semiannually",
    "a": "annually", "y": "annually", "yr": "annually",
    "year": "annually", "yearly": "annually", "annual": "annually",
    "bi": "biennially", "biennial": "biennially",
    "tri": "triennially", "triennial": "triennially",
}


def normalize_cycle(token):
    """Canonical WHMCS billing cycle for a user token, or None if unrecognised.

    Accepts the word ('quarterly'), a shorthand ('q'), or the month count
    ('3') - so a bare 1/3/6/12/24/36 reads as the billing term.
    """
    low = str(token).strip().lower()
    if low in CYCLES:
        return low
    if low in MONTHS_TO_CYCLE:
        return MONTHS_TO_CYCLE[low]
    return CYCLE_ALIASES.get(low)


def cycle_label(cycle):
    """'annually (12 months)' for display, or the raw value if it is odd."""
    m = CYCLE_TO_MONTHS.get(cycle)
    if not m:
        return str(cycle)
    return "%s (%s month%s)" % (cycle, m, "" if m == "1" else "s")


def _parse_qty(token):
    """Quantity from an EXPLICIT count token: 'x12'/'X12'/'*12'/'qty:12'/'n:12'.

    A bare number is left alone here - the parser reads a bare 1/3/6/12/24/36 as
    a billing term, and any other bare number as a quantity. To buy a number of
    boxes that collides with a billing term (e.g. 12), write it as x12.
    """
    low = str(token).strip().lower()
    if low.startswith(("qty:", "n:")):
        low = low.split(":", 1)[1]
    elif low.startswith(("x", "*")):
        low = low[1:]
    else:
        return None
    return int(low) if low.isdigit() else None


def parse_watch_args(args):
    """Split /watch arguments into (query_tokens, usernames, qty, cycle, error).

    Everything after the product can arrive in any order:
      - 1/3/6/12/24/36, a cycle word or shorthand -> cycle (months, quarterly, q)
      - x<n> / *<n> / n:<n>, or any other bare number -> qty
      - u:<a>,<b> / user:<a>,<b>                      -> usernames (explicit)
      - any other bare word (pid form)                -> a username

    Usernames accumulate, because one box cannot be bought twice under one
    name: `/watch 355 boxa boxb x2` is two boxes with a name each.

    Bare 1/3/6/12/24/36 is read as the billing term, so to buy that many boxes
    write the count as x<n> (e.g. x12). For the pid form
    (`/watch 354 mybox x2 12`) bare words are classified directly; for the name
    form (`/watch storage 16TB 2`) only options are pulled out so product-name
    words are left alone.
    """
    if not args:
        return [], None, None, None, None

    usernames = []
    qty = cycle = None
    pid_form = args[0].isdigit()
    query = [args[0]]
    rest = args[1:]

    for tok in rest:
        low = tok.lower()
        if low.startswith(("u:", "user:")):
            names = normalise_names(tok.split(":", 1)[1])
            if not names:
                return query, usernames, qty, cycle, "empty username after u:"
            usernames += names
            continue
        c = normalize_cycle(tok)
        if c:
            if cycle is not None and cycle != c:
                return query, usernames, qty, cycle, "two billing cycles given"
            cycle = c
            continue
        q = _parse_qty(tok)
        if q is None and tok.isdigit():
            q = int(tok)          # a bare number not in the billing set = qty
        if q is not None:
            if qty is not None:
                return query, usernames, qty, cycle, "two quantities given"
            qty = q
            continue
        # An unmarked, non-number, non-cycle word.
        if pid_form:
            usernames += normalise_names(tok)
        else:
            query.append(tok)     # part of the product name

    return query, normalise_names(usernames), qty, cycle, None

HELP = [
    ("/ref", "show the purchase referral"),
    ("/setup", "guided walk-through: product, count, usernames, term"),
    ("/cancel", "abandon the /setup in progress"),
    ("/status", "is it alive, what has it seen lately"),
    ("/list", "watch list with stock and progress"),
    ("/watch <pid> [users...] [x n] [months]",
     "snipe - one username per box, count (x2), billing months (1/3/6/12/24/36)"),
    ("/unwatch <pid>", "stop watching it"),
    ("/products [filter]", "browse the store with ids and live stock"),
    ("/stock <pid>", "live stock check for one product"),
    ("/qty <pid> <n>", "buy n more of this one"),
    ("/max <pid> <eur>", "price ceiling per month for this one"),
    ("/cycle <pid> <months>", "change billing: 1 3 6 12 24 36"),
    ("/user <pid> <name...>", "set the username pool - one name per box"),
    ("/user <pid> + <name...>", "add more names without dropping the old ones"),
    ("/speed [pid] <sec>", "how often to check (default 5s)"),
    ("/top <pid>", "check this one first each cycle"),
    ("/pause [pid]", "pause everything, or one target"),
    ("/resume [pid]", "resume"),
    ("/check", "run a poll cycle right now"),
    ("/log [n]", "last n log lines"),
    ("/arm", "allow real orders (asks to confirm)"),
    ("/disarm", "back to dry run"),
    ("/cookie <value>", "hot-swap the WHMCS session cookie"),
    ("/stop", "shut the sniper down"),
]


def esc(text):
    return html.escape(str(text), quote=False)


SETUP_TIMEOUT = 600      # an abandoned wizard is forgotten after this long


class Setup(object):
    """One /setup conversation in progress.

    The bot answers a single authorised chat, so one of these at a time is
    enough. It holds nothing the poll loop can see: the target is built and
    added only at the final confirmation, exactly the way /watch does it, so an
    abandoned wizard cannot leave a half-configured target that might order.
    """

    STEPS = ("product", "count", "usernames", "cycle", "confirm")

    def __init__(self):
        self.step = "product"
        self.candidates = []     # ambiguous name search, awaiting a pick
        self.product = None
        self.count = None
        self.usernames = []
        self.cycle = None
        self.touched = time.time()

    def stale(self):
        return time.time() - self.touched > SETUP_TIMEOUT

    def heading(self, title):
        return "<b>Setup %d/%d - %s</b>" % (
            self.STEPS.index(self.step) + 1, len(self.STEPS), title)


class TelegramBot(object):
    def __init__(self, cfg, store, runtime, notifier, catalog=None):
        self.cfg = cfg
        self.store = store
        self.rt = runtime
        self.notifier = notifier
        self.catalog = catalog or Catalog(cfg)
        self.s = requests.Session()
        self.offset = None
        self.thread = None
        self._arm_asked_at = 0
        self.handlers = {
            "ref": self.cmd_ref, "referral": self.cmd_ref,
            "start": self.cmd_help, "help": self.cmd_help,
            "status": self.cmd_status, "list": self.cmd_list,
            "watch": self.cmd_watch, "unwatch": self.cmd_unwatch,
            "products": self.cmd_products, "search": self.cmd_products,
            "stock": self.cmd_stock, "qty": self.cmd_qty, "max": self.cmd_max,
            "cycle": self.cmd_cycle, "top": self.cmd_top,
            "user": self.cmd_user, "speed": self.cmd_speed,
            "pause": self.cmd_pause, "resume": self.cmd_resume,
            "check": self.cmd_check, "log": self.cmd_log,
            "arm": self.cmd_arm, "disarm": self.cmd_disarm,
            "cookie": self.cmd_cookie, "stop": self.cmd_stop,
            "id": self.cmd_id, "setup": self.cmd_setup,
            "cancel": self.cmd_cancel, "abort": self.cmd_cancel,
        }
        self.setup = None            # the open /setup conversation, if any

    @property
    def enabled(self):
        return bool(self.cfg.TG_TOKEN and self.cfg.TG_CHAT)

    # -- transport -----------------------------------------------------------
    def api(self, method, _timeout=None, **params):
        r = self.s.post(API % (self.cfg.TG_TOKEN, method), data=params,
                        timeout=_timeout or self.cfg.TIMEOUT)
        r.raise_for_status()
        return r.json()

    def send(self, text, chat=None):
        """Send, splitting on line boundaries if the text is too long."""
        chat = chat or self.cfg.TG_CHAT
        for chunk in self._chunks(text):
            try:
                self.api("sendMessage", chat_id=chat, text=chunk,
                         parse_mode="HTML", disable_web_page_preview="true")
            except requests.RequestException as exc:
                log.warning("Telegram send failed: %s", exc)
                return

    @staticmethod
    def _chunks(text):
        if len(text) <= MAX_MSG:
            return [text]
        out, buf = [], ""
        for line in text.split("\n"):
            if len(buf) + len(line) + 1 > MAX_MSG:
                out.append(buf)
                buf = ""
            buf += line + "\n"
        if buf.strip():
            out.append(buf)
        return out

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        if not self.enabled:
            log.info("Telegram control disabled (no token/chat id).")
            return None
        self._skip_backlog()
        self._publish_commands()
        self.thread = threading.Thread(target=self._loop, name="tgbot", daemon=True)
        self.thread.start()
        log.info("Telegram control thread started; /help for commands.")
        return self.thread

    def _skip_backlog(self):
        """Ignore commands sent while the bot was down - they are stale orders."""
        try:
            data = self.api("getUpdates", offset=-1, timeout=0, _timeout=20)
            results = data.get("result") or []
            if results:
                self.offset = results[-1]["update_id"] + 1
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("Could not read Telegram backlog: %s", exc)

    def _publish_commands(self):
        """Populate the in-app command menu so the commands are discoverable."""
        try:
            # HELP lists some commands under more than one form (/user and
            # /user + ...); Telegram rejects the whole call on a duplicate name.
            seen, cmds = set(), []
            for c, d in HELP:
                name = c.split()[0].lstrip("/")
                if name in seen:
                    continue
                seen.add(name)
                cmds.append({"command": name, "description": d})
            self.api("setMyCommands", commands=json.dumps(cmds))
        except (requests.RequestException, ValueError) as exc:
            log.debug("setMyCommands failed: %s", exc)

    def _loop(self):
        backoff = 1
        while not self.rt.stopping:
            try:
                data = self.api("getUpdates", offset=self.offset or 0, timeout=25,
                                allowed_updates=json.dumps(["message"]), _timeout=40)
                backoff = 1
                for upd in data.get("result") or []:
                    self.offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if msg:
                        self._handle(msg)
            except requests.RequestException as exc:
                log.warning("Telegram poll failed: %s - retrying in %ds", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:              # never let this thread die
                log.exception("Telegram handler crashed: %s", exc)
                time.sleep(5)

    # -- dispatch ------------------------------------------------------------
    def _handle(self, msg):
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat != str(self.cfg.TG_CHAT):
            log.warning("Ignoring Telegram message from unauthorised chat %s", chat)
            return
        if not text.startswith("/"):
            if self._setup_reply(text):
                return
            self.send("Not a command. /help lists what I understand.")
            return

        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        args = parts[1:]
        handler = self.handlers.get(cmd)
        if not handler:
            self.send("Unknown command <code>/%s</code>. Try /help." % esc(cmd))
            return
        # /cookie carries a live session; it must never reach the log file.
        log.info("Telegram command: /%s %s", cmd,
                 "(redacted)" if cmd == "cookie" and args else " ".join(args))
        try:
            handler(args, msg)
        except Exception as exc:
            log.exception("Command /%s failed", cmd)
            self.send("<b>/%s failed:</b> %s" % (esc(cmd), esc(exc)))

    # -- commands: info ------------------------------------------------------
    def cmd_ref(self, args, msg):
        self.send("Purchase referral: <code>https://pulsedmedia.com/clients/aff.php?aff=%s</code>"
                  % esc(self.cfg.AFFILIATE_ID))

    def cmd_help(self, args, msg):
        lines = ["<b>Pulsed sniper</b> - commands", ""]
        lines += ["<code>%s</code> - %s" % (esc(c), esc(d)) for c, d in HELP]
        self.send("\n".join(lines))

    def cmd_id(self, args, msg):
        self.send("Chat id: <code>%s</code>" % esc((msg.get("chat") or {}).get("id")))

    def cmd_status(self, args, msg):
        s = self.rt.snapshot()
        targets = self.store.all()
        if s["paused"]:
            head = "⏸ <b>PAUSED</b>"
        elif s["session_ok"] is False:
            head = "⚠️ <b>RUNNING - NO SESSION</b>"
        else:
            head = "✅ <b>RUNNING</b>"

        session = {True: "authenticated", False: "<b>NOT authenticated</b>",
                   None: "unknown"}[s["session_ok"]]
        lines = [
            head,
            "Uptime %s · %d cycles · %d checks" % (
                duration(s["uptime"]), s["cycles"], s["checks"]),
            "Last cycle: %s · every %ss (+/-%ss)" % (
                ago(s["last_cycle_at"]), self.cfg.POLL_SECONDS, self.cfg.JITTER_SECONDS),
            "Orders: <b>%s</b>" % ("DRY RUN - nothing will be bought"
                                   if s["dry_run"] else "ARMED - will buy for real"),
            "Session: %s (%s)" % (session, ago(s["session_checked_at"])),
        ]
        if s["last_error"]:
            lines.append("Last error: <code>%s</code>" % esc(s["last_error"])[:200])

        lines.append("")
        lines.append("<b>Watching %d</b>" % len([t for t in targets if not t.done]))
        lines += self._target_lines(targets)

        recent = self.rt.recent(6)
        if recent:
            lines.append("")
            lines.append("<b>Latest</b>")
            for ts, ev in recent:
                lines.append("• %s - %s" % (
                    time.strftime("%H:%M", time.localtime(ts)), esc(ev)))
        self.send("\n".join(lines))

    def _target_lines(self, targets):
        out = []
        for t in targets:
            stock = "?" if t.in_stock is None else ("IN STOCK" if t.in_stock else "out")
            bits = ["%s · %s" % (t.state(), stock), "owned %s" % t.goal()]
            clash = username_conflict(t, self.store, self.cfg)
            if clash:
                bits.append("<b>needs /user %d</b>" % t.pid)
            else:
                free = free_usernames(t, self.store, self.cfg)
                short = username_shortfall(t, self.store, self.cfg)
                bits.append("as %s%s" % (
                    esc(free[0]),
                    " (+%d ready)" % (len(free) - 1) if len(free) > 1 else ""))
                if short:
                    bits.append("<b>%d name(s) short</b>" % short)
            bits.append("every %ds" % t.every(self.cfg))
            if t.last_error:
                bits.append("err: %s" % esc(t.last_error)[:60])
            out.append("• <code>%d</code> <b>%s</b>\n   %s · checked %s" % (
                t.pid, esc(t.name or "(unresolved)"), " · ".join(bits),
                ago(t.last_checked)))
        return out or ["  (nothing on the watch list - /watch to add one)"]

    def cmd_list(self, args, msg):
        targets = self.store.all()
        if not targets:
            self.send("Watch list is empty. <code>/products</code> to browse, "
                      "then <code>/watch &lt;pid&gt;</code>.")
            return
        lines = ["<b>Watch list</b> (checked in this order)", ""]
        lines += self._target_lines(targets)
        self.send("\n".join(lines))

    @staticmethod
    def _short(p):
        """Name, stock and price; the pid is rendered separately."""
        stock = "stock n/a" if p.qty is None else (
            "%d in stock" % p.qty if p.qty > 0 else "OUT")
        return " - ".join(x for x in (p.name, stock, p.price) if x)

    def cmd_products(self, args, msg):
        term = " ".join(args)
        if term:
            hits = self.catalog.search(term)
            if not hits:
                self.send("Nothing in the store matches %s." % esc(term))
                return
            lines = ["<b>Store - %s</b>" % esc(term), ""]
            lines += ["<code>%d</code> %s" % (p.pid, esc(self._short(p))) for p in hits]
        else:
            groups = self.catalog.by_category()
            if not groups:
                self.send("Could not read the store right now. %s"
                          % esc(self.catalog.error or ""))
                return
            lines = ["<b>Store</b> - <code>/watch &lt;pid&gt;</code> to snipe one", ""]
            for cat in sorted(groups):
                lines.append("<b>%s</b>" % esc(cat.replace("-", " ")))
                for p in groups[cat]:
                    lines.append("  <code>%d</code> %s" % (p.pid, esc(self._short(p))))
                lines.append("")
        self.send("\n".join(lines))

    def cmd_stock(self, args, msg):
        if not args or not args[0].isdigit():
            self.send("Usage: <code>/stock &lt;pid&gt;</code>")
            return
        p = self.catalog.get(args[0], force=True)
        if not p:
            self.send("No product with id %s in the store." % esc(args[0]))
            return
        self.send("<b>%s</b>\nid <code>%d</code> · %s · %s\n%s" % (
            esc(p.name), p.pid,
            "stock n/a" if p.qty is None else ("%d available" % p.qty),
            esc(p.price), esc(p.url)))

    def cmd_log(self, args, msg):
        n = int(args[0]) if args and args[0].isdigit() else 15
        n = max(1, min(n, 60))
        path = getattr(self.cfg, "LOG_FILE", "")
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    tail = fh.readlines()[-n:]
                if tail:
                    body = esc("".join(tail))[-(MAX_MSG - 40):]
                    self.send("<b>Log</b>\n<pre>%s</pre>" % body)
                    return
            except OSError as exc:
                log.warning("Could not read log file: %s", exc)
        events = self.rt.recent(n)
        if not events:
            self.send("Nothing logged yet.")
            return
        self.send("<b>Recent events</b>\n" + "\n".join(
            "%s  %s" % (time.strftime("%H:%M:%S", time.localtime(ts)), esc(e))
            for ts, e in events))

    # -- commands: watch list ------------------------------------------------
    def cmd_watch(self, args, msg):
        if not args:
            self.send("Usage: <code>/watch &lt;pid&gt; [usernames...] [how "
                      "many] [cycle]</code>\n"
                      "Sets everything in one go, so it never orders before you "
                      "have chosen the usernames and billing cycle.\n\n"
                      "e.g. <code>/watch 354 mybox 12</code>  "
                      "(username mybox, 12-month billing)\n"
                      "Billing = months: <b>1 3 6 12 24 36</b>. "
                      "To buy several boxes use <code>x2</code> and give "
                      "<b>one username per box</b> - Pulsed Media will not take "
                      "the same one twice:\n"
                      "<code>/watch 354 mybox mybox2 x2 12</code>\n"
                      "Usernames, count and cycle can come in any order; only "
                      "the pid is required. Also works by name: "
                      "<code>/watch storage 16TB x2</code>.")
            return

        query, usernames, count, cycle, err = parse_watch_args(args)
        if err:
            self.send("Could not read that /watch: %s.\nTry "
                      "<code>/watch 354 mybox mybox2 x2 12</code> (buy 2, a "
                      "username each, 12-month billing)." % esc(err))
            return

        if query[0].isdigit():
            product = self.catalog.get(query[0], force=True)
            if not product:
                self.send("No product with id %s in the store. /products to browse."
                          % esc(query[0]))
                return
        else:
            hits = self.catalog.search(" ".join(query))
            if not hits:
                self.send("Nothing matches %s. /products to browse."
                          % esc(" ".join(query)))
                return
            if len(hits) > 1:
                lines = ["%d products match - pick one by id:" % len(hits), ""]
                lines += ["<code>/watch %d</code> - %s" % (p.pid, esc(self._short(p)))
                          for p in hits[:20]]
                self.send("\n".join(lines))
                return
            product = hits[0]

        if self.store.get(product.pid):
            self.send("Already watching <b>%s</b> (id %d). "
                      "<code>/qty %d &lt;n&gt;</code>, <code>/user</code> or "
                      "<code>/cycle</code> to change it."
                      % (esc(product.name), product.pid, product.pid))
            return

        # Reject a clashing username up front instead of parking an un-orderable
        # target - the whole point of setting it here is to be ready to order.
        if usernames:
            claimed = claimed_usernames(self.store, self.cfg)
            taken = [(n, claimed[n.lower()]) for n in usernames
                     if n.lower() in claimed]
            if taken:
                self.send("Already in use: %s.\nPick other names and run "
                          "/watch again."
                          % "; ".join("<b>%s</b> (%s)" % (esc(n), esc(by))
                                      for n, by in taken))
                return

        count = max(1, count or 1)
        # Username and cycle are set on the Target before it is added, so the
        # poll loop can never see it in an orderable-but-unconfigured state.
        self.store.add(Target(
            pid=product.pid, name=product.name, url=product.url,
            category_url=product.category_url, want=None, buy=count,
            usernames=usernames, cycle=cycle,
        ))
        self.rt.event("watch added: %s (pid %d, buy %d, users %s, cycle %s)"
                      % (product.name, product.pid, count,
                         ", ".join(usernames) or "default", cycle or "default"))
        self.rt.wake.set()
        stock = "stock n/a" if product.qty is None else ("%d available" % product.qty)

        lines = ["Now sniping <b>%s</b>" % esc(product.name),
                 "id <code>%d</code> · %s · %s" % (product.pid, stock,
                                                   esc(product.price)),
                 "Username%s: <b>%s</b>%s" % (
                     "s" if len(usernames) > 1 else "",
                     esc(", ".join(usernames)) if usernames
                     else "default (PM_USERNAME)",
                     "" if usernames else " ⚠️ set one with /user"),
                 "Billing: <b>%s</b>%s" % (
                     esc(cycle_label(cycle)) if cycle else "monthly (default)",
                     "" if cycle in (None, "monthly") else
                     " - whole term charged upfront from credit"),
                 "Will buy <b>%d</b> more, then stop." % count]

        # Only warn about a clash when a real username is in play. The empty
        # case is already covered by the "set one with /user" note above.
        watched = self.store.get(product.pid)
        if effective_username(watched, self.cfg):
            clash = username_conflict(watched, self.store, self.cfg)
            if clash:
                lines.append("\n⚠️ No free username: %s. Send "
                             "<code>/user %d &lt;name&gt; [more names]</code> or "
                             "it will not order." % (esc(clash), product.pid))
            elif count > len(free_usernames(watched, self.store, self.cfg)):
                # One name per box: without this the target buys as many boxes
                # as it has names and then stalls waiting to be topped up.
                lines.append("\n⚠️ %d box(es) wanted but only %d "
                             "username(s) given - each box needs its own. Send "
                             "<code>/user %d &lt;name&gt; [more names]</code>."
                             % (count,
                                len(free_usernames(watched, self.store, self.cfg)),
                                product.pid))
        if self.rt.snapshot()["dry_run"]:
            lines.append("\n⚠️ Dry run is on - it will not actually order. "
                         "/arm to change.")
        self.send("\n".join(lines))

    # -- commands: guided setup ---------------------------------------------
    def cmd_setup(self, args, msg):
        """Add a target by conversation instead of /watch syntax."""
        if self.setup is not None and not self.setup.stale():
            self.send("Dropping the /setup that was already open.")
        self.setup = Setup()
        self.send("%s\nSend a product id (<code>355</code>) or part of its name "
                  "(<code>storage 16TB</code>).\n\n"
                  "I will ask for the count, a username per box and the billing "
                  "term, then show you the lot before anything is added. "
                  "<code>/cancel</code> stops it at any point."
                  % self.setup.heading("what do you want to snipe?"))
        if args:                    # /setup 355 answers the first question
            self._setup_reply(" ".join(args))

    def cmd_cancel(self, args, msg):
        if self.setup is None:
            self.send("Nothing to cancel.")
            return
        self.setup = None
        self.send("Setup cancelled - nothing was added.")

    def _setup_reply(self, text):
        """Feed a plain-text reply to an open /setup. True if it was consumed."""
        w = self.setup
        if w is None:
            return False
        if w.stale():
            self.setup = None
            self.send("That /setup went quiet for a while, so I dropped it - "
                      "nothing was added. <code>/setup</code> starts again.")
            return True
        if text.strip().lower() in ("cancel", "stop", "quit"):
            self.setup = None
            self.send("Setup cancelled - nothing was added.")
            return True
        w.touched = time.time()
        try:
            getattr(self, "_setup_" + w.step)(w, text.strip())
        except Exception as exc:              # a step must never kill the thread
            log.exception("/setup step %s failed", w.step)
            self.setup = None
            self.send("<b>Setup failed:</b> %s\nNothing was added." % esc(exc))
        return True

    def _setup_product(self, w, text):
        tokens = text.split()
        if w.candidates and tokens[0].isdigit():
            # Answering the "pick one" list: accept the row number or the pid.
            n = int(tokens[0])
            by_row = w.candidates[n - 1] if 1 <= n <= len(w.candidates) else None
            product = next((p for p in w.candidates if p.pid == n), by_row)
            if product is None:
                self.send("Pick one of the numbers above, or send a different "
                          "search.")
                return
        elif tokens[0].isdigit():
            product = self.catalog.get(tokens[0], force=True)
            if not product:
                self.send("No product with id <code>%s</code> in the store. Send "
                          "another id or a name - /products lists them."
                          % esc(tokens[0]))
                return
        else:
            hits = self.catalog.search(text)
            if not hits:
                self.send("Nothing matches <b>%s</b>. Try fewer words, or "
                          "/products to browse." % esc(text))
                return
            if len(hits) > 1:
                w.candidates = hits[:20]
                lines = ["%d products match - send the number or the id:"
                         % len(hits), ""]
                lines += ["<b>%d.</b> <code>%d</code> %s" % (i, p.pid,
                                                             esc(self._short(p)))
                          for i, p in enumerate(w.candidates, 1)]
                self.send("\n".join(lines))
                return
            product = hits[0]

        if self.store.get(product.pid):
            self.setup = None
            self.send("Already watching <b>%s</b> (id %d) - nothing to set up. "
                      "Use <code>/qty %d</code>, <code>/user %d</code> or "
                      "<code>/cycle %d</code> to change it."
                      % (esc(product.name), product.pid, product.pid,
                         product.pid, product.pid))
            return

        w.product, w.candidates = product, []
        w.step = "count"
        stock = "stock n/a" if product.qty is None else ("%d available" % product.qty)
        self.send("%s\n<b>%s</b>\nid <code>%d</code> · %s · %s\n\n"
                  "%s\nHow many do you want to buy? Send a number (1-10)."
                  % (w.heading("how many?"), esc(product.name), product.pid,
                     stock, esc(product.price),
                     "Each one is a separate order placed on its own drop."))

    def _setup_count(self, w, text):
        token = text.lstrip("xX*")
        if not token.isdigit() or not 1 <= int(token) <= 10:
            self.send("Send a plain number between 1 and 10.")
            return
        w.count = int(token)
        w.step = "usernames"
        self.send("%s\nPulsed Media will not take the same seedbox username "
                  "twice, so <b>%d box(es) needs %d name(s)</b>.\n\n"
                  "Send them in one message, separated by spaces or commas:\n"
                  "<code>boxa boxb boxc</code>"
                  % (w.heading("usernames"), w.count, w.count))

    def _setup_usernames(self, w, text):
        fresh = normalise_names(text)
        if not fresh:
            self.send("I did not read a username in that. Try "
                      "<code>boxa boxb</code>.")
            return

        # The same checks /user makes, so the wizard cannot produce a target
        # that is refused the moment stock lands.
        claimed = claimed_usernames(self.store, self.cfg)
        taken = [(n, claimed[n.lower()]) for n in fresh if n.lower() in claimed]
        if taken:
            self.send("Already in use: %s.\nSend different name(s)."
                      % "; ".join("<b>%s</b> (%s)" % (esc(n), esc(by))
                                  for n, by in taken))
            return

        w.usernames = normalise_names(w.usernames + fresh)
        missing = w.count - len(w.usernames)
        if missing > 0:
            self.send("Got %s. <b>%d more</b> needed - send them."
                      % (", ".join("<b>%s</b>" % esc(n) for n in w.usernames),
                         missing))
            return

        w.step = "cycle"
        spare = len(w.usernames) - w.count
        self.send("%s\nUsernames: <b>%s</b>%s\n\n"
                  "Billing term? Send the number of months: <b>1 3 6 12 24 36</b>."
                  "\nAnything but monthly is charged for the whole term upfront, "
                  "out of your account credit."
                  % (w.heading("billing term"),
                     esc(", ".join(w.usernames)),
                     "" if not spare else " (%d spare)" % spare))

    def _setup_cycle(self, w, text):
        cycle = normalize_cycle(text.split()[0])
        if not cycle:
            self.send("I did not recognise that term. Send one of "
                      "<b>1 3 6 12 24 36</b> (months).")
            return
        w.cycle, w.step = cycle, "confirm"
        armed = ("<b>ARMED</b> - it will spend real credit"
                 if not self.rt.snapshot()["dry_run"]
                 else "dry run - it will stop short of paying")
        self.send("%s\n<b>%s</b>\nid <code>%d</code>\nBuy: <b>%d</b>\n"
                  "Usernames: <b>%s</b>\nBilling: <b>%s</b>\nOrders: %s\n\n"
                  "Send <b>yes</b> to start sniping it, or /cancel."
                  % (w.heading("confirm"), esc(w.product.name), w.product.pid,
                     w.count, esc(", ".join(w.usernames)),
                     esc(cycle_label(w.cycle)), armed))

    def _setup_confirm(self, w, text):
        if text.lower() not in ("y", "yes", "ok", "go", "do it", "confirm"):
            self.send("Send <b>yes</b> to add it, or /cancel to drop it.")
            return
        if self.store.get(w.product.pid):     # added by hand while we talked
            self.setup = None
            self.send("Something already added <b>%s</b> while we were talking - "
                      "left it alone." % esc(w.product.name))
            return

        self.store.add(Target(
            pid=w.product.pid, name=w.product.name, url=w.product.url,
            category_url=w.product.category_url, want=None, buy=w.count,
            usernames=w.usernames, cycle=w.cycle,
        ))
        self.rt.event("watch added via /setup: %s (pid %d, buy %d, users %s, "
                      "cycle %s)" % (w.product.name, w.product.pid, w.count,
                                     ", ".join(w.usernames), w.cycle))
        self.rt.wake.set()
        pid, name = w.product.pid, w.product.name
        self.setup = None

        lines = ["✅ Now sniping <b>%s</b> (id <code>%d</code>)" % (esc(name), pid),
                 "Buying <b>%d</b>, one per drop, as <b>%s</b>." % (
                     w.count, esc(", ".join(w.usernames))),
                 "Billing <b>%s</b>." % esc(cycle_label(w.cycle)),
                 "", "/status to watch it, /list for the whole list."]
        if self.rt.snapshot()["dry_run"]:
            lines.append("\n⚠️ Dry run is on - it will not actually "
                         "order. /arm to change.")
        if self.rt.snapshot()["session_ok"] is False:
            lines.append("\n⚠️ No WHMCS session - it can watch but not "
                         "order. Send /cookie &lt;value&gt;.")
        self.send("\n".join(lines))

    def cmd_unwatch(self, args, msg):
        if not args or not args[0].isdigit():
            self.send("Usage: <code>/unwatch &lt;pid&gt;</code>")
            return
        t = self.store.get(args[0])
        if not self.store.remove(args[0]):
            self.send("Not on the watch list.")
            return
        self.rt.event("watch removed: pid %s" % args[0])
        self.send("Stopped watching <b>%s</b> (id %s)."
                  % (esc(t.name if t else ""), esc(args[0])))

    def _edit(self, args, usage, apply_fn):
        if len(args) < 2 or not args[0].isdigit():
            self.send("Usage: <code>%s</code>" % usage)
            return None
        t = self.store.get(args[0])
        if not t:
            self.send("Not watching id %s. /list to see the watch list." % esc(args[0]))
            return None
        apply_fn(t, args[1:])
        self.store.save()
        return t

    def cmd_qty(self, args, msg):
        def apply(t, rest):
            t.buy = max(1, int(rest[0]))
            t.want = None          # recomputed from what the account holds now
            t.done = False
        try:
            t = self._edit(args, "/qty &lt;pid&gt; &lt;n&gt;", apply)
        except ValueError:
            self.send("That is not a number.")
            return
        if t:
            self.rt.wake.set()
            self.send("Will buy <b>%d</b> more of <b>%s</b> from now."
                      % (t.buy, esc(t.name)))

    def cmd_max(self, args, msg):
        def apply(t, rest):
            t.max_total = float(rest[0].replace(",", ".").lstrip("€"))
        try:
            t = self._edit(args, "/max &lt;pid&gt; &lt;eur&gt;", apply)
        except ValueError:
            self.send("That is not a price.")
            return
        if t:
            self.send("Price ceiling for <b>%s</b> is now %.2f/month - it aborts "
                      "if the cart's per-month cost is above that (the whole-term "
                      "total is divided by the cycle's months)."
                      % (esc(t.name), t.max_total))

    def cmd_cycle(self, args, msg):
        if len(args) >= 2 and args[0].isdigit() and not normalize_cycle(args[1]):
            self.send("Unknown billing cycle %s. Use months <b>1 3 6 12 24 36</b> "
                      "or a name (%s)." % (esc(args[1]), ", ".join(CYCLES)))
            return
        t = self._edit(args, "/cycle &lt;pid&gt; &lt;1|3|6|12|24|36&gt;",
                       lambda t, rest: setattr(t, "cycle", normalize_cycle(rest[0])))
        if t:
            warn = ("" if t.cycle == "monthly" else
                    "\n⚠️ Non-monthly charges the whole term upfront from credit.")
            self.send("Billing cycle for <b>%s</b> is now <b>%s</b>.%s"
                      % (esc(t.name), esc(cycle_label(t.cycle)), warn))

    def cmd_user(self, args, msg):
        """Set or extend the target's username pool - one name per box."""
        if len(args) < 2 or not args[0].isdigit():
            self.send("Usage: <code>/user &lt;pid&gt; &lt;name&gt; [more "
                      "names]</code>\n"
                      "Each box needs its own - Pulsed Media will not take the "
                      "same seedbox username twice, so buying 3 boxes needs 3 "
                      "names:\n<code>/user 355 boxa boxb boxc</code>\n"
                      "Add to the pool instead of replacing it with a leading "
                      "<code>+</code>: <code>/user 355 + boxd</code>")
            return
        t = self.store.get(args[0])
        if not t:
            self.send("Not watching id %s. /list to see the watch list." % esc(args[0]))
            return

        rest = args[1:]
        append = False
        if rest[0] == "+":
            append, rest = True, rest[1:]
        elif rest[0].startswith("+") and len(rest[0]) > 1:
            append, rest = True, [rest[0][1:]] + list(rest[1:])
        wanted = normalise_names(rest)
        if not wanted:
            self.send("Give me at least one username.")
            return

        # Names already spent on a box cannot come back, and one held by another
        # target would make both of them unorderable.
        claimed = claimed_usernames(self.store, self.cfg, exclude_pid=t.pid)
        taken = [(n, claimed[n.lower()]) for n in wanted if n.lower() in claimed]
        if taken:
            self.send("Already in use: %s.\nPick other names."
                      % "; ".join("<b>%s</b> (%s)" % (esc(n), esc(by))
                                  for n, by in taken))
            return

        t.usernames = normalise_names(list(t.usernames) + wanted) if append \
            else wanted
        t.last_error = None
        self.store.save()
        self.rt.wake.set()

        free = free_usernames(t, self.store, self.cfg)
        short = username_shortfall(t, self.store, self.cfg)
        lines = ["New boxes for <b>%s</b> will be created as: <b>%s</b>"
                 % (esc(t.name), esc(", ".join(free)) or "(none free)"),
                 "%d free name(s) for %d box(es) still wanted."
                 % (len(free), remaining_wanted(t))]
        lines.append("⚠️ %d more name(s) needed." % short if short
                     else "It can order now.")
        self.send("\n".join(lines))

    def cmd_speed(self, args, msg):
        """How often each target is checked. Lower = found sooner, more requests."""
        if not args:
            lines = ["<b>Check interval</b>", ""]
            for t in self.store.all():
                lines.append("<code>%d</code> %s - every <b>%ds</b>"
                             % (t.pid, esc(t.name), t.every(self.cfg)))
            lines.append("")
            lines.append("<code>/speed 5</code> for all, "
                         "<code>/speed 355 3</code> for one.")
            self.send("\n".join(lines))
            return

        if len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
            targets, seconds = [self.store.get(args[0])], int(args[1])
            if not targets[0]:
                self.send("Not watching id %s." % esc(args[0]))
                return
        elif args[0].isdigit():
            targets, seconds = self.store.all(), int(args[0])
        else:
            self.send("Usage: <code>/speed &lt;seconds&gt;</code> or "
                      "<code>/speed &lt;pid&gt; &lt;seconds&gt;</code>")
            return

        if seconds < 2:
            self.send("2 seconds is the floor. Below that the gain is noise and "
                      "an IP block costs you the box you are waiting for.")
            return
        for t in targets:
            t.interval = seconds
            t.next_check = 0            # check on the next pass, not later
        self.store.save()
        self.rt.wake.set()

        per_day = int(86400 / seconds) * len(self.store.active())
        warn = ("" if seconds >= 5 else
                "\n\n⚠️ Under 5s is a lot of traffic for a small host. "
                "Worth it only if you expect a drop right now.")
        self.send("Checking every <b>%ds</b> (%d target(s)).\n"
                  "Found within ~%ds of stock appearing; about %s requests a day.%s"
                  % (seconds, len(targets), seconds, "{:,}".format(per_day), warn))

    def cmd_top(self, args, msg):
        if not args or not args[0].isdigit():
            self.send("Usage: <code>/top &lt;pid&gt;</code>")
            return
        if not self.store.move_to_front(args[0]):
            self.send("Not on the watch list.")
            return
        self.send("id %s is now checked first each cycle." % esc(args[0]))

    # -- commands: control ---------------------------------------------------
    def cmd_pause(self, args, msg):
        if args and args[0].isdigit():
            t = self.store.get(args[0])
            if not t:
                self.send("Not watching id %s." % esc(args[0]))
                return
            t.enabled = False
            self.store.save()
            self.rt.event("paused pid %s" % args[0])
            self.send("Paused <b>%s</b>." % esc(t.name))
            return
        self.rt.paused = True
        self.rt.event("paused (all)")
        self.send("⏸ Paused. Nothing will be checked or ordered until /resume.")

    def cmd_resume(self, args, msg):
        if args and args[0].isdigit():
            t = self.store.get(args[0])
            if not t:
                self.send("Not watching id %s." % esc(args[0]))
                return
            t.enabled = True
            t.done = False
            self.store.save()
            self.rt.event("resumed pid %s" % args[0])
            self.rt.wake.set()
            self.send("Resumed <b>%s</b>." % esc(t.name))
            return
        self.rt.paused = False
        self.rt.event("resumed (all)")
        self.rt.wake.set()
        self.send("▶ Resumed.")

    def cmd_check(self, args, msg):
        self.rt.wake.set()
        self.send("Running a cycle now - /status in a few seconds.")

    def cmd_arm(self, args, msg):
        if not self.rt.snapshot()["dry_run"]:
            self.send("Already armed - real orders are enabled.")
            return
        confirmed = args and args[0].lower() in ("yes", "confirm", "y")
        if not confirmed:
            self._arm_asked_at = time.time()
            self.send("⚠️ Arming lets the bot spend your account credit for real.\n"
                      "Send <code>/arm yes</code> within 2 minutes to confirm.")
            return
        if time.time() - self._arm_asked_at > ARM_CONFIRM_WINDOW:
            self.send("Confirmation expired. Send /arm again.")
            return
        self.rt.submit("dry_run", value=False)
        self.send("Armed. The next in-stock hit gets ordered for real.")

    def cmd_disarm(self, args, msg):
        self.rt.submit("dry_run", value=True)
        self.send("Dry run is back on - it will walk the flow but not order.")

    def cmd_cookie(self, args, msg):
        if not args:
            self.send("Usage: <code>/cookie &lt;WHMCS session value&gt;</code>\n"
                      "Log in on the VPS browser, copy the <code>%s</code> cookie, "
                      "send it here. I delete the message afterwards."
                      % esc(self.cfg.COOKIE_NAME))
            return
        self.rt.submit("cookie", value=" ".join(args))
        try:
            self.api("deleteMessage", chat_id=msg["chat"]["id"],
                     message_id=msg["message_id"])
        except requests.RequestException:
            self.send("(Could not delete your message - delete it yourself, "
                      "it holds a live session.)")
        self.send("Cookie received, testing it...")

    def cmd_stop(self, args, msg):
        self.rt.event("stop requested from Telegram")
        self.send("Shutting down. Start it again on the VPS with start.bat.")
        self.rt.submit("stop")
