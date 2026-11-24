#!/usr/bin/env python3
"""Start the sniper if it is not running.

Driven by the PulsedSniperWatchdog scheduled task. Run it with **pythonw.exe**,
which has no console: the previous PowerShell version flashed a window in the
user's face every 10 minutes.

Safe to run at any time, and safe to run twice - sniper.py holds a single
instance lock, so a spurious start exits immediately instead of producing two
armed snipers.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "watchdog.log")
LOCK = os.path.join(HERE, "sniper.lock")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def log(msg):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def sniper_running():
    """True if another process holds the sniper's lock file.

    Asking the lock is better than listing processes: it is the same thing the
    sniper itself uses to decide, so the two can never disagree.
    """
    if not os.path.exists(LOCK):
        return False
    try:
        handle = open(LOCK, "a+")
    except OSError:
        return True             # cannot tell; assume it is up rather than double-start
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False            # the lock was free, so nothing is running
    except OSError:
        return True
    finally:
        handle.close()


def load_env():
    """Same .env the manual launcher reads, so both start it identically."""
    env = os.environ.copy()
    path = os.path.join(HERE, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as exc:
        log("could not read .env: %s" % exc)
    return env


def start():
    python = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.exists(python):
        python = sys.executable
    try:
        out = open(os.path.join(HERE, "sniper.log"), "a", encoding="utf-8")
        err = open(os.path.join(HERE, "sniper.err.log"), "a", encoding="utf-8")
        proc = subprocess.Popen(
            [python, os.path.join(HERE, "sniper.py")],
            cwd=HERE, env=load_env(), stdout=out, stderr=err,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        )
    except OSError as exc:
        log("start failed: %s" % exc)
        return
    with open(os.path.join(HERE, "sniper.pid"), "w", encoding="ascii") as fh:
        fh.write(str(proc.pid))
    time.sleep(6)
    log("started, pid %d%s" % (proc.pid,
                               "" if sniper_running() else " - but it is NOT running"))


if __name__ == "__main__":
    if sniper_running():
        sys.exit(0)
    log("sniper not running - starting it")
    start()
