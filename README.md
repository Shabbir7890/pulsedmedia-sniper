# Pulsed Auto Order

Watches Pulsed Media products and orders them automatically from your account
credit the moment they come back in stock. Controlled from Telegram.

> **Use at your own risk.** This is an unofficial tool, not affiliated with
> Pulsed Media. It places real orders with real money when `PM_DRY_RUN=false`.
> Keep polling intervals reasonable. MIT licensed; see `LICENSE`.

## Purchase referral

Purchases are made through Pulsed Media affiliate ID `3246`, which is fixed in
`config.py`. `/ref` in Telegram shows it.

The bot visits `aff.php` using its buying session before checkout and checks
the matching `WHMCSAffiliateID` (or legacy `AffiliateID`) cookie. A missing
cookie logs a warning and continues checkout; commission cannot be confirmed.
Only `aff` is
used: `gid` is a storefront destination, and never changes the watched product.
WHMCS does not award commission on an affiliate's own account purchases:
https://docs.whmcs.com/9-0/clients/the-affiliate-system/affiliate-links/

Offline verification: `python -m unittest test_referral`.

Watches **Eternal Väinämöinen - Seedbox 8TB R5 10G** (product id `355`) and grabs
it the moment stock appears, paying from your account credit.

---

## Read this first: the captcha situation

Pulsed Media runs **reCAPTCHA v2 (the visible checkbox kind)** on some forms.
I checked each form in the flow directly:

| Form | Captcha? | Consequence |
|---|---|---|
| Login (`index.php/login`) | **YES** — `btn-recaptcha` on the submit button | Email+password **cannot** log in unattended. Use `PM_COOKIE`. |
| Add to cart (`a=confproduct`) | **No** | Always automatable. |
| Guest checkout | **YES** — in the "Spam Bot Verification" block | Irrelevant; the bot never checks out as a guest. |
| **Logged-in checkout** | **No** — verified against a real account | Fully automatic ordering works. Use `PM_MODE=order`. |

The guest captcha sits inside the new-customer registration block, and this
install has WHMCS's "disable for logged-in users" behaviour: an authenticated
checkout renders **no** captcha. That was confirmed by carting a real item on a
logged-in session and reading the resulting `#frmCheckout` — so `PM_MODE=order`
is viable and the cart-only fallback is just insurance.

Re-run **`probe_auth.py`** if Pulsed Media ever changes its captcha settings. It
carts the cheapest in-stock item, reads the real logged-in checkout form, reports
captcha yes/no, and empties the cart. It never orders anything.

### Why `PM_COOKIE` rather than a password

The captcha on the login form exists precisely to stop scripted logins, and I am
not going to work around it with a solving service. The supported path is that
**you** authenticate as a human once in your browser, and the bot reuses that
session:

1. Log in to `pulsedmedia.com/clients` in your browser.
2. DevTools → Application → Cookies → `pulsedmedia.com`.
3. Copy the value of the **`WHMCSmNqPBCNDcH8d`** cookie.
4. `heroku config:set PM_COOKIE='WHMCSmNqPBCNDcH8d=<value>'`
   (a bare value works too — the cookie name is filled in from `PM_COOKIE_NAME`).

The bot pings `clientarea.php` every 5 minutes (`PM_KEEPALIVE_SECONDS`), which
resets PHP's idle timer and keeps that session alive more or less indefinitely
while it runs. If the session does die it does **not** crash: it keeps watching
stock and sends you a "refresh PM_COOKIE" alert. Do not log out in the browser
you took the cookie from — that kills the session server-side.

### The three modes (`PM_MODE`)

| Mode | Behaviour |
|---|---|
| `order` | Detect stock → add to cart → **complete the order** from credit. Use if the probe says no captcha. |
| `cart` | Detect stock → add to cart → Telegram you a one-click checkout link. Use if the probe says captcha. |
| `auto` (default) | Try to order; if a captcha turns up at checkout, fall back to alerting you with the item already in the cart. |

Even in `cart` mode you still win the slow part: detection, login, product
configuration and cart-filling are done for you within ~a minute of restock, and
all you do is click **Complete Order**.

> One honest caveat: WHMCS decrements stock when the order *completes*, not when
> an item enters a cart. Having it in your cart does not reserve it. `order` mode
> is genuinely faster than `cart` mode — so run the probe and use `order` if you can.

---

## How it works

Polling is a single **0-byte HEAD** request to the product URL:

| Response | Meaning |
|---|---|
| `200` | Out of stock (WHMCS renders the Out of Stock page) |
| `302 -> cart.php?a=confproduct&i=0` | **In stock, and the product is now staged in your session** |

That is the whole trick: WHMCS redirects the storefront URL straight into the
configure step when stock exists, and a HEAD runs the same PHP as a GET — so
detection and staging happen in one request, and the poll transfers no body
either way.

When it fires, the order completes in four more requests:

| Step | Request | Guard applied |
|---|---|---|
| 1 | `GET cart.php?a=confproduct&i=0` | page must name the target product |
| 2 | `POST cart.php` `ajax=1&a=confproduct&…` | empty response = success; qty forced to 1 |
| 3 | `GET cart.php?a=view` | exactly **1** line, and it must be the target |
| 4 | `POST cart.php?a=checkout` `applycredit=1` | total must be under `PM_MAX_TOTAL` |

**Measured: 2.6 s from detection to order-ready** on a live in-stock product.

### It cannot order the wrong product

Five independent checks, each verified by test:

1. **Startup:** `PM_PID` is resolved against the catalog and its name must match
   `PM_PRODUCT_NAME`, or the bot refuses to start. *(Tested: pid 351 with the 8TB
   name is rejected.)*
2. **Startup:** the cart is emptied, so cart index 0 is always ours.
3. **Staging:** the poll targets one product-specific URL, so only that product
   can ever be staged.
4. **Configure:** the staged form must name the target product.
5. **Pre-checkout:** the cart must hold exactly one line, it must be the target,
   and no sibling product may be present. *(Tested: a cart deliberately loaded
   with pid 351 + 353 is rejected.)*

Any failure aborts **before** the order POST and empties the cart.

### It cannot touch your existing boxes

Audited: the entire codebase issues exactly two POSTs — add-to-cart and the
order submit. `clientarea.php` is only ever read. No cancel, upgrade, downgrade,
terminate or service-management endpoint appears anywhere in the code.

`PM_TARGET_TOTAL` counts matching services on the account and stops at the
target, re-checked immediately before ordering, so it buys exactly one more.

### Why `PM_USERNAME` exists

Custom-field ids are **per product**: pid 351 uses `customfield[575]/[576]`, pid
357 uses `[587]/[588]`. Pid 355's ids cannot be read while it is out of stock, so
hardcoding them is impossible. The bot reads the form at order time and fills
whichever field is *labelled* "Username" from `PM_USERNAME`. Verified working
against a live product.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PM_COOKIE` | — | **Set this.** Logged-in session cookie (see above) |
| `PM_COOKIE_NAME` | `WHMCSmNqPBCNDcH8d` | Session cookie name for this install |
| `PM_USERNAME` | — | **Set this.** Preferred seedbox username (required field) |
| `PM_MODE` | `auto` | `order`, `cart` or `auto` |
| `PM_DRY_RUN` | `true` | **Ships safe.** `false` to arm real ordering |
| `PM_MAX_TOTAL` | `0` | Abort if total due exceeds this. Recommended: `12` |
| `PM_PID` | `355` | Product id |
| `PM_BILLING_CYCLE` | `monthly` | `monthly` … `triennially` |
| `PM_POLL_SECONDS` / `PM_JITTER_SECONDS` | `20` / `5` | Poll pacing |
| `PM_STRICT_CART_CHECK` | `true` | Re-read the cart and prove it holds exactly 1x the target before ordering |
| `PM_KEEPALIVE_SECONDS` | `300` | Session keep-alive interval |
| `PM_EMAIL` / `PM_PASSWORD` | — | Only usable if the login captcha is ever removed |
| `PM_CUSTOMFIELDS` | `{}` | Explicit ids, e.g. `{"587":"myname"}` |
| `PM_TARGET_TOTAL` | `1` | Stop once the account holds this many matching services. Restart-safe. |
| `PM_STOP_AFTER_ORDER` | `true` | Exit once ordered |
| `PM_SKIP_OWNED_CHECK` | `false` | Disable the ownership guard entirely (not recommended) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Notifications — **strongly recommended in `cart` mode** |

---

## Setup order

```bash
pip install -r requirements.txt

# 1. No credentials, no orders — confirms the scraper still matches the site.
python selftest.py

# 2. With your cookie — settles the logged-in-checkout captcha question
#    and shows the custom fields. Places no orders.
export PM_COOKIE='WHMCSmNqPBCNDcH8d=...'
export PM_USERNAME='yourseedboxname'
python probe_auth.py

# 3. Dry run: logs in, and if stock exists fills the cart and stops.
export PM_DRY_RUN=true
python sniper.py
```

Set `PM_MODE` from what step 2 told you (currently: `order`), then arm with
`PM_DRY_RUN=false`.

## Deploy to Heroku

```bash
cd pulsed-sniper
git init && git add -A && git commit -m "Pulsed Media stock sniper"

heroku create your-app-name
heroku config:set \
  PM_COOKIE='WHMCSmNqPBCNDcH8d=...' \
  PM_USERNAME='yourseedboxname' \
  PM_MODE=auto \
  PM_MAX_TOTAL=12 \
  PM_DRY_RUN=true \
  TELEGRAM_BOT_TOKEN='123:ABC' TELEGRAM_CHAT_ID='987654321'

git push heroku main
heroku ps:scale web=0 worker=1     # worker, not web — there is no HTTP endpoint
heroku logs --tail
```

Confirm a clean `Authenticated via PM_COOKIE` and a `Still watching — 0 available`
heartbeat, then `heroku config:set PM_DRY_RUN=false`.

Notes:

* Heroku has no free tier; a worker on the **Eco** plan ($5/mo, 1000 dyno-hours)
  covers 24/7 (~720 h/month).
* Heroku cycles dynos daily, wiping `/tmp`. The guard against double-buying is
  therefore **`PM_TARGET_TOTAL`**, an absolute count read from your account
  rather than from local state: the bot counts how many matching services you
  already own and stops once that reaches the target. It re-checks immediately
  before ordering too, so a restart mid-flight cannot cause a second purchase.
  If the services list cannot be read it refuses to order rather than guess.
* Telegram: message `@BotFather` → `/newbot`, then read your chat id from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`.

---

## Being a good neighbour

Polling every 20 s sounds aggressive, but the HEAD fast path made it *lighter*
than the original 60 s design: each poll now transfers a response body of **0
bytes** instead of downloading a 70 KB catalog page. Bandwidth cost dropped by
roughly 99% even after tripling the frequency.

Errors still back off exponentially to 15 minutes. I would not go below ~10 s:
past that the gain is marginal and you start looking like something worth
rate-limiting, and an IP block would cost you the box you are waiting for.

---

## Telegram control

The bot answers commands in the same chat it sends alerts to. Only that chat id
is accepted; anything else is logged and dropped.

| Command | What it does |
|---|---|
| `/setup` | **guided walk-through** — asks for the product, count, usernames and term one question at a time |
| `/cancel` | abandon the `/setup` in progress; nothing is added |
| `/status` | running or not, uptime, session health, armed or dry run, every target's stock and progress, and the last few things that happened |
| `/list` | the watch list, in the order it is checked each cycle |
| `/watch <pid\|name> [n]` | start sniping a product — `n` is how many **more** to buy (default 1) |
| `/unwatch <pid>` | drop it from the list |
| `/products [filter]` | browse the store: ids, names, live stock, price |
| `/stock <pid>` | one-off live stock check |
| `/qty <pid> <n>` | change how many more to buy |
| `/max <pid> <eur>` | per-product price ceiling |
| `/cycle <pid> <term>` | billing cycle for that product |
| `/user <pid> <name...>` | the username **pool** for that product — one name per box, **required**, see below |
| `/user <pid> + <name...>` | add names to the pool instead of replacing it |
| `/speed [pid] <sec>` | how often to check; per target or all at once |
| `/top <pid>` | check this one first each cycle |
| `/pause` `/resume` | with a pid, one target; without, everything |
| `/check` | run a cycle immediately instead of waiting out the sleep |
| `/log [n]` | tail of `sniper.log` |
| `/arm` / `/disarm` | turn real ordering on (asks you to confirm with `/arm yes`) or back to dry run |
| `/cookie <value>` | hot-swap the WHMCS session cookie; the message is deleted afterwards |
| `/stop` | shut the process down |

`/watch` takes a name too — `/watch storage 16TB` — and lists the matches to
pick from if the name is ambiguous.

### `/setup` — the same thing without the syntax

`/watch 355 boxa boxb x2 12` is four decisions crammed into one line, and the
order of the arguments is easy to get wrong. `/setup` asks for them one at a
time instead:

```
you  /setup
bot  Setup 1/5 - what do you want to snipe?
you  355                    ← or a name: "storage 16TB", then pick from the list
bot  Setup 2/5 - how many?  (shows the product, its price and live stock)
you  3
bot  Setup 3/5 - 3 box(es) needs 3 name(s). Send them in one message.
you  boxa boxb boxc         ← validated here, not when stock lands
bot  Setup 4/5 - billing term? 1 3 6 12 24 36
you  12
bot  Setup 5/5 - confirm    (the whole thing, plus whether it is armed)
you  yes
```

- **Nothing is written until you send `yes`** — the target is built in memory
  and added at the last step, so an abandoned wizard cannot leave something
  half-configured that might order.
- Every answer is validated on the spot and a bad one just re-asks, so you
  cannot end up with a clashing username or an unknown billing term.
- `/cancel` (or just saying `cancel`) drops it. So does leaving it alone for
  ten minutes.
- Other commands still work while it is open: `/status` mid-wizard answers and
  the wizard carries on waiting.
- `/setup 355` answers the first question on the way in.

`/watch` is unchanged and still the fastest way in when you know the syntax.

### How many it buys

`/watch 355 2` means *buy two more*. The goal is pinned when the bot first reads
your services list: `want = what you own right now + 2`. That absolute number is
what the guard compares against before every order, so a restart, a crash, or a
lost state file cannot make it buy extras.

Each box is a separate order of quantity 1, placed on its own drop — the bot
never carts two at once. Buying `n` therefore needs `n` usernames; see below.

### Every box needs its own username — so give it a pool

Pulsed Media will not take the same seedbox username twice. A target therefore
holds a **list** of names and spends one per order:

```
/user 355 boxa boxb boxc      # pool of three
/user 355 + boxd              # add a fourth, keep the first three
/watch 355 boxa boxb x2 12    # or set it while adding the target
```

Before each order the bot picks the first name nothing has claimed, fills the
product's username field with **that** name, and records it against the order.
Recording it is what retires it, so the next drop moves on to the next name.
`x3` with three names is three unattended orders.

When the pool runs out the target is **refused, not ordered** — the staged cart
is emptied and you get an alert naming every holder and the `/user` command that
fixes it. It keeps watching, so topping the pool up re-arms it for the next drop.

A pool **shorter than the goal** is not an error, it is a warning: you are told
at startup, when the target is added, and after every order how many more names
are needed, while there is still time to send them. `/list` shows the name the
next order will use, how many spares are ready, and how many are missing.

A target with no pool of its own inherits `PM_USERNAME`, which is the username of
the box you already have — so it counts as claimed and that target can never
order. That is deliberate: it is the reason a bare `/watch` cannot buy anything
until you have chosen real names.

Names are checked against `PM_USERNAME`, every other target's pool, and every box
the bot has already ordered. A name listed by two targets belongs to whichever is
first in the watch list; `/watch` and `/user` reject a duplicate up front.

### Keeping it alive on a laptop

Three layers, because the first two have each already failed once:

1. **Power scheme.** On mains: sleep never, hibernate never, unattended sleep
   never, lid close does nothing. Note this laptop is a Modern Standby (S0) box,
   and the AC sleep timeout was still 4 hours despite the Settings page saying
   otherwise — check the *active scheme*, not the UI:

   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /q SCHEME_CURRENT SUB_SLEEP    # 0x00000000 = never
   ```

2. **The process asks for itself.** `sniper.py` calls `SetThreadExecutionState`
   with `ES_CONTINUOUS | ES_SYSTEM_REQUIRED` at startup, so Windows will not idle
   the machine out from under it regardless of what the scheme says. The display
   is not held on.

3. **A watchdog.** `watchdog.py` starts the sniper if it is not running, driven
   by the `PulsedSniperWatchdog` scheduled task at logon and every 10 minutes. It
   covers reboots, crashes and Windows Update restarts. Run it with
   **pythonw.exe** — a PowerShell action flashes a console window in the user's
   face every 10 minutes, and registering the task to run hidden (S4U) needs
   admin. Check it with:

   ```powershell
   Get-ScheduledTaskInfo -TaskName PulsedSniperWatchdog
   Get-Content watchdog.log -Tail 5
   ```

   Remove it with `Unregister-ScheduledTask -TaskName PulsedSniperWatchdog`.

### How fast is fast enough

A poll is one 0-byte HEAD, measured at **~140 ms** against a plain Apache with no
CDN, no rate-limit headers, and no conditional-request support — so there is no
cheaper change signal to switch to. The category page would cover every product
in one request, but it is 70 KB and does not compress, which is far worse.

Each target keeps its own schedule (`/speed [pid] <seconds>`), so requests spread
out instead of arriving in a burst. At the default 5 s:

| Interval | Found within | Requests/day (2 targets) |
|---|---|---|
| 20 s (old) | ~20 s, ~10 s average | ~8,600 |
| **5 s** | ~5 s, ~2.5 s average | ~34,000 |
| 2 s (floor) | ~2 s | ~86,000 |

Below 2 s is refused. The gain is noise at that point and an IP block costs you
the box you are waiting for — Pulsed Media is a small host, not a CDN edge.

**Interval is not the whole race, and it is not the interesting half.** The HEAD
that detects stock also *stages the product in the session*, so the order starts
already past a step other bots have to make a round trip for. Detection to
order-ready was measured at 2.6 s. A bot that finds stock 3 s sooner and then
spends 8 s walking the cart still loses.

### Removing it

```powershell
.\uninstall.ps1 -DryRun                 # show what would happen
.\uninstall.ps1                          # task, process, power settings
.\uninstall.ps1 -PurgeFiles              # ...and the state files
.\uninstall.ps1 -RestoreDefaultSchemes   # reset ALL power settings instead
```

It unregisters the watchdog task **before** stopping the sniper - the other
order just gets the sniper restarted ten minutes later.

Only one original power value was ever captured: the AC sleep timeout was 4
hours. Unattended-sleep and lid-close are restored to the usual Windows defaults
and re-hidden, which may not match what this machine had - use
`-RestoreDefaultSchemes` if you want certainty and do not mind losing unrelated
power tuning.

Three things the script deliberately does not do, because they are yours to
decide: logging out of pulsedmedia.com (which kills the session server-side),
revoking the bot token via @BotFather, and deleting the folder. `session.json`
holds a live session cookie and `.env` holds the bot token - `-PurgeFiles`
removes the first, never the second.

### Only one sniper, ever

`sniper.py` takes an exclusive lock on `sniper.lock` at startup and exits if it
cannot get it. This is not belt-and-braces — two armed snipers is the worst
failure mode this program has:

- they both long-poll Telegram, so the API kills both with `409 Conflict` and
  neither answers commands;
- they interleave writes into one log file, which is how this was found;
- they can both pass the owned-count guard on the same drop and buy twice.

It happened on 2026-08-24: the logon trigger and the 10-minute repeat trigger
both fired within 24 seconds and each started an instance. A watchdog cannot
prevent that — it races. The process holding its own lock can, and Windows
releases it however the process dies.

`watchdog.py` asks the same lock rather than listing processes, so the two can
never disagree about whether the sniper is up.

### A dead session no longer kills the bot

WHMCS binds a session to the IP that created it, and cookies expire. The bot used
to exit on that. Now it keeps watching, reports `NOT authenticated` in `/status`,
retries every minute, and if stock appears while it is logged out it tells you
immediately with the order link so you can buy by hand. Log in on the VPS browser
and send `/cookie <value>` to put it back to work — no restart, no console.

### The watch list

Lives in `targets.json` next to the script, written by both the poll loop and the
commands, and re-read at startup. On first run it is seeded from `PM_PID` /
`PM_PRODUCT_NAME` / `PM_TARGET_TOTAL`, so an existing deployment keeps hunting
exactly what it was hunting. After that the env vars are only the seed — the file
is the source of truth.

Each target is re-checked against the storefront catalogue at startup: if a pid
now resolves to a different product, that target is disabled instead of ordered.

---

## Files

```
sniper.py       polling loop, keep-alive, retry/backoff, idempotency, alerts
whmcs.py        WHMCS client — stock, cart, captcha detection, checkout
targets.py      the watch list (targets.json) and per-target config binding
catalog.py      read-only storefront crawl: pid -> name, URL, live stock
tgbot.py        Telegram command interface (/status, /watch, …)
runtime.py      live state shared between the poll loop and the command thread
config.py       environment configuration
notify.py       Telegram notifier
watchdog.py     restarts the sniper if it is not running (scheduled task)
uninstall.ps1   removes the task, the process and the power changes
selftest.py     live scraper validation (no credentials, no orders)
probe_auth.py   settles the logged-in checkout captcha question (no orders)
```
