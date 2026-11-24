#!/usr/bin/env python3
"""Watch Pulsed Media products and order them the moment stock appears.

One process, three parts:

  * this poll loop, which owns the authenticated WHMCS session and is the only
    thing allowed to buy anything;
  * a watch list on disk (targets.json) that survives restarts;
  * a Telegram control thread (tgbot.py) that reports status and retargets the
    loop without touching the session.

See README.md.
"""
import json
import logging
import os
import random
import re
import signal
import sys
import time

import requests

import config as cfg
from catalog import Catalog
from notify import Notifier
from runtime import Runtime, ago
from targets import (TargetStore, free_usernames, next_username,
                     remaining_wanted, username_conflict, username_shortfall)
from tgbot import TelegramBot
from whmcs import CaptchaRequired, OrderError, PulsedMedia

# The product names carry Finnish vowels; a cp1252 console would mangle every
# log line and every guard message that quotes a name.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("sniper")

TAG_RE = re.compile(r"<[^>]+>")
SESSION_RETRY_SECONDS = 60

# SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_lock_handle = None


def acquire_single_instance():
    """Refuse to run if another sniper already is. Returns False if one is.

    Two armed snipers is the worst failure this program has: they fight over
    Telegram (409 Conflict, so neither answers commands), interleave writes into
    one log, and can both pass the owned-count guard on the same drop and buy
    twice. This has happened in practice when the logon and the 10-minute watchdog
    triggers both fired within 24 seconds.

    A watchdog cannot be the guarantee here - it races. The lock is held by the
    process itself, and Windows releases it however the process dies.
    """
    global _lock_handle
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sniper.lock")
    try:
        handle = open(path, "a+")
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_handle = handle        # keep it open for the life of the process
    return True


def keep_system_awake():
    """Tell Windows this process must not be idle-slept out of existence.

    Power-scheme settings are the first line of defence, but they are one
    Settings-page edit or one scheme switch away from being wrong - which is how
    an overnight watch was lost. This is the process saying it directly. The
    display is deliberately NOT held on; only the system.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        ok = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if ok:
            log.info("Holding the system awake while the sniper runs.")
            return True
        log.warning("SetThreadExecutionState was refused; relying on the power scheme.")
    except (AttributeError, OSError) as exc:
        log.warning("Could not request stay-awake: %s", exc)
    return False


class Alerts(object):
    """A Telegram notification plus a line in the /status feed."""

    def __init__(self, notifier, rt):
        self.notifier = notifier
        self.rt = rt

    def send(self, text, event=None):
        self.rt.event(event or TAG_RE.sub("", text).replace("\n", " - ")[:160])
        self.notifier.send(text)


def attempt_order(pm):
    """Hot path. The product is ALREADY staged by the polling HEAD request.

    Every step re-proves we are handling the intended product before the next
    one runs, so a stale cart or a mismatched pid aborts instead of buying
    something unintended.
    """
    soup = pm.load_staged_form()      # verifies the staged product identity
    pm.configure_product(soup)        # commits it to the cart
    if cfg.STRICT_CART_CHECK:
        pm.verify_cart()              # exactly 1x the target, nothing else
    return pm.checkout()              # verifies total, then orders


# -- session -----------------------------------------------------------------
def check_session(pm, rt):
    """Refresh rt.session_ok. None means 'could not tell' (network blip)."""
    pm.unbind()
    try:
        ok = pm.is_logged_in()
    except requests.RequestException as exc:
        log.warning("Session check failed: %s", exc)
        return rt.session_ok
    with rt.lock:
        rt.session_ok = ok
        rt.session_checked_at = time.time()
    return ok


# -- targets -----------------------------------------------------------------
def resolve_target(t, catalog):
    """Prove the pid still maps to the product we think it does.

    The catalogue is the authority for name and URL, so a pid that has been
    reassigned to another product is caught here rather than at checkout.
    """
    product = catalog.get(t.pid)
    if product is None:
        t.last_error = "not in the store catalogue"
        t.resolved = False
        return False

    stored = (t.name or "").strip().lower()
    live = product.name.strip().lower()
    if stored and stored != live and stored not in live and live not in stored:
        t.last_error = "pid %d is now %r, not %r" % (t.pid, product.name, t.name)
        t.enabled = False
        t.resolved = False
        log.error("Disabling target %d: %s", t.pid, t.last_error)
        return False

    t.name = product.name
    t.url = product.url
    t.category_url = product.category_url
    t.siblings = [p.name for p in catalog.by_category().get(product.category, [])
                  if p.pid != t.pid]
    t.last_error = None
    t.resolved = True
    return True


def refresh_owned(t, pm, rt):
    """How many matching services the account already holds.

    This is the restart-safe guard: an absolute count read from the account, so
    losing local state cannot make the bot buy a second copy of something.
    Returns None when the list could not be read - callers must then refuse to
    order rather than guess.
    """
    if rt.session_ok is not True:
        return None
    pm.bind(t)
    owned = pm.count_services()
    if owned is None:
        return None
    t.owned = owned
    if t.want is None:
        # /watch means "get me N more", pinned to what is owned right now.
        t.want = owned + max(1, t.buy)
        log.info("Target %d (%s): owns %d, goal %d", t.pid, t.name, owned, t.want)
    return owned


# -- one poll ----------------------------------------------------------------
def check_target(t, pm, rt, alerts, store):
    """One 0-byte HEAD, and the whole order flow if it comes back 302.

    Returns True when something happened that is worth writing to disk.
    """
    pm.bind(t)
    t.checks += 1
    t.last_checked = time.time()
    rt.checks += 1

    in_stock = pm.poll_in_stock()
    changed = in_stock != t.in_stock
    if changed:
        t.last_change = time.time()
        log.info("%s (pid %d): in_stock=%s", t.name, t.pid, in_stock)
        rt.event("%s: %s" % (t.name, "IN STOCK" if in_stock else "out of stock"))
    t.in_stock = in_stock
    if not in_stock:
        return changed

    log.warning("IN STOCK - %s (pid %d), product staged.", t.name, t.pid)

    if rt.session_ok is not True:
        alerts.send(
            "<b>%s is IN STOCK</b> but I have no logged-in session, so I cannot "
            "order it. Buy it by hand now:\n%s\nThen send /cookie with a fresh "
            "session value." % (t.name, t.url),
            event="%s in stock - no session, could not order" % t.name)
        return False

    owned = refresh_owned(t, pm, rt)
    if owned is None:
        t.last_error = "services list unreadable"
        log.error("Services list unreadable - skipping this attempt.")
        pm.empty_cart()               # the HEAD staged it; do not leave it at i=0
        return False
    if owned >= t.want:
        t.done = True
        # Same reason: a staged leftover would sit at cart index 0 and make the
        # NEXT target's staged-form check read the wrong product and abort.
        pm.empty_cart()
        store.save()
        alerts.send("Target met for <b>%s</b> (%d owned) - no longer watching it."
                    % (t.name, owned))
        return False

    # Every box needs its own seedbox username, so the pool is walked one name
    # per order. Nothing is bought without a name that is provably unclaimed.
    chosen = next_username(t, store, cfg)
    if chosen is None:
        clash = username_conflict(t, store, cfg)
        msg = "no free username left (%s)" % clash
        if t.last_error != msg:          # alert on the change, not every poll
            alerts.send("<b>%s is in stock but I did not order it.</b>\nEvery "
                        "username it has is taken: %s.\nPulsed Media will not "
                        "take one twice - send <code>/user %d &lt;name&gt; "
                        "[more names]</code> and I will buy it on the next drop."
                        % (t.name, clash, t.pid),
                        event="%s: out of usernames, did not order" % t.name)
        t.last_error = msg
        log.error("Refusing to order %s: %s", t.name, msg)
        pm.empty_cart()
        return False

    # Re-bind so the staged form is filled with the name actually being bought
    # rather than whichever one happens to head the pool.
    t.pending_username = chosen
    pm.bind(t)
    log.info("Ordering %s as username %r.", t.name, chosen)

    try:
        result = attempt_order(pm)
    except CaptchaRequired as exc:
        t.last_error = str(exc)
        t.pending_username = None     # nothing was bought; the name is still free
        alerts.send("Manual action needed for <b>%s</b>: %s" % (t.name, exc))
        return False
    except (OrderError, requests.RequestException) as exc:
        t.last_error = str(exc)
        t.pending_username = None
        pm.empty_cart()               # leave no half-built cart for the next try
        alerts.send("Order attempt failed for <b>%s</b>: %s" % (t.name, exc),
                    event="order failed: %s - %s" % (t.name, exc))
        return False

    t.last_error = None

    t.pending_username = None

    if result.get("cart_only"):
        alerts.send("<b>%s</b> is in your CART (total %s).\nClick Complete Order "
                    "now: %s" % (t.name, result.get("total"), result.get("url")))
        store.save()
        return True

    if result.get("dry_run"):
        pm.empty_cart()
        alerts.send("DRY RUN: reached checkout for <b>%s</b> (total %s) and stopped "
                    "there. Send /arm to place orders for real."
                    % (t.name, result.get("total")),
                    event="dry run reached checkout: %s" % t.name)
        return True

    order_no = result.get("order_number") or "(number not parsed)"
    # Recording the name is what retires it: claimed_usernames() reads the
    # order log, so the next drop for this target picks the next free name.
    t.orders.append({"order": order_no, "total": result.get("total"),
                     "username": chosen, "at": time.time()})
    t.owned = (t.owned or 0) + 1
    if t.owned >= t.want:
        t.done = True
    pm.empty_cart()
    store.save()
    tail = ""
    if not t.done:
        short = username_shortfall(t, store, cfg)
        tail = "\nStill hunting %d more." % (t.want - t.owned)
        if short:
            tail += ("\n⚠️ Only %d username(s) left in the pool - send "
                     "<code>/user %d &lt;name&gt; [more names]</code> or the next "
                     "%d will not be ordered."
                     % (len(free_usernames(t, store, cfg)), t.pid, short))
    alerts.send("<b>ORDER PLACED</b> - %s\nOrder: %s\nTotal: %s\nPaid from account "
                "credit. Username: <b>%s</b>.%s"
                % (t.name, order_no, result.get("total"), chosen, tail),
                event="ORDER PLACED: %s (%s) as %s" % (t.name, order_no, chosen))
    return True


# -- Telegram-submitted actions ----------------------------------------------
def run_actions(pm, rt, alerts, store, bot):
    """Work the control thread is not allowed to do itself: it would race the
    poller for the one authenticated session."""
    for kind, payload in rt.drain():
        try:
            if kind == "stop":
                rt.stopping = True
                log.warning("Stop requested from Telegram.")
            elif kind == "dry_run":
                cfg.DRY_RUN = bool(payload["value"])
                rt.dry_run = cfg.DRY_RUN
                log.warning("DRY_RUN set to %s from Telegram.", cfg.DRY_RUN)
                rt.event("dry run %s" % ("ON" if cfg.DRY_RUN else "OFF - armed"))
            elif kind == "cookie":
                apply_cookie(pm, rt, alerts, bot, payload["value"])
        except Exception as exc:
            # A bad command is not worth the process. Losing the poll loop means
            # missing the drop, which is the one thing this bot exists to avoid.
            log.exception("Action %r failed", kind)
            rt.event("action %s failed: %s" % (kind, exc))
            bot.send("<b>/%s failed:</b> %s" % (kind, exc))


def remember_cookie(value):
    """Persist a cookie that just authenticated, so a restart can reuse it."""
    try:
        with open(cfg.SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"cookie": value, "saved_at": time.time()}, fh)
    except OSError as exc:
        log.warning("Could not save the session cookie: %s", exc)


def saved_cookie():
    try:
        with open(cfg.SESSION_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh).get("cookie") or None
    except (OSError, ValueError):
        return None


def apply_cookie(pm, rt, alerts, bot, value):
    try:
        ok = pm.set_cookie(value)
    except requests.RequestException as exc:
        bot.send("Could not test the cookie: %s" % exc)
        return
    with rt.lock:
        rt.session_ok = ok
        rt.session_checked_at = time.time()
    if ok:
        remember_cookie(value)
        pm.empty_cart()
        rt.event("session cookie replaced - authenticated")
        bot.send("Session is live again. Back to hunting.")
        log.warning("Session cookie replaced from Telegram; authenticated.")
    else:
        rt.event("cookie rejected")
        bot.send("That cookie does not authenticate. Log in with the browser on this machine "
                 "(same IP as this bot) and copy the <code>%s</code> value again."
                 % cfg.COOKIE_NAME)


# -- main --------------------------------------------------------------------
def main():
    rt = Runtime()
    rt.dry_run = cfg.DRY_RUN

    def _handle_signal(signum, _frame):
        rt.stopping = True
        rt.wake.set()
        log.info("Signal %s received - shutting down after this cycle.", signum)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not acquire_single_instance():
        log.warning("Another sniper is already running - exiting. "
                    "(This is the single-instance lock doing its job.)")
        return

    keep_system_awake()
    notifier = Notifier(cfg)
    alerts = Alerts(notifier, rt)
    catalog = Catalog(cfg)
    store = TargetStore(cfg.TARGETS_FILE)
    store.seed_from_env(cfg)
    pm = PulsedMedia(cfg)
    bot = TelegramBot(cfg, store, rt, notifier, catalog)
    bot.start()

    log.info("Mode=%s | dry_run=%s | poll=%ss(+/-%ss) | targets=%d",
             cfg.MODE, cfg.DRY_RUN, cfg.POLL_SECONDS, cfg.JITTER_SECONDS,
             len(store.all()))
    if cfg.MODE == "cart":
        log.warning("CART MODE - the bot fills the cart and pings you to click "
                    "Complete Order. It will not place the order itself.")
    if cfg.DRY_RUN:
        log.warning("DRY RUN is ON - the final checkout POST will be skipped. "
                    "Send /arm (or set PM_DRY_RUN=false) to arm it.")

    # A dead session is no longer fatal: keep watching, keep answering /status,
    # and shout the moment stock appears so it can be bought by hand. WHMCS
    # binds sessions to an IP and they expire; exiting on that means silently
    # missing the drop, which is the one failure this bot exists to avoid.
    authed = check_session(pm, rt)
    if not authed and saved_cookie():
        # PM_COOKIE has expired, but a cookie sent through /cookie once worked.
        # Sessions outlive reboots, so this is usually the fresher of the two.
        log.info("PM_COOKIE did not authenticate - trying the saved session.")
        try:
            authed = pm.set_cookie(saved_cookie())
        except requests.RequestException as exc:
            log.warning("Saved session could not be tested: %s", exc)
        with rt.lock:
            rt.session_ok = authed
            rt.session_checked_at = time.time()

    if authed:
        pm.empty_cart()   # start from a known-empty cart so index 0 is always ours
        log.info("Authenticated.")
    else:
        alerts.send("PulsedMedia Sniper started but is <b>not logged in</b>. It will "
                    "watch stock and alert you, but cannot order until you send "
                    "/cookie with a fresh session value.",
                    event="started without a session")

    for t in store.active():
        clash = username_conflict(t, store, cfg)
        if clash:
            alerts.send("<b>%s cannot be ordered yet.</b>\nIt has no free "
                        "username: %s. Set them with <code>/user %d &lt;name&gt; "
                        "[more names]</code>."
                        % (t.name or t.pid, clash, t.pid),
                        event="pid %d needs its own username" % t.pid)
            continue
        # One name per box, so a pool shorter than the goal stalls part-way
        # through - say so now, not on the drop that cannot be taken.
        short = username_shortfall(t, store, cfg)
        if short:
            alerts.send("<b>%s</b> wants %d more box(es) but has only %d free "
                        "username(s). Send <code>/user %d &lt;name&gt; [more "
                        "names]</code> - each box needs its own."
                        % (t.name or t.pid, remaining_wanted(t),
                           len(free_usernames(t, store, cfg)), t.pid),
                        event="pid %d is %d username(s) short" % (t.pid, short))

    armed = "ARMED" if not cfg.DRY_RUN else "dry run"
    alerts.send("PulsedMedia Sniper up (%s). Watching %d target(s). /status for details."
                % (armed, len(store.active())),
                event="sniper started (%s)" % armed)

    last_session_check = time.time()
    last_owned_check = 0.0
    last_heartbeat = time.time()
    failures = 0

    while not rt.stopping:
        rt.wake.clear()
        run_actions(pm, rt, alerts, store, bot)
        if rt.stopping:
            break

        delay = cfg.POLL_SECONDS + random.uniform(0, cfg.JITTER_SECONDS)

        if rt.paused:
            rt.wake.wait(min(delay, 5))
            continue

        try:
            if time.time() - last_session_check >= cfg.KEEPALIVE_SECONDS:
                last_session_check = time.time()
                was = rt.session_ok
                now = check_session(pm, rt)
                if was is True and now is False:
                    alerts.send("PulsedMedia Sniper <b>LOST ITS SESSION</b>. It can still "
                                "see stock but cannot order. Log in with the browser on the bot's machine "
                                "and send /cookie &lt;value&gt;.",
                                event="session lost")
                elif was is False and now is True:
                    alerts.send("Session is back.", event="session restored")
            elif rt.session_ok is False and time.time() - (
                    rt.session_checked_at or 0) >= SESSION_RETRY_SECONDS:
                check_session(pm, rt)

            # Keep the owned counts warm so /status always shows real progress
            # instead of a question mark, and so the pre-order guard is never
            # reading a number for the first time under time pressure.
            refresh_owned_now = (rt.session_ok is True and
                                 time.time() - last_owned_check >= cfg.KEEPALIVE_SECONDS)

            dirty = False
            for t in store.active():
                if rt.stopping:
                    break
                if not t.resolved:
                    # Either way the target changed on disk: it gained a URL and
                    # sibling list, or it recorded why it could not be resolved.
                    dirty = True
                    if not resolve_target(t, catalog):
                        continue
                if t.want is None or refresh_owned_now:
                    if refresh_owned(t, pm, rt) is not None:
                        dirty = True
                # Each target keeps its own schedule, so a hot one can be watched
                # closely without dragging every other target's request rate up
                # with it, and the requests spread out instead of arriving in a
                # burst followed by a long silence.
                if time.time() < t.next_check:
                    continue
                t.next_check = (time.time() + t.every(cfg)
                                + random.uniform(0, cfg.JITTER_SECONDS))
                if check_target(t, pm, rt, alerts, store) or t.done:
                    dirty = True
            pm.unbind()

            if refresh_owned_now:
                last_owned_check = time.time()
            rt.cycles += 1
            rt.last_cycle_at = time.time()
            failures = 0
            with rt.lock:
                rt.last_error = None
            if dirty or rt.cycles % 10 == 0:
                store.save()
            if time.time() - last_heartbeat >= cfg.HEARTBEAT_SECONDS:
                last_heartbeat = time.time()
                changes = [t.last_change or 0 for t in store.all()]
                log.info("Still watching (%d checks, %d target(s), last change %s)",
                         rt.checks, len(store.active()),
                         ago(max(changes) if changes else 0))

            # Sleep only until the next target is due.
            upcoming = [t.next_check for t in store.active()]
            if upcoming:
                delay = max(0.25, min(upcoming) - time.time())
            else:
                log.info("Nothing left to watch - idling. /watch to add a target.")
                delay = max(delay, 30)

        except Exception as exc:
            # Includes RequestException and OrderError. Anything unexpected backs
            # off and retries rather than taking the watcher down with it.
            if not isinstance(exc, (requests.RequestException, OrderError)):
                log.exception("Unexpected error in poll cycle")
            failures += 1
            with rt.lock:
                rt.last_error = str(exc)
            delay = min(cfg.POLL_SECONDS * (2 ** min(failures, 6)), cfg.MAX_BACKOFF)
            delay += random.uniform(0, cfg.JITTER_SECONDS)
            log.warning("Cycle failed (%d in a row): %s - backing off %.0fs",
                        failures, exc, delay)
            if failures == 5:
                alerts.send("PulsedMedia Sniper: %d consecutive failures. Last: %s"
                            % (failures, exc))

        # Interruptible sleep: /check, /watch and /stop all cut it short.
        rt.wake.wait(delay)

    store.save()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
