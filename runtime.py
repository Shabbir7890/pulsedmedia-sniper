"""Shared live state between the poll loop and the Telegram control thread.

The poll loop owns the authenticated WHMCS session, so anything the Telegram
thread wants done with it is posted here as an action and executed by the loop
between polls. State reads are lock-guarded and cheap.
"""
import queue
import threading
import time
from collections import deque


def ago(ts):
    """'4m ago' style relative time, or 'never'."""
    if not ts:
        return "never"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return "%ds ago" % d
    if d < 3600:
        return "%dm ago" % (d // 60)
    if d < 86400:
        return "%dh %dm ago" % (d // 3600, (d % 3600) // 60)
    return "%dd ago" % (d // 86400)


def duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)


class Runtime(object):
    def __init__(self):
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.cycles = 0
        self.checks = 0
        self.last_cycle_at = None
        self.session_ok = None
        self.session_checked_at = None
        self.last_error = None
        self.paused = False
        self.stopping = False
        self.dry_run = None            # mirrors config, hot-swappable via /arm
        self.events = deque(maxlen=60)
        self.actions = queue.Queue()   # work for the poll loop
        self.wake = threading.Event()  # cut a sleep short

    # -- events --------------------------------------------------------------
    def event(self, text):
        with self.lock:
            self.events.appendleft((time.time(), text))

    def recent(self, n=8):
        with self.lock:
            return list(self.events)[:n]

    # -- actions -------------------------------------------------------------
    def submit(self, kind, **payload):
        """Ask the poll loop to do something that needs the WHMCS session."""
        self.actions.put((kind, payload))
        self.wake.set()

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.actions.get_nowait())
            except queue.Empty:
                return out

    def snapshot(self):
        with self.lock:
            return {
                "uptime": time.time() - self.started_at,
                "cycles": self.cycles,
                "checks": self.checks,
                "last_cycle_at": self.last_cycle_at,
                "session_ok": self.session_ok,
                "session_checked_at": self.session_checked_at,
                "last_error": self.last_error,
                "paused": self.paused,
                "dry_run": self.dry_run,
            }
