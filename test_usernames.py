"""Offline proof that the username pool walks forward across orders.

Run it from the project directory: `python test_usernames.py`.

No network, no session: it drives targets.py directly the way sniper.py does.
"""
import json, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import targets as T


class Cfg(object):
    USERNAME = "mybox"          # the box the account already has


cfg = Cfg()
fails = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label + "  -> " + repr(got))
    if not ok:
        fails.append("%s: got %r, want %r" % (label, got, want))


def new_store(targets):
    path = os.path.join(tempfile.mkdtemp(), "targets.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"targets": targets}, fh)
    return T.TargetStore(path)


print("1. an old targets.json still loads")
store = new_store([{"pid": 355, "name": "8TB", "username": "newbox", "want": 2,
                    "owned": 1, "orders": []}])
t = store.get(355)
check("legacy username -> pool", t.usernames, ["newbox"])
check("round-trips as a list", t.to_dict()["usernames"], ["newbox"])
check("no stale key", "username" in t.to_dict(), False)

print("\n2. a pool of 3 is walked one name per order")
store = new_store([{"pid": 355, "name": "8TB", "usernames": ["boxa", "boxb", "boxc"],
                    "want": 4, "owned": 1, "orders": []}])
t = store.get(355)
check("remaining wanted", T.remaining_wanted(t), 3)
check("shortfall covered", T.username_shortfall(t, store, cfg), 0)
for expected in ("boxa", "boxb", "boxc"):
    chosen = T.next_username(t, store, cfg)
    check("next name", chosen, expected)
    # what sniper.py does around attempt_order:
    t.pending_username = chosen
    view = t.cfg_view(cfg)
    check("  form is filled with it", view.USERNAME, expected)
    t.orders.append({"order": "#%s" % expected, "username": chosen, "at": time.time()})
    t.owned += 1
    t.pending_username = None
check("pool exhausted", T.next_username(t, store, cfg), None)
check("target is done", t.owned >= t.want, True)
conflict = T.username_conflict(t, store, cfg)
check("conflict names every holder", all(n in conflict for n in ("boxa", "boxb", "boxc")), True)

print("\n3. a short pool is flagged before the drop, not during it")
store = new_store([{"pid": 355, "name": "8TB", "usernames": ["boxa"],
                    "want": 4, "owned": 1, "orders": []}])
t = store.get(355)
check("2 names missing", T.username_shortfall(t, store, cfg), 2)
check("but it can still order now", T.next_username(t, store, cfg), "boxa")

print("\n4. names cannot be shared or reused")
store = new_store([
    {"pid": 355, "name": "8TB", "usernames": ["boxa", "mybox"], "want": 3, "owned": 1,
     "orders": []},
    {"pid": 357, "name": "2TB", "usernames": ["boxa", "boxz"], "want": 2, "owned": 0,
     "orders": []},
])
a, b = store.get(355), store.get(357)
check("355 skips the global PM_USERNAME", T.free_usernames(a, store, cfg), ["boxa"])
check("357 cannot take 355's boxa", T.free_usernames(b, store, cfg), ["boxz"])
a.orders.append({"order": "#1", "username": "boxa", "at": time.time()})
check("once 355 spends boxa it has nothing", T.next_username(a, store, cfg), None)
check("357 is unaffected", T.next_username(b, store, cfg), "boxz")

print("\n5. an empty pool falls back to PM_USERNAME and refuses")
store = new_store([{"pid": 355, "name": "8TB", "want": 2, "owned": 1, "orders": []}])
t = store.get(355)
check("conflict reported", bool(T.username_conflict(t, store, cfg)), True)
check("nothing to order with", T.next_username(t, store, cfg), None)

print("\n6. /watch parsing collects one name per box")
os.environ.setdefault("TELEGRAM_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
import tgbot
q, users, qty, cycle, err = tgbot.parse_watch_args(["355", "boxa", "boxb", "x2", "12"])
check("error", err, None)
check("usernames", users, ["boxa", "boxb"])
check("qty", qty, 2)
check("cycle", cycle, "annually")
q, users, qty, cycle, err = tgbot.parse_watch_args(["355", "u:boxa,boxb,boxc", "x3"])
check("u: list", users, ["boxa", "boxb", "boxc"])
q, users, qty, cycle, err = tgbot.parse_watch_args(["355", "boxa", "BOXA"])
check("case-duplicate dropped", users, ["boxa"])
q, users, qty, cycle, err = tgbot.parse_watch_args(["storage", "16TB", "x2"])
check("name form keeps product words", q, ["storage", "16TB"])
check("name form takes no usernames", users, [])

print("\n" + ("ALL PASS" if not fails else "FAILURES:\n" + "\n".join(fails)))
sys.exit(1 if fails else 0)
