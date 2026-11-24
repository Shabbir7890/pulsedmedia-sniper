"""Offline drive of the /setup wizard: no Telegram, no network, no orders.

It builds a TelegramBot against a stub catalog, replaces send() with a capture
list, and pushes messages through _handle() exactly as the update loop does.

Run it from the project directory: `python test_setup.py`.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from catalog import Product
from runtime import Runtime
from targets import TargetStore
import tgbot

CHAT = "999"
fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (("  -> " + detail) if detail else ""))
    if not ok:
        fails.append(label + (" -> " + detail if detail else ""))


class Cfg(object):
    USERNAME = "mybox"
    TG_TOKEN = "stub"
    TG_CHAT = CHAT
    TIMEOUT = 5
    POLL_SECONDS = 5
    JITTER_SECONDS = 1


class StubCatalog(object):
    """Stands in for the live storefront crawl."""

    def __init__(self, products):
        self.products = products

    def get(self, pid, force=False):
        return next((p for p in self.products if p.pid == int(pid)), None)

    def search(self, term):
        term = term.lower()
        return [p for p in self.products
                if all(w in p.name.lower() for w in term.split())]


def product(pid, name):
    return Product(pid=pid, name=name, url="https://example/%d" % pid,
                   category="cat", category_url="https://example/cat",
                   qty=0, price="9.99EUR")


def new_bot(products, targets=None):
    path = os.path.join(tempfile.mkdtemp(), "targets.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"targets": targets or []}, fh)
    store = TargetStore(path)
    rt = Runtime()
    rt.dry_run = False
    rt.session_ok = True
    bot = tgbot.TelegramBot(Cfg(), store, rt, notifier=None,
                            catalog=StubCatalog(products))
    bot.sent = []
    bot.send = lambda text, chat=None: bot.sent.append(text)
    return bot, store


def say(bot, text):
    """One message from the authorised chat."""
    bot.sent = []
    bot._handle({"chat": {"id": CHAT}, "text": text})
    return "\n---\n".join(bot.sent)


CATALOG = [product(355, "Eternal Vainamoinen - Seedbox 8TB R5 10G"),
           product(357, "Eternal Vainamoinen - Storage 2TB R5 10G"),
           product(351, "Eternal Vainamoinen - Storage 4TB R5 10G")]


print("1. the happy path, by product id")
bot, store = new_bot(CATALOG)
out = say(bot, "/setup")
check("asks for the product", "Setup 1/5" in out)
out = say(bot, "355")
check("found it and asks how many", "Setup 2/5" in out and "8TB" in out)
out = say(bot, "3")
check("asks for 3 names", "Setup 3/5" in out and "3 name(s)" in out, out.splitlines()[1][:60])
out = say(bot, "boxa boxb boxc")
check("accepts all three, asks the term", "Setup 4/5" in out and "boxa, boxb, boxc" in out)
out = say(bot, "12")
check("confirms before doing anything", "Setup 5/5" in out and "annually" in out)
check("still nothing added", store.get(355) is None)
out = say(bot, "yes")
t = store.get(355)
check("target added on yes", t is not None)
check("  pool of three", t.usernames if t else None, str(t.usernames if t else None))
check("  buys three", (t.buy if t else 0) == 3)
check("  billing carried over", (t.cycle if t else "") == "annually")
check("wizard closed", bot.setup is None)

print("\n2. a name search that matches several")
bot, store = new_bot(CATALOG)
say(bot, "/setup")
out = say(bot, "storage")
check("lists the matches", "2 products match" in out)
out = say(bot, "2")
check("row number picks one", "Setup 2/5" in out and "4TB" in out)

print("\n3. bad answers do not advance or crash")
bot, store = new_bot(CATALOG)
say(bot, "/setup")
say(bot, "355")
out = say(bot, "twelve")
check("count rejects words", "between 1 and 10" in out and bot.setup.step == "count")
out = say(bot, "99")
check("count rejects out of range", bot.setup.step == "count")
say(bot, "2")
out = say(bot, "mybox")
check("rejects the existing box name", "Already in use" in out)
check("  and stays on the step", bot.setup.step == "usernames")
out = say(bot, "boxa")
check("one short: asks for the rest", "1 more" in out and bot.setup.step == "usernames")
out = say(bot, "boxb")
check("  then moves on", bot.setup.step == "cycle")
out = say(bot, "weekly")
check("rejects an unknown term", "did not recognise" in out and bot.setup.step == "cycle")
say(bot, "1")
out = say(bot, "maybe")
check("confirm needs a yes", "Send <b>yes</b>" in out and store.get(355) is None)

print("\n4. cancelling and other commands")
bot, store = new_bot(CATALOG)
say(bot, "/setup")
say(bot, "355")
out = say(bot, "/cancel")
check("/cancel drops it", "cancelled" in out and bot.setup is None)
check("nothing added", store.get(355) is None)
bot, store = new_bot(CATALOG)
say(bot, "/setup")
out = say(bot, "cancel")
check("the word cancel works too", bot.setup is None)
bot, store = new_bot(CATALOG)
out = say(bot, "hello")
check("plain text with no wizard still explains itself", "Not a command" in out)
bot, store = new_bot(CATALOG)
say(bot, "/setup")
say(bot, "355")
out = say(bot, "/list")
check("other commands still answer mid-wizard", "watch list" in out.lower() or "nothing" in out.lower())
check("  and the wizard is still open", bot.setup is not None and bot.setup.step == "count")

print("\n5. /setup 355 skips the first question")
bot, store = new_bot(CATALOG)
out = say(bot, "/setup 355")
check("jumps straight to the count", "Setup 2/5" in out)

print("\n6. it refuses to set up something already watched")
bot, store = new_bot(CATALOG, [{"pid": 355, "name": "8TB", "usernames": ["boxa"]}])
say(bot, "/setup")
out = say(bot, "355")
check("points at /qty instead", "Already watching" in out and bot.setup is None)

print("\n7. the command menu has no duplicate names")
names = [c.split()[0].lstrip("/") for c, _ in tgbot.HELP]
check("HELP has a repeated name to collapse", len(names) != len(set(names)))
published = []


class FakeApi(object):
    def __init__(self, bot):
        self.bot = bot

    def __call__(self, method, _timeout=None, **params):
        if method == "setMyCommands":
            published.extend(json.loads(params["commands"]))
        return {}


bot, store = new_bot(CATALOG)
bot.api = FakeApi(bot)
bot._publish_commands()
pub = [c["command"] for c in published]
check("published list is deduped", len(pub) == len(set(pub)), ", ".join(pub[:6]) + " ...")
check("setup is in the menu", "setup" in pub)

print("\n" + ("ALL PASS" if not fails else "FAILURES:\n" + "\n".join(fails)))
sys.exit(1 if fails else 0)
