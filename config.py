"""Configuration loaded from environment variables."""
import json
import os
import sys


def _env(name, default=None, required=False, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        raw = default
    if required and (raw is None or raw == ""):
        sys.exit("FATAL: missing required environment variable %s" % name)
    if raw is None:
        return None
    if cast is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        return cast(raw)
    except (TypeError, ValueError):
        sys.exit("FATAL: bad value for %s: %r" % (name, raw))


# --- target -----------------------------------------------------------------
BASE = _env("PM_BASE", "https://pulsedmedia.com/clients").rstrip("/")
CATEGORY_URL = _env(
    "PM_CATEGORY_URL",
    "https://pulsedmedia.com/clients/index.php/store/the-eternal-vainamoinen",
)
PID = _env("PM_PID", "355", cast=int)
PRODUCT_NAME = _env("PM_PRODUCT_NAME", "Eternal Väinämöinen - Seedbox 8TB R5 10G")

# --- auth -------------------------------------------------------------------
# Either email+password, or paste a logged-in session cookie (see README).
EMAIL = _env("PM_EMAIL", "")
PASSWORD = _env("PM_PASSWORD", "")
COOKIE = _env("PM_COOKIE", "")  # e.g. "WHMCSmNqPBCNDcH8d=abcdef123..." or just the value
# Session cookie name for this WHMCS install (used when PM_COOKIE is a bare value).
COOKIE_NAME = _env("PM_COOKIE_NAME", "WHMCSmNqPBCNDcH8d")
# order = add to cart and complete the order.
# cart  = add to cart and notify only, leaving you to click Complete Order.
# auto  = try to order; fall back to cart mode if checkout turns out captcha-gated.
MODE = _env("PM_MODE", "auto").strip().lower()
# How often to touch clientarea.php to keep the PHP session from idling out.
KEEPALIVE_SECONDS = _env("PM_KEEPALIVE_SECONDS", "300", cast=int)

# --- order options ----------------------------------------------------------
BILLING_CYCLE = _env("PM_BILLING_CYCLE", "monthly")
# Custom fields, by WHMCS field id: {"587": "myusername", "588": "Google"}
CUSTOMFIELDS = json.loads(_env("PM_CUSTOMFIELDS", "{}"))
# Convenience: value used for any custom field whose label mentions "username".
USERNAME = _env("PM_USERNAME", "")
PAYMENT_METHOD = _env("PM_PAYMENTMETHOD", "")  # blank = keep the page default
ORDER_NOTES = _env("PM_NOTES", "")
PROMO_CODE = _env("PM_PROMO", "")

# --- safety -----------------------------------------------------------------
DRY_RUN = _env("PM_DRY_RUN", "true", cast=bool)
MAX_TOTAL = _env("PM_MAX_TOTAL", "0", cast=float)  # per-MONTH ceiling; 0 disables
STOP_AFTER_ORDER = _env("PM_STOP_AFTER_ORDER", "true", cast=bool)
# Absolute goal: stop once the account holds this many matching services.
# Restart-safe, because it is counted from the account, not from local state.
TARGET_TOTAL = _env("PM_TARGET_TOTAL", "1", cast=int)
# Escape hatch: skip the ownership guard entirely (not recommended).
SKIP_OWNED_CHECK = _env("PM_SKIP_OWNED_CHECK", "false", cast=bool)

# --- polling ----------------------------------------------------------------
POLL_SECONDS = _env("PM_POLL_SECONDS", "20", cast=int)
JITTER_SECONDS = _env("PM_JITTER_SECONDS", "5", cast=int)
MAX_BACKOFF = _env("PM_MAX_BACKOFF", "900", cast=int)
TIMEOUT = _env("PM_TIMEOUT", "30", cast=int)
# Seconds between "still watching" log lines. Time-based, not cycle-based:
# a cycle count means the log rate changes whenever the poll interval does.
HEARTBEAT_SECONDS = _env("PM_HEARTBEAT_SECONDS", "600", cast=int)
# Re-read the cart and prove it holds exactly 1x the target before checkout.
STRICT_CART_CHECK = _env("PM_STRICT_CART_CHECK", "true", cast=bool)
USER_AGENT = _env(
    "PM_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

# --- notifications ----------------------------------------------------------
TG_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = _env("TELEGRAM_CHAT_ID", "")

# --- files ------------------------------------------------------------------
# Kept next to the script so a Windows service and a console run share them.
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = _env("PM_STATE_FILE", os.path.join(HERE, "state.json"))
# The watch list the Telegram commands edit. Seeded from PM_PID on first run.
TARGETS_FILE = _env("PM_TARGETS_FILE", os.path.join(HERE, "targets.json"))
# Read back by /log. run_local.ps1 redirects stdout here.
LOG_FILE = _env("PM_LOG_FILE", os.path.join(HERE, "sniper.log"))
# A cookie accepted via /cookie is remembered here so a restart does not
# need console access. Only consulted when PM_COOKIE fails to authenticate.
SESSION_FILE = _env("PM_SESSION_FILE", os.path.join(HERE, "session.json"))

# Purchase referral. Fixed: not configurable from the environment or Telegram.
AFFILIATE_ID = "3246"
