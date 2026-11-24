# PulsedMedia Sniper

[![tests](https://github.com/Shabbir7890/pulsedmedia-sniper/actions/workflows/tests.yml/badge.svg)](https://github.com/Shabbir7890/pulsedmedia-sniper/actions/workflows/tests.yml)

Watches [Pulsed Media](https://pulsedmedia.com) seedbox products and orders them
automatically from your account credit the moment they come back in stock.
You control it from Telegram: pick products, set how many to buy, arm it, and
get a message when it orders.

> [!IMPORTANT]
> **It needs a machine with a browser, not a headless server.** Pulsed Media's
> login has a captcha, so the bot uses a session cookie from your browser, and
> that cookie only works from the IP address it was created on. Run it on your
> own Windows PC or laptop, or on a Windows VPS you use over RDP. Cloud workers,
> Docker and headless Linux servers will not work. See [Where to run it](#where-to-run-it).

> [!WARNING]
> **Use at your own risk.**
> - This is an unofficial tool, not affiliated with or endorsed by Pulsed Media.
> - Once armed it places **real orders paid from your real account credit**.
> - Automated ordering may be against Pulsed Media's terms of service and could
>   get your account limited or suspended. Read their terms and decide for yourself.
> - Provided as-is under the MIT license, with no warranty. See [`LICENSE`](LICENSE).

> [!CAUTION]
> **Your session cookie is as good as your password.** Anyone who has it can
> spend your Pulsed Media credit. It is stored in plain text in `.env` and, if you
> send it with `/cookie`, in `session.json`. Never share either file, never
> commit them, and never post logs or screenshots that show the cookie. Both
> files are already in `.gitignore`.

---

## Quick start

1. **Install** Python 3.12 and the two dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. **Make a Telegram bot** (optional, strongly recommended). Message
   `@BotFather` → `/newbot`, then get your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging your bot once.

3. **Configure.** Copy `.env.example` to `.env` and fill in:
   - `PM_COOKIE`: your session cookie ([how to get it](#getting-your-session-cookie))
   - `PM_USERNAME`: the username of a box you already own, or the name you want
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

   Leave `PM_DRY_RUN=true` for now.

4. **Dry run.** This logs in and watches, and if something is in stock it fills
   the cart and stops. It never buys.

   ```powershell
   python selftest.py      # no credentials needed: checks the site still parses
   .\run_local.ps1         # dry run in this window, Ctrl+C to stop
   ```

   You want to see `Authenticated via PM_COOKIE` and a `Still watching` heartbeat.

5. **Arm it.** Set `PM_DRY_RUN=false` in `.env` (or send `/arm` in Telegram), then
   launch it in the background with `start.bat`. Use `/setup` in Telegram to
   choose what to buy.

---

## Getting your session cookie

The login form has a reCAPTCHA, and this project deliberately does not try to
beat it. You log in once as a human, and the bot reuses that session.

1. On **the machine that will run the bot**, log in to
   `https://pulsedmedia.com/clients` in your browser.
2. Open DevTools (F12) → **Application** → **Cookies** → `pulsedmedia.com`.
3. Copy the value of the **`WHMCSmNqPBCNDcH8d`** cookie.
4. Put it in `.env` as `PM_COOKIE=WHMCSmNqPBCNDcH8d=<value>` (a bare value works
   too), or send `/cookie <value>` to the bot while it runs. The bot deletes that
   Telegram message after reading it.

The bot touches `clientarea.php` every 5 minutes to keep the session alive.
**Do not log out** in that browser: logging out kills the session for the bot too.

If the session does die (cookies expire eventually), the bot keeps running. It
reports `NOT authenticated` in `/status`, messages you, and if stock appears while
it is logged out it sends you the order link so you can buy by hand. Log in again
in the same browser and send `/cookie <value>`; no restart needed.

---

## Where to run it

Pulsed Media's WHMCS ties each login session to the IP address that created it.
A cookie copied from one IP is rejected from any other, so **the bot and the
browser you log in with must share one public IP**.

| Host | Works? |
|---|---|
| Your Windows PC or laptop, left on | **Yes** |
| Windows VPS with a desktop, used over RDP (log in with its browser) | **Yes** |
| Heroku, Railway, Render, Fly and other cloud workers | No: no browser, and the IP is not yours |
| Docker or a headless Linux server | No: you cannot log in from its IP |

Not sure two machines share an IP? Run `python tools/diag.py` on the bot's
machine and compare the IP it prints with the one your browser shows on any
"what is my IP" site.

The helper scripts (`start.bat`, `stop.bat`, `status.ps1`, `watchdog.py`,
`uninstall.ps1`) are Windows-only.

---

## Telegram commands

The bot only answers the chat in `TELEGRAM_CHAT_ID`; messages from anyone else
are logged and ignored.

| Command | What it does |
|---|---|
| `/setup` | **Guided setup.** Asks for product, count, usernames and billing term one at a time |
| `/cancel` | Abandon a `/setup` in progress; nothing is saved |
| `/status` | Running state, session health, armed or dry run, each target's stock and progress |
| `/list` | The watch list, in the order it is checked |
| `/products [filter]` | Browse the store: ids, names, live stock, price |
| `/stock <pid>` | One-off live stock check |
| `/watch <pid\|name> [n]` | Start sniping a product. `n` is how many **more** to buy (default 1) |
| `/unwatch <pid>` | Remove it from the list |
| `/qty <pid> <n>` | Change how many more to buy |
| `/max <pid> <eur>` | Price ceiling for that product |
| `/cycle <pid> <term>` | Billing cycle for that product |
| `/user <pid> <name...>` | Set the username pool for that product (one name per box) |
| `/user <pid> + <name...>` | Add names to the pool |
| `/speed [pid] <sec>` | How often to check; minimum 10 seconds |
| `/top <pid>` | Check this product first each cycle |
| `/pause` / `/resume` | With a pid, one target; without, everything |
| `/check` | Run a check right now |
| `/log [n]` | Last lines of `sniper.log` |
| `/arm` / `/disarm` | Turn real ordering on (confirm with `/arm yes`) or back to dry run |
| `/cookie <value>` | Replace the session cookie without restarting |
| `/ref` | Show the purchase referral link |
| `/stop` | Shut the bot down |

### `/setup` walkthrough

```
you  /setup
bot  Setup 1/5 - what do you want to snipe?
you  355                    ← or a name like "storage 16TB", then pick from the list
bot  Setup 2/5 - how many?  (shows the product, its price and live stock)
you  2
bot  Setup 3/5 - 2 box(es) needs 2 name(s). Send them in one message.
you  boxa boxb
bot  Setup 4/5 - billing term? 1 3 6 12 24 36
you  12
bot  Setup 5/5 - confirm
you  yes
```

Nothing is saved until you send `yes`. Each answer is checked as you go, and a
bad one just asks again. `/cancel`, or ten minutes of silence, drops it.

### How many it buys

`/watch 355 2` means *buy two more than you own right now*. The bot reads your
services list once and pins an absolute goal (`owned + 2`). Before every order it
counts again from your account, so a restart, crash or lost state file can never
make it buy extras. If it cannot read your services list, it refuses to order.

Each box is a separate order of quantity 1.

### Every box needs its own username

Pulsed Media will not take the same seedbox username twice, so each target has a
**pool** of names and uses one per order:

```
/user 355 boxa boxb boxc      # pool of three
/user 355 + boxd              # add a fourth
```

If the pool runs out, the bot does not order. It empties the cart, tells you, and
keeps watching, so adding a name with `/user` re-arms it. `PM_USERNAME` (the box
you already have) always counts as taken, so a target with no pool can never
order by accident.

---

## Safety guards

**It cannot order the wrong product.** Five separate checks, each covered by tests:

1. At startup each product id is matched against the store catalogue by name; a
   mismatch disables that target.
2. The cart is emptied at startup.
3. Stock checks hit one product-specific URL, so only that product can be staged.
4. The configure page must name the target product.
5. Right before checkout the cart must hold exactly one line, the target, at
   quantity 1, and the total must be under the price ceiling.

Any failure aborts **before** the order is submitted and empties the cart.

**It cannot touch your existing services.** The code makes exactly two kinds of
POST request: add-to-cart and order submit. Your client area is only ever read.
There is no cancel, upgrade or service-management code anywhere.

**Only one copy can run.** `sniper.py` holds an exclusive lock on `sniper.lock`.
Two armed copies could both pass the owned-count check on the same drop and buy
twice, so a second copy exits immediately.

**Polling has a 10-second minimum.** See [Being a good neighbour](#being-a-good-neighbour).

---

## Configuration

Set these in `.env`. Most day-to-day changes are easier from Telegram.

| Variable | Default | Purpose |
|---|---|---|
| `PM_COOKIE` | none | **Required.** Session cookie |
| `PM_USERNAME` | none | **Required.** Your existing box's username (always treated as taken) |
| `PM_DRY_RUN` | `true` | `false` to place real orders |
| `PM_MODE` | `auto` | `order`, `cart` or `auto` (see below) |
| `PM_PID` | `355` | Product id to watch on first run (after that, use `/watch`) |
| `PM_TARGET_TOTAL` | `1` | First-run goal: how many matching services you want in total |
| `PM_BILLING_CYCLE` | `monthly` | `monthly` … `triennially` |
| `PM_MAX_TOTAL` | `0` | Abort if the total due exceeds this. `0` = no cap |
| `PM_POLL_SECONDS` | `20` | Seconds between checks (minimum 10) |
| `PM_JITTER_SECONDS` | `5` | Random extra delay per check |
| `PM_KEEPALIVE_SECONDS` | `300` | Session keep-alive interval |
| `PM_COOKIE_NAME` | `WHMCSmNqPBCNDcH8d` | Session cookie name |
| `PM_STRICT_CART_CHECK` | `true` | Re-read the cart before ordering |
| `PM_STOP_AFTER_ORDER` | `true` | Exit once every goal is met |
| `PM_CUSTOMFIELDS` | `{}` | Explicit custom field ids, e.g. `{"587":"myname"}` |
| `PM_SKIP_OWNED_CHECK` | `false` | Disable the owned-count guard (not recommended) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | none | Telegram control and alerts |

### Modes

| Mode | Behaviour |
|---|---|
| `order` | Detect stock, fill the cart and **complete the order** from credit |
| `cart` | Detect stock, fill the cart, and send you a checkout link to click |
| `auto` | Try `order`; if a captcha ever appears at checkout, fall back to `cart` |

Logged-in checkout currently has no captcha, so `order` and `auto` both order
fully automatically. Putting an item in the cart does **not** reserve it; only a
completed order does.

### Purchase referral

Orders are placed through Pulsed Media affiliate link `aff=3246`, which is fixed
in `config.py`. `/ref` shows it. This does not change the price you pay.

---

## Running it long-term

```powershell
start.bat         # start in the background (logs to sniper.log)
status.bat        # is it running? last log lines
stop.bat          # stop it
```

To keep it alive on a PC or laptop:

1. **Power settings.** On mains power, set sleep and hibernate to never:

   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /q SCHEME_CURRENT SUB_SLEEP    # 0x00000000 = never
   ```

   On Modern Standby laptops the real timeout can differ from the Settings
   page, so check the active scheme as above.

2. **The bot keeps Windows awake itself** while running
   (`SetThreadExecutionState`). The display can still turn off.

3. **Watchdog.** `watchdog.py` starts the bot if it is not running. Register it
   as a scheduled task (`PulsedSniperWatchdog`) that runs at logon and every 10
   minutes, using `pythonw.exe` so no console window flashes. It covers reboots,
   crashes and Windows Update restarts. Run this in PowerShell from the project
   folder:

   ```powershell
   $pyw = (Get-Command pythonw).Source
   $act = New-ScheduledTaskAction -Execute $pyw -Argument "`"$PWD\watchdog.py`"" -WorkingDirectory $PWD
   $t1  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
   $t2  = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10)
   Register-ScheduledTask -TaskName PulsedSniperWatchdog -Action $act -Trigger $t1,$t2
   ```

### Removing it

```powershell
.\uninstall.ps1 -DryRun       # show what would happen
.\uninstall.ps1               # remove the watchdog task, stop the bot, undo power changes
.\uninstall.ps1 -PurgeFiles   # ...and delete state files including session.json
```

It does not log you out of Pulsed Media, revoke your Telegram bot token, or
delete `.env`. Do those yourself if you are done with it.

---

## Being a good neighbour

Pulsed Media is a small host, not a CDN. Each check is one `HEAD` request that
transfers no body, but if many people run this at high speed the site will
notice, and the likely result is that it gets blocked for everyone.

So the fastest allowed interval is **10 seconds**, and the default is 20. Errors
back off exponentially up to 15 minutes. The speed that matters most is not the
interval anyway (see below): this bot is already a step ahead of a typical
checker once it sees stock.

| Interval | Found within | Requests/day per product |
|---|---|---|
| **20 s** (default) | ~20 s | ~4,300 |
| 10 s (minimum) | ~10 s | ~8,600 |

---

## How it works

Each check is a single **`HEAD`** request to the product's store URL:

| Response | Meaning |
|---|---|
| `200` | Out of stock |
| `302 → cart.php?a=confproduct&i=0` | **In stock, and already placed in your session's cart** |

WHMCS redirects the store page straight into the configure step when stock
exists, and a `HEAD` runs the same server code as a `GET`. So one request both
detects stock and starts the order, without downloading anything.

The order then completes in four more requests:

| Step | Request | Check |
|---|---|---|
| 1 | `GET cart.php?a=confproduct&i=0` | Page must name the target product |
| 2 | `POST cart.php` with `ajax=1&a=confproduct&…` | Empty reply = success; quantity forced to 1 |
| 3 | `GET cart.php?a=view` | Exactly one line, the target |
| 4 | `POST cart.php?a=checkout` with `applycredit=1` | Total under the price ceiling |

Measured on a live product: **about 2.6 seconds from detection to order-ready.**

Two details that are easy to get wrong:

- **Add to cart is an AJAX POST where an empty response means success.** A
  non-empty response is a list of validation errors. Posting to the visible form
  URL instead silently adds nothing.
- **Custom field ids differ per product** and cannot be read while a product is
  out of stock. The bot reads the form at order time and fills whichever field is
  labelled "Username".

Captcha status of each form, checked against the live site:

| Form | Captcha |
|---|---|
| Login | Yes (reCAPTCHA v2), hence the cookie |
| Add to cart | No |
| Guest checkout | Yes, but the bot never checks out as a guest |
| Logged-in checkout | No, so ordering is fully automatic |

If Pulsed Media changes this, `python tools/probe_auth.py` re-checks it: it carts
the cheapest in-stock item, reads the logged-in checkout form, reports whether
there is a captcha, and empties the cart. It never orders.

### The watch list

Targets live in `targets.json`, which both the bot and the Telegram commands
update. On first run it is seeded from `PM_PID` and `PM_TARGET_TOTAL`; after that
the file is the source of truth.

---

## Files

```
sniper.py          main loop: polling, keep-alive, backoff, ordering, alerts
whmcs.py           Pulsed Media (WHMCS) client: stock, cart, checkout
targets.py         the watch list (targets.json)
catalog.py         read-only store crawl: product id → name, URL, stock
tgbot.py           Telegram commands
runtime.py         live state shared by the main loop and Telegram thread
config.py          settings from .env / environment
notify.py          Telegram alerts
watchdog.py        restarts the bot if it is not running (scheduled task)
selftest.py        checks the site still parses (no login, no orders)
run_local.ps1      loads .env and runs the bot
start.bat / stop.bat / status.bat / status.ps1 / uninstall.ps1
tools/diag.py        shows this machine's public IP and what the site returns
tools/probe_auth.py  checks for a captcha on logged-in checkout (no orders)
test_*.py          offline tests, run automatically on every push
```

## Tests

```powershell
python -m unittest test_referral
python test_setup.py
python test_usernames.py
```

None of them touch the network or place orders.

## License

MIT. See [`LICENSE`](LICENSE).
