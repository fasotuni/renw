"""
renwe.py — TokBoostly auto-registration (async Playwright + stealth)

IP modes:
  [1] my own IP
  [2] free proxy lists (pool, background refill)
  [3] decodo gateway (US)
  [4] dexodata gateway
  [5] proxies.txt (webshare, static list, cycles through)

CLOUD MODE:
  Fill in HARDCODED_GATEWAY_USER / HARDCODED_GATEWAY_PASS below (or set
  RENWE_GATEWAY_USER / RENWE_GATEWAY_PASS as env vars). Either source
  triggers cloud mode: prompts skipped, headless forced, ip_mode=gateway
  (decodo), target_accounts defaults to HARDCODED_TARGET_ACCOUNTS.

Proxy validation is strict: a proxy enters the pool only if it can
CONNECT-tunnel to tokboostly.com:443. Additionally, every proxy is
preflighted with a raw CONNECT immediately before a browser context
uses it, and any tunnel failure during [0] swaps to the next pool
entry without burning an account slot.

If the tunnel dies AFTER jmk_tg._ has been linked (during [9.5] or [10]),
the account is resurrected on a fresh proxy — cookies and localStorage
are carried over into a new browser context, so the login persists and
the release step can complete. Up to 3 fresh-proxy attempts.

Loop delay:
  In unlimited mode (accounts = 0), a configurable LOOP_DELAY_SECS
  pause is inserted after every successful account before the next one
  starts. Set to 0 to disable. Set via the constant below.

Flow per account:
  [0] signup -> [1] Continue with email -> [2] name -> [3] email
  [4] password x2 -> Create account
  [5] OTP from gocaria inbox -> Verify email
  [6] dashboard -> Instagram tab -> Connect -> TARGET_HANDLE (jmk_tg._) -> Save
      ** JMK LINKED: pending entry written to jmk_pending.txt **
  [7] Gain Followers -> [8] tick terms -> Place order with wallet
  [9] wait for "Order placed successfully"
  [9.5] settle: IG dashboard -> Orders -> 3x Refresh (5s apart), idle rest
  [10] IG dashboard -> Change Profile -> random verified small handle -> Save
       ** JMK RELEASED: pending entry cleared **
  [11] accounts.txt: email|password|release_handle (or UNRELEASED)

JMK SAFETY:
  - on link: jmk_pending.txt gets email|password|ts
  - on release: entry removed
  - on failure/interrupt while pending: LOUD alarm + jmk_alerts.txt +
    accounts.txt writes UNRELEASED as the third field
  - on startup: any pending entries from a previous run are shouted about
"""

import asyncio
import random
import string
import re
import time
import os
import hashlib
import base64
import socket
import ssl
import urllib.request
import urllib.parse
import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ========== HARDCODED CREDENTIALS (fallback for cloud) ==========
# Fill these in to guarantee cloud mode works even if env vars don't reach
# the container. Leave empty to rely on env vars / local prompts.
#
# >>> FILL THESE IN BEFORE PUSHING TO RAILWAY <<<
HARDCODED_GATEWAY_USER = ""      # decodo username, e.g. "sp12345678"
HARDCODED_GATEWAY_PASS = ""      # decodo password
HARDCODED_GATEWAY_HOST = "gate.decodo.com"
HARDCODED_GATEWAY_PORT = 7000
HARDCODED_TARGET_ACCOUNTS = 0    # 0 = unlimited loop
# <<< <<< <<<

# ========== CONFIGURATION ==========
TOKBOOSTLY_URL      = "https://tokboostly.com/signup/"
TOKBOOSTLY_HOST     = "tokboostly.com"
TOKBOOSTLY_DASH_IG  = "https://tokboostly.com/dashboard/?platform=instagram"
GOCARIA_URL         = "https://gocaria.my.id"
OTP_TIMEOUT         = 150
ORDER_WAIT_SECS     = 30
POST_ORDER_DELAY    = 120
ORDERS_REFRESH_TRIES = 3
ORDERS_REFRESH_GAP   = 5

# ========== LOOP DELAY (unlimited mode only) ==========
LOOP_DELAY_SECS = 420

TARGET_HANDLE       = "jmk_tg._"

INSTA_POOL_FILE     = "insta_pool.txt"
USED_INSTA_FILE     = "used_insta.txt"
INSTA_MAX_TRIES     = 8
INSTA_MIN_LEN       = 6
INSTA_MAX_LEN       = 20
INSTA_MAX_FOLLOWERS = 10_000

# ========== JMK SAFETY FILES ==========
JMK_PENDING_FILE = "jmk_pending.txt"
JMK_ALERTS_FILE  = "jmk_alerts.txt"

# ========== PROXIES.TXT (webshare etc.) ==========
PROXY_FILE = "proxies.txt"
FILE_PROXY_DEFAULT_CC = "US"
FILE_PROXY_DETECT_COUNTRY = False
FILE_PROXY_DETECT_CONCURRENCY = 20
FILE_PROXY_DETECT_TIMEOUT = 6

# ========== NETWORK RETRY ==========
TUNNEL_MARKERS = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_NETWORK_CHANGED",
)
GOTO_RETRIES = 3
GOTO_RETRY_WAIT = 4
RESURRECT_MAX_TRIES = 3

# ========== BANDWIDTH ECONOMY ==========
SAVE_BANDWIDTH = True
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
ASSET_CACHE_DIR = os.path.join(BASE_DIR, "asset_cache")
BLOCKED_TYPES = {"image", "media", "font"}

ATTRIBUTION_SAFE_HOSTS = (TOKBOOSTLY_HOST,)
ATTRIBUTION_SAFE_BLOCKED_TYPES = {"media", "font"}

TELEMETRY_MARKERS = (
    "google-analytics", "googletagmanager", "gtag/js",
    "/collect", "facebook.net/en_US/fbevents", "hotjar",
    "clarity.ms", "posthog", "sentry.io",
)

CAPTCHA_MARKERS = (
    "arkoselabs", "funcaptcha", "octocaptcha", "hcaptcha", "recaptcha",
    "challenges.cloudflare.com", "turnstile",
)

# ========== PROXY CONFIG (free lists) ==========
USE_PROXY = True
PROXY_POOL_SIZE = 15
PROXY_LOW_WATER = 5
PROXY_CHECK_URLS = [
    "https://api.myip.com",
    "https://ifconfig.co/json",
    "https://ipinfo.io/json",
    "https://ipwho.is/",
]
PROXY_CHECK_URL = PROXY_CHECK_URLS[0]
PROXY_CHECK_TIMEOUT = 4
PROXY_MAX_CANDIDATES = 150

PROXY_CACHE_FILE = "proxy_cache.txt"
PROXY_CACHE_TTL  = 15 * 60

FREE_PROXY_SOURCES = [
    ("bare", "https://api.proxyscrape.com/v4/free-proxy-list/get"
             "?request=displayproxies&protocol=http&timeout=10000"
             "&country=all&ssl=all&anonymity=all&limit=2000"),
    ("bare", "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt"),
    ("scheme", "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/all/data.txt"),
    ("scheme", "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt"),
    ("bare", "https://raw.githubusercontent.com/proxmint/free-proxy-list/main/proxies/http.txt"),
    ("bare", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("bare", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
]

# ========== CUSTOM GATEWAY (Decodo) ==========
GATEWAY_HOST = "gate.decodo.com"
GATEWAY_PORT = 7000
GATEWAY_USER = ""
GATEWAY_PASS = ""

# ========== DEXODATA GATEWAY ==========
DEXO_HOST = ""
DEXO_PORT = 0
DEXO_USER = ""
DEXO_PASS = ""
DEXO_COUNTRY = "US"

# ========== CLOUD / RAILWAY MODE ==========
# Triggered by env vars OR by the hardcoded constants at the top of the file.
# Env vars win if both are present.
_env_user = os.environ.get("RENWE_GATEWAY_USER", "").strip()
_env_pass = os.environ.get("RENWE_GATEWAY_PASS", "").strip()

CLOUD_GATEWAY_USER = _env_user or HARDCODED_GATEWAY_USER.strip()
CLOUD_GATEWAY_PASS = _env_pass or HARDCODED_GATEWAY_PASS.strip()
CLOUD_GATEWAY_HOST = (os.environ.get("RENWE_GATEWAY_HOST", "").strip()
                      or HARDCODED_GATEWAY_HOST)
try:
    CLOUD_GATEWAY_PORT = int(os.environ.get("RENWE_GATEWAY_PORT", "").strip()
                             or str(HARDCODED_GATEWAY_PORT))
except ValueError:
    CLOUD_GATEWAY_PORT = HARDCODED_GATEWAY_PORT

try:
    CLOUD_TARGET_ACCOUNTS = int(os.environ.get("RENWE_TARGET_ACCOUNTS", "").strip()
                                or str(HARDCODED_TARGET_ACCOUNTS))
except ValueError:
    CLOUD_TARGET_ACCOUNTS = HARDCODED_TARGET_ACCOUNTS
if CLOUD_TARGET_ACCOUNTS < 0:
    CLOUD_TARGET_ACCOUNTS = 0

CLOUD_MODE = bool(CLOUD_GATEWAY_USER)

# HEADLESS: env override → cloud mode → default visible
_env_headless = os.environ.get("RENWE_HEADLESS", "").strip().lower()
if _env_headless in ("1", "true", "yes", "on"):
    HEADLESS = True
elif CLOUD_MODE:
    HEADLESS = True
else:
    HEADLESS = False

# ========== UTILITY ==========

def generate_username(length=None):
    length = length or random.randint(8, 12)
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def generate_password_from_email(email):
    return email


def extract_otp_from_text(text):
    match = re.search(r'\b(\d{6})\b', text) or re.search(r'\b(\d{8})\b', text)
    return match.group(1) if match else None


def random_person_name():
    first = random.choice((
        "James", "Liam", "Noah", "Ethan", "Lucas", "Mason", "Logan", "Jack",
        "Sophia", "Olivia", "Emma", "Ava", "Mia", "Isabella", "Amelia", "Harper",
        "Chloe", "Elena", "Nora", "Ruby", "Diego", "Mateo", "Hugo", "Louis",
        "Léa", "Camille", "Ines", "Sofia", "Marco", "Giulia", "Luca", "Emilia",
    ))
    last = random.choice((
        "Reed", "Hayes", "Bennett", "Foster", "Morgan", "Coleman", "Parker",
        "Brooks", "Ellis", "Sullivan", "Ramirez", "Delgado", "Costa", "Rossi",
        "Bianchi", "Moreau", "Laurent", "Girard", "Keller", "Vogel", "Novak",
    ))
    return f"{first} {last}"


# ---------- Instagram handle sourcing (for rotation/closing) ----------

def _used_handles_path():
    return os.path.join(BASE_DIR, USED_INSTA_FILE)


def _load_used_handles():
    path = _used_handles_path()
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {ln.strip().lstrip("@").lower()
                    for ln in f if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def mark_handle_used(handle):
    if not handle:
        return
    try:
        with open(_used_handles_path(), "a", encoding="utf-8") as f:
            f.write(handle.lstrip("@") + "\n")
    except OSError as e:
        print(f"[insta] could not append to {USED_INSTA_FILE}: {e}")


def _random_insta_handle():
    core = ''.join(random.choices(string.ascii_lowercase, k=random.randint(6, 10)))
    sep = random.choice(("", ".", "_"))
    tail = ''.join(random.choices(string.ascii_lowercase + string.digits,
                                  k=random.randint(1, 5)))
    handle = core + sep + tail
    return handle[:INSTA_MAX_LEN].rstrip("._")


def _pop_handle_from_pool(used):
    path = os.path.join(BASE_DIR, INSTA_POOL_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.strip().lstrip("@") for ln in f
                     if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return None
    lines = [h for h in lines if h.lower() not in used]
    if not lines:
        return None
    handle = lines[0]
    remaining = lines[1:]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(remaining) + ("\n" if remaining else ""))
    print(f"[insta] Popped '{handle}' from {INSTA_POOL_FILE} "
          f"({len(remaining)} remaining).")
    return handle


IG_WEB_APP_ID = "936619743392459"
IG_INFO_URL = "https://i.instagram.com/api/v1/users/web_profile_info/?username={handle}"


def _ig_profile(handle, proxies=None, timeout=8):
    try:
        resp = requests.get(
            IG_INFO_URL.format(handle=handle),
            proxies=proxies,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/121.0.0.0 Safari/537.36",
                "X-IG-App-ID": IG_WEB_APP_ID,
                "Accept": "application/json",
            },
        )
    except Exception:
        return None
    if resp.status_code == 404:
        return {"exists": False}
    if resp.status_code == 200:
        try:
            data = resp.json()
            u = (data.get("data") or {}).get("user") or {}
            if not u.get("username"):
                return None
            followers = (u.get("edge_followed_by") or {}).get("count")
            return {
                "exists": True,
                "username": u.get("username"),
                "followers": followers,
            }
        except Exception:
            return None
    return None


def instagram_handle_exists(handle, proxies=None):
    info = _ig_profile(handle, proxies)
    if info is None:
        return None
    return bool(info.get("exists"))


def pick_rotation_handle(proxies=None, exclude=None):
    used = _load_used_handles()
    if exclude:
        used |= {h.lower().lstrip("@") for h in exclude}

    pooled = _pop_handle_from_pool(used)
    if pooled:
        return pooled

    attempts = 0
    inconclusive = 0
    while attempts < 40:
        attempts += 1
        handle = _random_insta_handle()
        if len(handle) < INSTA_MIN_LEN:
            continue
        if handle.lower() in used:
            continue
        info = _ig_profile(handle, proxies)
        if info is None:
            inconclusive += 1
            if inconclusive >= 3:
                print(f"[insta] IG check inconclusive - handing off @{handle} "
                      f"to tokboostly's own validation.")
                return handle
            continue
        if not info.get("exists"):
            continue
        followers = info.get("followers")
        if followers is not None and followers > INSTA_MAX_FOLLOWERS:
            print(f"[insta] @{handle} has {followers} followers - too big, skip.")
            continue
        print(f"[insta] Rotation candidate: @{handle} "
              f"(followers={followers}) [try {attempts}]")
        return handle
    return _random_insta_handle()


def proxy_dict_for_ig(proxy_server, proxy_auth):
    if not proxy_server:
        return None
    if proxy_auth:
        url = proxy_server
        if "://" in url:
            scheme, rest = url.split("://", 1)
            return {scheme: f"{scheme}://{proxy_auth[0]}:{proxy_auth[1]}@{rest}"}
        return {"http": url, "https": url}
    return {"http": proxy_server, "https": proxy_server}


# ========== PROXIES.TXT PARSING ==========

def _parse_proxy_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower().strip()
        if scheme not in ("http", "https", "socks5", "socks4"):
            scheme = "http"
    else:
        scheme, rest = "http", line

    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        if ":" in creds:
            user, pw = creds.split(":", 1)
        else:
            user, pw = creds, ""
        return (f"{scheme}://{hostport}", user, pw)

    parts = rest.split(":")
    if len(parts) == 2:
        return (f"{scheme}://{parts[0]}:{parts[1]}", None, None)
    if len(parts) == 4:
        ip, port, user, pw = parts
        return (f"{scheme}://{ip}:{port}", user, pw)
    return None


def load_proxies_file():
    path = os.path.join(BASE_DIR, PROXY_FILE)
    if not os.path.isfile(path):
        return []
    entries = []
    bad = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                parsed = _parse_proxy_line(ln)
                if parsed:
                    entries.append(parsed)
                elif ln.strip() and not ln.strip().startswith("#"):
                    bad += 1
    except OSError as e:
        print(f"[Proxy] could not read {PROXY_FILE}: {e}")
        return []
    if bad:
        print(f"[Proxy] {bad} unparsable line(s) in {PROXY_FILE}")
    print(f"[Proxy] loaded {len(entries)} proxy entries from {PROXY_FILE}")
    return entries


def _detect_one_country(entry):
    server, user, pw = entry
    proxies_dict = proxy_dict_for_ig(server, (user, pw) if user else None)
    if not proxies_dict:
        return None
    cc, _ = lookup_egress(proxies_dict,
                          timeout=FILE_PROXY_DETECT_TIMEOUT,
                          urls=PROXY_CHECK_URLS[:2])
    return cc


async def detect_file_proxy_countries(entries):
    print(f"[Proxy] detecting country for {len(entries)} proxies "
          f"(concurrency {FILE_PROXY_DETECT_CONCURRENCY})...")
    sem = asyncio.Semaphore(FILE_PROXY_DETECT_CONCURRENCY)
    results = [None] * len(entries)

    async def one(i, e):
        async with sem:
            cc = await asyncio.to_thread(_detect_one_country, e)
            results[i] = (*e, cc)
            mark = cc or "??"
            print(f"[Proxy] {i+1}/{len(entries)}: {e[0]} -> {mark}")

    await asyncio.gather(*(one(i, e) for i, e in enumerate(entries)))
    return results


# ========== PROXY PREFLIGHT (real CONNECT tunnel test) ==========

class ProxyDead(Exception):
    """This proxy stopped tunnelling between validation and use. Free proxies
    die in seconds; this exception means swap to the next one and retry."""
    pass


def _split_proxy_url(proxy_url):
    """'scheme://user:pass@host:port' -> (scheme, host, port, user, pw) or None."""
    if not proxy_url:
        return None
    if "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
    else:
        scheme, rest = "http", proxy_url
    scheme = scheme.lower()
    if scheme not in ("http", "https", "socks5", "socks4"):
        scheme = "http"
    user = pw = None
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        if ":" in creds:
            user, pw = creds.split(":", 1)
        else:
            user, pw = creds, ""
    else:
        hostport = rest
    if ":" not in hostport:
        return None
    host, ps = hostport.rsplit(":", 1)
    try:
        port = int(ps)
    except ValueError:
        return None
    return scheme, host, port, user, pw


def preflight_proxy(proxy_url, auth=None, target="tokboostly.com:443", timeout=6):
    parsed = _split_proxy_url(proxy_url)
    if not parsed:
        return False
    scheme, host, port, user_embedded, pw_embedded = parsed
    if auth:
        user, pw = auth[0], auth[1]
    else:
        user, pw = user_embedded, pw_embedded

    sock, _ = _tcp_open(host, port, timeout=timeout)
    if not sock:
        return False

    if scheme == "https":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            sock = ctx.wrap_socket(sock, server_hostname=host)
        except (ssl.SSLError, OSError):
            _shut(sock)
            return False

    try:
        verdict, _detail = _connect_once(sock, user or "", pw or "", target)
    finally:
        _shut(sock)
    return verdict == "ok"


def check_proxy_strict(proxy_url):
    cc, _ = lookup_egress({"http": proxy_url, "https": proxy_url},
                          timeout=PROXY_CHECK_TIMEOUT,
                          urls=PROXY_CHECK_URLS[:2])
    if not cc:
        return None
    if not preflight_proxy(proxy_url, target="tokboostly.com:443",
                           timeout=PROXY_CHECK_TIMEOUT + 2):
        return None
    return cc


# ========== STATIC FILE PROXY POOL ==========

class FileProxyPool:
    def __init__(self, entries):
        self.entries = list(entries)
        self._deck = []
        self._blacklist = set()
        self._reshuffle()
        self._lock = asyncio.Lock()

    def _reshuffle(self):
        fresh = [e for e in self.entries if e[0] not in self._blacklist]
        self._deck = list(fresh)
        random.shuffle(self._deck)

    def size(self):
        return len(self._deck)

    def total(self):
        return len(self.entries)

    def take(self):
        if not self._deck:
            self._reshuffle()
            print(f"[Proxy] file pool exhausted - reshuffled "
                  f"{len(self._deck)} entries "
                  f"({len(self._blacklist)} blacklisted)")
        return self._deck.pop() if self._deck else None

    def blacklist(self, server):
        if server and server not in self._blacklist:
            self._blacklist.add(server)
            print(f"[Proxy] blacklisted {server} "
                  f"({len(self._blacklist)} dead this run)")

    def maybe_refill(self, *_, **__):
        pass

    async def wait_for_one(self):
        return self.take()

    async def aclose(self):
        pass


# ========== JMK SAFETY ==========

def _jmk_pending_path():
    return os.path.join(BASE_DIR, JMK_PENDING_FILE)


def _jmk_alerts_path():
    return os.path.join(BASE_DIR, JMK_ALERTS_FILE)


def set_jmk_pending(email, password):
    try:
        with open(_jmk_pending_path(), "a", encoding="utf-8") as f:
            f.write(f"{email}|{password}|{int(time.time())}\n")
    except OSError as e:
        print(f"[JMK] ⚠️ could not append to {JMK_PENDING_FILE}: {e}")


def clear_jmk_pending(email):
    path = _jmk_pending_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return
    prefix = email + "|"
    keep = [ln for ln in lines if not ln.startswith(prefix)]
    try:
        if keep:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(keep) + "\n")
        else:
            os.remove(path)
    except OSError as e:
        print(f"[JMK] ⚠️ could not update {JMK_PENDING_FILE}: {e}")


def _append_jmk_alert(email, password, reason):
    try:
        with open(_jmk_alerts_path(), "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts}|UNRELEASED|{email}|{password}|{reason}\n")
    except OSError as e:
        print(f"[JMK] ⚠️ could not append to {JMK_ALERTS_FILE}: {e}")


def alarm_jmk_linked(email, password, reason=""):
    set_jmk_pending(email, password)
    _append_jmk_alert(email, password, reason or "(no reason given)")

    bar = "!" * 78
    print("\n" + bar)
    print("🚨  ATTENTION  🚨   JMK_TG._ IS STILL LINKED TO AN ACCOUNT   🚨  ATTENTION  🚨")
    print(bar)
    print(f"  The target handle @{TARGET_HANDLE} was linked to this account")
    print(f"  and the release/rotation step did NOT complete.")
    print("")
    print(f"  EMAIL:    {email}")
    print(f"  PASSWORD: {password}")
    print(f"  REASON:   {reason or '(unspecified)'}")
    print("")
    print(f"  You MUST manually change this account's Instagram handle")
    print(f"  (tokboostly -> dashboard -> Instagram -> Change Profile) before")
    print(f"  continuing, OR @{TARGET_HANDLE} stays attached to it.")
    print("")
    print(f"  Pending file : {JMK_PENDING_FILE}")
    print(f"  Alert log    : {JMK_ALERTS_FILE}")
    print(bar + "\n")


def check_jmk_pending_at_startup():
    path = _jmk_pending_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = [ln.strip() for ln in f
                       if ln.strip() and not ln.startswith("#")]
    except OSError:
        return
    if not entries:
        return

    bar = "!" * 78
    print("\n" + bar)
    print(f"🚨  PREVIOUS RUN LEFT @{TARGET_HANDLE} LINKED TO "
          f"{len(entries)} ACCOUNT(S)  🚨")
    print(bar)
    for ln in entries:
        email = ln.split("|", 1)[0] if "|" in ln else ln
        print(f"    {email}")
    print("")
    print(f"  These accounts still have @{TARGET_HANDLE} linked.")
    print(f"  Fix them manually, or delete {JMK_PENDING_FILE} if you've")
    print(f"  already handled them.")
    print(f"  Full history in: {JMK_ALERTS_FILE}")
    print(bar + "\n")


# ========== GENERIC PAGE HELPERS ==========

async def wait_first(page, selectors, timeout=20000, state="visible"):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and (state != "visible" or await el.is_visible()):
                    return sel
            except Exception:
                pass
        await asyncio.sleep(0.4)
    raise PlaywrightTimeoutError(f"None of these selectors appeared: {selectors}")


async def click_any(page, selectors, timeout=15000, scroll=True):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                if not await el.is_visible():
                    continue
                if not await el.is_enabled():
                    continue
                if scroll:
                    try:
                        await el.scroll_into_view_if_needed()
                    except Exception:
                        pass
                await el.click()
                return sel
            except Exception:
                continue
        await asyncio.sleep(0.4)
    return None


async def fill_first(page, selectors, value, timeout=15000):
    sel = await wait_first(page, selectors, timeout=timeout)
    await page.click(sel)
    await page.fill(sel, "")
    await page.type(sel, value, delay=random.uniform(35, 90))
    return sel


async def countdown_sleep(seconds, label=""):
    remaining = int(seconds)
    while remaining > 0:
        mins, secs = divmod(remaining, 60)
        prefix = f"[{label}] " if label else ""
        print(f"{prefix}waiting {mins:02d}:{secs:02d} ...")
        chunk = min(10, remaining)
        try:
            await asyncio.sleep(chunk)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        remaining -= chunk


async def goto_with_retry(page, url, tries=GOTO_RETRIES, wait=GOTO_RETRY_WAIT,
                          label=""):
    tag = f"[{label}] " if label else ""
    last_err = None
    for i in range(tries):
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            if i > 0:
                print(f"{tag}goto recovered on try {i+1}/{tries}")
            return True
        except Exception as e:
            err = str(e)
            last_err = err
            if any(m in err for m in TUNNEL_MARKERS):
                if i < tries - 1:
                    print(f"{tag}tunnel error on goto try {i+1}/{tries}, "
                          f"retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                raise ProxyDead(f"goto failed after {tries} tries: {err[:120]}")
            raise
    raise ProxyDead(f"goto exhausted retries: {str(last_err)[:120]}")


# ========== BANDWIDTH SAVER ==========

def new_bandwidth_stats():
    return {"wire": 0, "saved": 0, "blocked": 0, "from_cache": 0,
            "cached_new": 0, "local_urls": set()}


_ASSET_TASKS = set()


def _cache_paths(url):
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return (os.path.join(ASSET_CACHE_DIR, key + ".bin"),
            os.path.join(ASSET_CACHE_DIR, key + ".ct"))


def _immutable_response(headers):
    cc = (headers.get("cache-control") or "").lower()
    if any(bad in cc for bad in ("no-store", "no-cache", "private")):
        return False
    if "immutable" in cc:
        return True
    m = re.search(r"max-age=(\d+)", cc)
    return bool(m and int(m.group(1)) >= 86400)


def _cacheable_request(request):
    return (request.method == "GET"
            and request.resource_type in ("script", "stylesheet"))


def make_route_handler(stats):
    async def handler(route):
        request = route.request
        url = request.url
        lowered = url.lower()
        rtype = request.resource_type
        host = urllib.parse.urlparse(url).netloc.lower()

        if any(m in lowered for m in CAPTCHA_MARKERS):
            await route.continue_()
            return

        if any(h in host for h in ATTRIBUTION_SAFE_HOSTS):
            if rtype in ATTRIBUTION_SAFE_BLOCKED_TYPES:
                stats["blocked"] += 1
                try:
                    await route.abort()
                except Exception:
                    pass
                return
        elif any(m in lowered for m in TELEMETRY_MARKERS) or rtype in BLOCKED_TYPES:
            stats["blocked"] += 1
            try:
                await route.abort()
            except Exception:
                pass
            return

        if _cacheable_request(request):
            body_path, ct_path = _cache_paths(url)
            if os.path.exists(body_path):
                try:
                    with open(body_path, "rb") as f:
                        body = f.read()
                    ctype = "application/javascript"
                    if os.path.exists(ct_path):
                        with open(ct_path, "r", encoding="utf-8") as f:
                            ctype = f.read().strip() or ctype
                    stats["from_cache"] += 1
                    stats["saved"] += len(body)
                    stats["local_urls"].add(url)
                    await route.fulfill(status=200, body=body, content_type=ctype)
                    return
                except OSError:
                    pass

        await route.continue_()

    return handler


async def _store_asset(response, stats):
    try:
        headers = {k.lower(): v for k, v in (await response.all_headers()).items()}
        if not _immutable_response(headers):
            return
        body = await response.body()
        if not body:
            return
        body_path, ct_path = _cache_paths(response.request.url)
        os.makedirs(ASSET_CACHE_DIR, exist_ok=True)
        tmp = body_path + ".part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, body_path)
        with open(ct_path, "w", encoding="utf-8") as f:
            f.write(headers.get("content-type") or "application/javascript")
        stats["cached_new"] += 1
    except Exception:
        pass


def attach_asset_cache(context, stats):
    def on_response(response):
        try:
            request = response.request
            if response.status != 200 or not _cacheable_request(request):
                return
            url = request.url
            if url in stats["local_urls"]:
                return
            body_path, _ = _cache_paths(url)
            if os.path.exists(body_path):
                return
            task = asyncio.get_running_loop().create_task(_store_asset(response, stats))
            _ASSET_TASKS.add(task)
            task.add_done_callback(_ASSET_TASKS.discard)
        except Exception:
            pass

    context.on("response", on_response)


async def drain_asset_tasks():
    if not _ASSET_TASKS:
        return
    pending = list(_ASSET_TASKS)
    done, still_going = await asyncio.wait(pending, timeout=3)
    for task in still_going:
        task.cancel()
    if still_going:
        await asyncio.gather(*still_going, return_exceptions=True)


async def attach_bandwidth_meter(context, page, stats):
    try:
        cdp = await context.new_cdp_session(page)
    except Exception as e:
        print(f"[bw] Meter unavailable ({e}).")
        return
    seen = {}

    def on_sent(evt):
        seen[evt.get("requestId")] = ((evt.get("request") or {}).get("url") or "")

    def on_done(evt):
        url = seen.pop(evt.get("requestId"), "")
        if url and url in stats["local_urls"]:
            return
        stats["wire"] += evt.get("encodedDataLength") or 0

    cdp.on("Network.requestWillBeSent", on_sent)
    cdp.on("Network.loadingFinished", on_done)
    try:
        await cdp.send("Network.enable")
    except Exception as e:
        print(f"[bw] Meter could not start ({e}).")


async def install_bandwidth_saver(context, page=None, stats=None):
    if not SAVE_BANDWIDTH:
        return None
    stats = new_bandwidth_stats() if stats is None else stats
    await context.route("**/*", make_route_handler(stats))
    attach_asset_cache(context, stats)
    if page is not None:
        await attach_bandwidth_meter(context, page, stats)
    return stats


async def relax_bandwidth_saver(context):
    try:
        await context.unroute("**/*")
        print("[bw] Asset filter lifted.")
    except Exception:
        pass


def report_bandwidth(stats, label):
    if not stats:
        return
    print(f"[bw] {label}: {stats['wire']/1048576:.2f} MB over the proxy | "
          f"{stats['blocked']} refused | "
          f"{stats['from_cache']} from disk ({stats['saved']/1048576:.2f} MB) | "
          f"{stats['cached_new']} newly cached")


# ========== TEARDOWN ==========

async def _safe_screenshot(page, path):
    try:
        await page.screenshot(path=path)
        return True
    except Exception:
        return False


async def _safe_close(closable):
    try:
        await closable.close()
    except Exception:
        pass


async def _sleep_or_stop(seconds):
    try:
        await asyncio.sleep(seconds)
        return True
    except (KeyboardInterrupt, asyncio.CancelledError):
        return False


async def _shutdown(browser):
    try:
        await drain_asset_tasks()
    except BaseException:
        pass
    try:
        await browser.close()
    except BaseException:
        pass


# ========== COUNTRY POOLS ==========

EUROPEAN_COUNTRIES = [
    ("Albania", "AL"), ("Austria", "AT"), ("Belgium", "BE"), ("Bulgaria", "BG"),
    ("Croatia", "HR"), ("Cyprus", "CY"), ("Czech Republic", "CZ"), ("Denmark", "DK"),
    ("Estonia", "EE"), ("Finland", "FI"), ("France", "FR"), ("Germany", "DE"),
    ("Greece", "GR"), ("Hungary", "HU"), ("Iceland", "IS"), ("Ireland", "IE"),
    ("Italy", "IT"), ("Latvia", "LV"), ("Lithuania", "LT"), ("Luxembourg", "LU"),
    ("Malta", "MT"), ("Netherlands", "NL"), ("Norway", "NO"), ("Poland", "PL"),
    ("Portugal", "PT"), ("Romania", "RO"), ("Serbia", "RS"), ("Slovakia", "SK"),
    ("Slovenia", "SI"), ("Spain", "ES"), ("Sweden", "SE"), ("Switzerland", "CH"),
    ("United Kingdom", "GB"),
]
OTHER_COUNTRIES = [
    ("United States of America", "US"), ("Canada", "CA"), ("Australia", "AU"),
    ("Brazil", "BR"), ("Kenya", "KE"),
]
EUROPE_WEIGHT = 0.8

GATEWAY_COUNTRIES = [
    ("United States of America", "US"),
]


def country_label_for(code):
    for label, c in EUROPEAN_COUNTRIES + OTHER_COUNTRIES:
        if c == code:
            return label
    return code


TZ_BY_COUNTRY = {
    "GB": "Europe/London", "IS": "Atlantic/Reykjavik",
    "EE": "Europe/Tallinn", "FI": "Europe/Helsinki", "GR": "Europe/Athens",
    "CY": "Asia/Nicosia", "US": "America/New_York", "CA": "America/Toronto",
    "IE": "Europe/Dublin", "PT": "Europe/Lisbon",
    "AU": "Australia/Sydney", "BR": "America/Sao_Paulo", "KE": "Africa/Nairobi",
}


def timezone_for(code):
    return TZ_BY_COUNTRY.get(code, "Europe/Berlin")


def _fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_proxy_list():
    proxies = set()
    for fmt, url in FREE_PROXY_SOURCES:
        try:
            text = _fetch_url(url)
        except Exception as e:
            host = url.split("/")[2] if "/" in url else url
            print(f"[Proxy] Source failed ({host}): {e}")
            continue
        host = url.split("/")[2] if "/" in url else url
        added = 0
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if fmt == "scheme":
                if ln.startswith("http://") or ln.startswith("https://"):
                    proxies.add(ln)
                    added += 1
            else:
                if "://" in ln:
                    if ln.startswith("http://") or ln.startswith("https://"):
                        proxies.add(ln)
                        added += 1
                elif ":" in ln:
                    proxies.add(f"http://{ln}")
                    added += 1
        print(f"[Proxy] {host}: {added} entries")
    proxies = list(proxies)
    random.shuffle(proxies)
    print(f"[Proxy] {len(proxies)} unique proxy endpoints after dedupe.")
    return proxies


def _country_from_payload(data):
    if not isinstance(data, dict):
        return None
    if data.get("success") is False or data.get("status") == "fail":
        return None
    for key in ("cc", "country_code", "countryCode", "country_iso", "country"):
        val = data.get(key)
        if isinstance(val, str) and len(val) == 2 and val.isalpha():
            return val.upper()
    return None


def lookup_egress(proxies, timeout=PROXY_CHECK_TIMEOUT, urls=None, verbose=False):
    failures = []
    for url in (urls or PROXY_CHECK_URLS):
        try:
            resp = requests.get(url, proxies=proxies, timeout=timeout,
                                headers={"User-Agent": "curl/8.5.0"})
            cc = _country_from_payload(resp.json())
            if cc:
                return cc, failures
            failures.append((url, f"HTTP {resp.status_code}, no country"))
        except Exception as e:
            failures.append((url, f"{type(e).__name__}: {str(e)[:90]}"))
        if verbose and failures:
            print(f"[Gateway]   check {failures[-1][0]} -> {failures[-1][1]}")
    return None, failures


def check_proxy(proxy_url):
    return check_proxy_strict(proxy_url)


# ========== FREE PROXY POOL (background refill, cache-backed) ==========

def _proxy_cache_path():
    return os.path.join(BASE_DIR, PROXY_CACHE_FILE)


def load_proxy_cache():
    path = _proxy_cache_path()
    if not os.path.isfile(path):
        return []
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return []
    if age > PROXY_CACHE_TTL:
        print(f"[Proxy] cache is {int(age)}s old (>{PROXY_CACHE_TTL}s) - ignoring.")
        return []
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                if "|" in ln:
                    url, cc = ln.split("|", 1)
                    url = url.strip()
                    cc = cc.strip() or None
                else:
                    url, cc = ln, None
                if url:
                    entries.append((url, cc))
    except OSError:
        return []
    print(f"[Proxy] loaded {len(entries)} cached proxies (age {int(age)}s).")
    return entries


def save_proxy_cache(items):
    path = _proxy_cache_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# TokBoostly proxy cache - "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            for url, cc in items:
                f.write(f"{url}|{cc or ''}\n")
    except OSError as e:
        print(f"[Proxy] could not save cache: {e}")


async def fetch_and_validate_proxies(target=PROXY_POOL_SIZE):
    print("[Proxy] Fetching free lists (no country filter)...")
    try:
        candidates = await asyncio.to_thread(fetch_proxy_list)
    except Exception as e:
        print(f"[Proxy] list fetch failed: {e}")
        return []
    if not candidates:
        print("[Proxy] no proxy candidates from any source.")
        return []
    random.shuffle(candidates)
    candidates = candidates[:PROXY_MAX_CANDIDATES]
    print(f"[Proxy] validating up to {len(candidates)} candidates "
          f"(strict: country + CONNECT-tunnel to tokboostly.com), "
          f"stops at {target} working...")
    sem = asyncio.Semaphore(40)
    working = []

    async def one(p):
        async with sem:
            cc = await asyncio.to_thread(check_proxy_strict, p)
            return (p, cc) if cc else None

    tasks = [asyncio.create_task(one(p)) for p in candidates]
    for fut in asyncio.as_completed(tasks):
        result = await fut
        if result:
            working.append(result)
            print(f"[Proxy] {len(working)}/{target}: {result[0]} ({result[1]})")
            if len(working) >= target:
                for t in tasks:
                    t.cancel()
                break
    return working


class ProxyPool:
    def __init__(self, initial=None):
        self.items = list(initial or [])
        random.shuffle(self.items)
        self.refill_task = None
        self._lock = asyncio.Lock()
        self._new_since_save = 0
        self._blacklist = set()

    def size(self):
        return len(self.items)

    def take(self):
        while self.items:
            item = self.items.pop()
            key = item[0] if isinstance(item, tuple) else item
            if key in self._blacklist:
                continue
            return item
        return None

    def blacklist(self, server):
        if server and server not in self._blacklist:
            self._blacklist.add(server)
            print(f"[Proxy] blacklisted {server} "
                  f"({len(self._blacklist)} dead this run)")

    async def _do_refill(self, target):
        try:
            new = await fetch_and_validate_proxies(target)
        except Exception as e:
            print(f"[Proxy] refill failed: {e}")
            return
        if not new:
            print("[Proxy] refill returned nothing.")
            return
        async with self._lock:
            self.items.extend(new)
            random.shuffle(self.items)
            self._new_since_save += len(new)
        print(f"[Proxy] refill: +{len(new)} validated, pool now {self.size()}")
        if self._new_since_save >= 5:
            try:
                save_proxy_cache(self.items)
                self._new_since_save = 0
            except Exception:
                pass

    def maybe_refill(self, low_water, target):
        if self.size() >= low_water:
            return
        if self.refill_task and not self.refill_task.done():
            return
        print(f"[Proxy] pool low ({self.size()}) - starting background refill")
        self.refill_task = asyncio.create_task(self._do_refill(target))

    async def wait_for_one(self):
        if self.refill_task and not self.refill_task.done():
            print("[Proxy] pool empty - waiting for in-flight refill...")
            await self.refill_task
        else:
            print("[Proxy] pool empty - refilling synchronously...")
            self.refill_task = asyncio.create_task(
                self._do_refill(PROXY_POOL_SIZE))
            await self.refill_task
        return self.take()

    async def aclose(self):
        t = self.refill_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# ========== GATEWAY HELPERS ==========

def gateway_auth_username(gateway):
    if gateway.get("provider") == "dexodata":
        return gateway["user"]
    return f"user-{gateway['user']}"


GATEWAY_SCHEMES = ("http", "https", "socks5")


def proxy_url_for(gateway, username=None):
    scheme = gateway.get("scheme") or "http"
    user = username if username is not None else gateway_auth_username(gateway)
    pw = gateway.get("pass", "")
    host, port = gateway["host"], gateway["port"]
    if not user:
        return f"{scheme}://{host}:{port}"
    return (f"{scheme}://{urllib.parse.quote(user, safe='')}:"
            f"{urllib.parse.quote(pw, safe='')}@{host}:{port}")


def _tcp_open(host, port, timeout=8):
    try:
        return socket.create_connection((host, port), timeout=timeout), "open"
    except socket.gaierror as e:
        return None, f"DNS failure ({e})"
    except socket.timeout:
        return None, "timed out"
    except ConnectionRefusedError:
        return None, "refused"
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"


def _connect_once(sock, user, pw, target):
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if user:
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        head += f"Proxy-Authorization: Basic {token}\r\n"
    head += "User-Agent: curl/8.5.0\r\nProxy-Connection: Keep-Alive\r\n\r\n"
    try:
        sock.settimeout(12)
        sock.sendall(head.encode())
        data = sock.recv(2048)
    except (ConnectionResetError, ssl.SSLError) as e:
        return "reset", f"reset ({type(e).__name__})"
    except socket.timeout:
        return "silent", "no reply within 12s"
    except OSError as e:
        return "reset", f"{type(e).__name__}: {e}"
    if not data:
        return "silent", "closed without reply"
    status = data.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    if b"200" in data[:16]:
        return "ok", status
    if b"407" in data[:16]:
        return "auth", status
    if b"403" in data[:16]:
        return "forbidden", f"{status} (target {target})"
    return "other", f"{status} (target {target})"


def _shut(sock):
    try:
        sock.close()
    except OSError:
        pass


PROBE_TARGETS = ("api.myip.com:443", "example.com:443", "ifconfig.co:443")
_VERDICT_RANK = {"ok": 5, "auth": 4, "forbidden": 3, "other": 2,
                 "silent": 1, "reset": 1, "no-tls": 0, "no-socks": 0, "dead": 0}


def _probe_connect(host, port, user, pw, wrap_tls=False):
    best = ("dead", "no attempt made")
    for target in PROBE_TARGETS:
        sock, note = _tcp_open(host, port)
        if not sock:
            return "dead", note
        if wrap_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                sock = ctx.wrap_socket(sock, server_hostname=host)
            except ssl.SSLError as e:
                _shut(sock)
                return "no-tls", f"not TLS ({e.reason or e})"
            except OSError as e:
                _shut(sock)
                return "no-tls", f"{type(e).__name__}: {e}"
        try:
            verdict, detail = _connect_once(sock, user, pw, target)
        finally:
            _shut(sock)
        if verdict == "ok":
            return "ok", f"{detail} (via {target})"
        if verdict == "auth":
            return verdict, detail
        if _VERDICT_RANK.get(verdict, 0) > _VERDICT_RANK.get(best[0], 0):
            best = (verdict, detail)
    return best


def _probe_http(host, port, user, pw):
    return _probe_connect(host, port, user, pw, wrap_tls=False)


def _probe_https(host, port, user, pw):
    return _probe_connect(host, port, user, pw, wrap_tls=True)


def _probe_socks5(host, port, user, pw):
    sock, note = _tcp_open(host, port)
    if not sock:
        return "dead", note
    try:
        sock.settimeout(10)
        sock.sendall(b"\x05\x02\x00\x02")
        greet = sock.recv(2)
        if len(greet) < 2 or greet[0] != 0x05:
            return "no-socks", "not SOCKS5"
        method = greet[1]
        if method == 0xFF:
            return "other", "no acceptable auth"
        if method == 0x02:
            ub, pb = user.encode(), pw.encode()
            sock.sendall(bytes([0x01, len(ub)]) + ub + bytes([len(pb)]) + pb)
            reply = sock.recv(2)
            if len(reply) == 2 and reply[1] == 0x00:
                return "ok", "SOCKS5 user/pass accepted"
            return "auth", "SOCKS5 rejected credentials"
        return "ok", "SOCKS5 no-auth"
    except (ConnectionResetError, socket.timeout) as e:
        return "no-socks", f"no handshake ({type(e).__name__})"
    except OSError as e:
        return "no-socks", f"{type(e).__name__}: {e}"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def my_public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            return requests.get(url, timeout=8).text.strip()
        except Exception:
            continue
    return None


def make_gateway_proxy(gateway):
    proxy = {
        "server": f"{gateway.get('scheme') or 'http'}://{gateway['host']}:{gateway['port']}",
        "username": gateway["user"],
        "password": gateway["pass"],
    }
    if gateway.get("provider") == "dexodata":
        cc = gateway.get("country") or "US"
        return proxy, (country_label_for(cc), cc)

    label, cc = random.choice(GATEWAY_COUNTRIES)
    session_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    proxy["username"] = f"{gateway_auth_username(gateway)}-country-{cc.lower()}-session-{session_id}"
    return proxy, (label, cc)


def probe_gateway(gateway):
    host = gateway.get("host", "")
    port = int(gateway.get("port") or 0)
    user = gateway_auth_username(gateway)
    pw = gateway.get("pass", "")
    report = []

    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(
            host, port, socket.AF_INET, socket.SOCK_STREAM)})
        report.append(f"DNS   {host} -> {', '.join(ips)}")
    except Exception as e:
        report.append(f"DNS   cannot resolve {host} ({e})")
        return None, "dns", report

    verdicts = {}
    for scheme, probe in (("http", _probe_http),
                          ("https", _probe_https),
                          ("socks5", _probe_socks5)):
        verdict, detail = probe(host, port, user, pw)
        verdicts[scheme] = verdict
        report.append(f"{scheme:<6} {verdict:<9} {detail}")
        if verdict == "ok":
            return scheme, "ok", report
        if verdict == "auth":
            return None, "auth", report
        if verdict == "forbidden":
            return None, "forbidden", report

    if verdicts.get("http") == "dead":
        return None, "closed", report
    if all(verdicts.get(s) in ("silent", "reset", "no-tls", "no-socks")
           for s in GATEWAY_SCHEMES):
        return None, "silent", report
    return None, "unknown", report


def print_gateway_help(gateway, reason):
    host, port = gateway.get("host"), gateway.get("port")
    print("[Gateway] -------- diagnosis --------")
    if reason == "dns":
        print(f"[Gateway] '{host}' does not resolve.")
    elif reason == "auth":
        print("[Gateway] 407 - endpoint IS a proxy, credentials are wrong.")
    elif reason == "forbidden":
        print("[Gateway] 403 on every probe - credentials accepted, port refusing tunnels.")
    elif reason == "closed":
        print(f"[Gateway] Nothing listening on {host}:{port}.")
    elif reason == "silent":
        print(f"[Gateway] {host}:{port} connects then goes quiet - IP-allowlist or wrong port.")
        ip = my_public_ip()
        if ip:
            print(f"[Gateway] Your public IP: {ip}")
    else:
        print("[Gateway] Endpoint reachable but not behaving as HTTP/HTTPS/SOCKS5.")
    print("[Gateway] ---------------------------")


def check_gateway(gateway):
    scheme, reason, report = probe_gateway(gateway)
    for line in report:
        print(f"[Gateway]   {line}")
    if not scheme:
        print_gateway_help(gateway, reason)
        return None
    gateway["scheme"] = scheme
    print(f"[Gateway] endpoint speaks {scheme.upper()}")
    url = proxy_url_for(gateway)
    cc, failures = lookup_egress({"http": url, "https": url}, timeout=20)
    if cc:
        return cc
    print("[Gateway] tunnel opened, but no IP-checker answered:")
    for chk_url, note in failures:
        print(f"[Gateway]   {chk_url} -> {note}")
    return None


REQUIRED_TARGETS = (
    "tokboostly.com:443",
    "gocaria.my.id:443",
    "i.instagram.com:443",
    "www.instagram.com:443",
)


def _connect_target(gateway, target):
    sock, note = _tcp_open(gateway["host"], gateway["port"])
    if not sock:
        return "dead", note
    if gateway.get("scheme") == "https":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            sock = ctx.wrap_socket(sock, server_hostname=gateway["host"])
        except (ssl.SSLError, OSError) as e:
            _shut(sock)
            return "no-tls", f"{type(e).__name__}: {e}"
    try:
        return _connect_once(sock, gateway.get("user", ""),
                             gateway.get("pass", ""), target)
    finally:
        _shut(sock)


def check_destinations(gateway):
    if gateway.get("scheme") == "socks5":
        print("[Gateway] destination policy: not checked (SOCKS5).")
        return []
    blocked = []
    for target in REQUIRED_TARGETS:
        verdict, detail = _connect_target(gateway, target)
        if verdict != "ok":
            verdict, detail = _connect_target(gateway, target)
        print(f"[Gateway]   {target:<26} {verdict:<9} {detail}")
        if verdict != "ok":
            blocked.append((target, verdict, detail))
    return blocked


# ========== GOCARIA INBOX ==========

async def get_email_from_gocaria(page=None):
    prefix = "".join(random.choices(string.ascii_lowercase,
                                    k=random.randint(10, 14)))
    full_email = f"{prefix}@prock.app"
    print(f"[1] Minted temporary email: {full_email}")
    print(f"    inbox: {GOCARIA_URL}/{full_email}")
    return full_email


async def wait_for_otp_from_gocaria(page, timeout_seconds=OTP_TIMEOUT,
                                    prefer_keyword=None, return_meta=False):
    print(f"[OTP] Waiting for code (max {timeout_seconds}s)...")
    keywords = ("tokboostly", "verify", "code", "otp", "verification")
    start = time.time()
    seen_subjects = []
    while time.time() - start < timeout_seconds:
        try:
            refresh_btn = await page.query_selector("button:has-text('Refresh')")
            if refresh_btn:
                await refresh_btn.click()
                await asyncio.sleep(random.uniform(2, 4))
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                pass

            items = await page.query_selector_all(
                "div.space-y-2 > div, div.cursor-pointer:has(h4), "
                "div[class*='inbox'] > *, ul > li, div[class*='email']"
            )
            preferred, rest = [], []
            for it in items:
                t = ((await it.text_content()) or "").lower()
                (preferred if (prefer_keyword and prefer_keyword in t) else rest).append(it)

            for it in (preferred + rest)[:5]:
                try:
                    snippet = ((await it.text_content()) or "")[:140]
                    if snippet and snippet not in seen_subjects:
                        seen_subjects.append(snippet)
                        print(f"[OTP-DIAG] inbox row: {snippet!r}")
                except Exception:
                    continue

            for item in preferred + rest:
                if not await item.is_visible():
                    continue
                text = (await item.text_content()) or ""
                lowered = text.lower()
                if not any(k in lowered for k in keywords):
                    continue

                list_otp = extract_otp_from_text(text)
                try:
                    await item.click()
                except Exception as click_err:
                    print(f"[OTP-DIAG] click failed ({click_err}), skipping")
                    continue
                await asyncio.sleep(random.uniform(1, 3))
                body = (await page.text_content("body")) or ""
                body_otp = extract_otp_from_text(body)

                ts = None
                m = re.search(r'\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\b', body)
                if m:
                    ts = m.group(1)

                if body_otp:
                    print(f"[OTP] Got code from body: {body_otp} "
                          f"(list preview said: {list_otp}, ts~{ts})")
                    if return_meta:
                        return body_otp, {
                            "source": "body",
                            "snippet": body[:800],
                            "timestamp": ts,
                            "list_preview_otp": list_otp,
                            "subjects_seen": seen_subjects,
                        }
                    return body_otp

                if list_otp:
                    print(f"[OTP] Body had no code - using list preview: {list_otp}")
                    if return_meta:
                        return list_otp, {
                            "source": "list_preview",
                            "snippet": text[:400],
                            "timestamp": ts,
                            "subjects_seen": seen_subjects,
                        }
                    return list_otp

                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            print("[OTP] No code yet, retrying in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[OTP] Error: {e}")
            await asyncio.sleep(5)
    raise Exception(f"OTP wait timed out after {timeout_seconds}s "
                    f"(subjects seen: {seen_subjects})")


# ========== TOKBOOSTLY FLOW ==========

async def _force_enable_and_click(page, label_regex):
    try:
        await page.evaluate("""(label) => {
            const btns = Array.from(document.querySelectorAll('button'));
            const b = btns.find(x => new RegExp(label, 'i').test((x.textContent||'').trim()));
            if (b) { b.removeAttribute('disabled'); b.click(); }
        }""", label_regex)
        return True
    except Exception:
        return False


async def _fire_validation(page, selectors):
    try:
        await page.evaluate("""(sels) => {
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (!el) continue;
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.dispatchEvent(new Event('blur',   {bubbles:true}));
            }
        }""", selectors)
    except Exception:
        pass


async def _fill_ig_modal_and_save(page, handle, timeout=12000):
    try:
        modal_sel = await wait_first(page, [
            "input[placeholder='yourhandle']",
            "input[autocomplete='off'][type='text'][placeholder*='handle']",
            "input[type='text'][placeholder*='handle']",
        ], timeout=timeout)
    except PlaywrightTimeoutError:
        return None

    try:
        await page.click(modal_sel)
        await page.fill(modal_sel, "")
        await page.type(modal_sel, handle, delay=random.uniform(50, 110))
        await asyncio.sleep(random.uniform(0.4, 0.9))

        saved_click = await click_any(page, [
            "button:text-is('Save')",
            "button[type='button']:text-is('Save')",
        ], timeout=8000)
        if not saved_click:
            try:
                await page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const b = btns.find(x => (x.textContent || '').trim() === 'Save');
                    if (b) b.click();
                }""")
            except Exception:
                pass

        await asyncio.sleep(random.uniform(2.5, 4))
        modal_still = await page.query_selector("input[placeholder='yourhandle']")
        body = ((await page.text_content("body")) or "").lower()
        error_markers = (
            "couldn't find", "not found", "invalid", "could not",
            "unable to load", "please check the username",
        )
        if modal_still and any(m in body for m in error_markers):
            return False
        if modal_still:
            await asyncio.sleep(2)
            modal_still = await page.query_selector("input[placeholder='yourhandle']")
            if modal_still:
                await click_any(page, ["button:text-is('Cancel')"], timeout=3000)
                await asyncio.sleep(1)
                return False
        return True
    except Exception as e:
        print(f"[ig-modal] attempt error: {e}")
        try:
            await click_any(page, ["button:text-is('Cancel')"], timeout=3000)
        except Exception:
            pass
        return False


async def run_tokboostly_account(email, password, ig_proxies, browser,
                                 proxy_server=None, country=None,
                                 proxy_auth=None, bw_totals=None,
                                 get_fresh_proxy=None):
    context_kwargs = dict(
        viewport={"width": 1366, "height": 768},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"),
        locale="en-US",
        timezone_id=timezone_for(country[1]) if country else "America/New_York",
    )
    if proxy_server:
        context_kwargs["proxy"] = {"server": proxy_server}
        if proxy_auth:
            context_kwargs["proxy"]["username"] = proxy_auth[0]
            context_kwargs["proxy"]["password"] = proxy_auth[1]

    if proxy_server:
        ok = await asyncio.to_thread(
            preflight_proxy, proxy_server,
            proxy_auth, "tokboostly.com:443", 6)
        if not ok:
            raise ProxyDead(f"preflight failed for {proxy_server}")

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()
    bw = await install_bandwidth_saver(context, page)

    jmk_linked = False
    jmk_released = False

    async def _resurrect():
        nonlocal context, page, bw, proxy_server, proxy_auth, country, ig_proxies
        if get_fresh_proxy is None:
            return False
        try:
            state = await context.storage_state()
        except Exception as e:
            print(f"[net] could not snapshot storage state: {e}")
            state = None
        try:
            report_bandwidth(bw, f"context pre-resurrect ({email})")
            if bw and bw_totals is not None:
                bw_totals["wire"] += bw["wire"]
                bw_totals["saved"] += bw["saved"]
                bw_totals["attempts"] += 1
        except Exception:
            pass
        bw = None
        await _safe_close(context)
        try:
            fresh = await get_fresh_proxy()
        except Exception as e:
            print(f"[net] fresh proxy provider raised: {e}")
            return False
        if not fresh:
            return False
        new_server, new_auth, new_country, new_ig = fresh
        new_kwargs = dict(context_kwargs)
        if new_server:
            new_kwargs["proxy"] = {"server": new_server}
            if new_auth:
                new_kwargs["proxy"]["username"] = new_auth[0]
                new_kwargs["proxy"]["password"] = new_auth[1]
        else:
            new_kwargs.pop("proxy", None)
        if state:
            new_kwargs["storage_state"] = state
        try:
            context = await browser.new_context(**new_kwargs)
            page = await context.new_page()
            bw = await install_bandwidth_saver(context, page)
            try:
                from playwright_stealth import stealth_async
                await stealth_async(page)
            except ImportError:
                pass
        except Exception as e:
            print(f"[net] could not rebuild context: {e}")
            return False
        proxy_server = new_server
        proxy_auth = new_auth
        country = new_country
        ig_proxies = new_ig
        print(f"[net] resurrected on fresh proxy {new_server} "
              f"({'auth' if new_auth else 'no-auth'})")
        return True

    try:
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
            print("[Stealth] Applied.")
        except ImportError:
            print("[Stealth] playwright-stealth not installed - skipping.")

        # ---- [0] signup page ----
        print("\n[0] Opening TokBoostly signup...")
        try:
            await goto_with_retry(page, TOKBOOSTLY_URL,
                                  tries=GOTO_RETRIES, wait=GOTO_RETRY_WAIT,
                                  label="0")
        except ProxyDead:
            if await _resurrect():
                await goto_with_retry(page, TOKBOOSTLY_URL,
                                      tries=2, wait=GOTO_RETRY_WAIT,
                                      label="0")
            else:
                raise
        await page.wait_for_selector(
            "button:has-text('Continue with email'), "
            "button:has-text('Continue with GitHub')",
            timeout=30000)
        await asyncio.sleep(random.uniform(1.5, 3))

        # ---- [1] Continue with email ----
        print("[1] Clicking 'Continue with email'...")
        clicked = await click_any(page, [
            "button:has-text('Continue with email')",
            "button:has-text('Continue with Email')",
        ], timeout=15000)
        if not clicked:
            raise Exception("'Continue with email' button not found")
        await asyncio.sleep(random.uniform(2, 3.5))

        # ---- [2] name ----
        print("[2] Filling name field...")
        full_name = random_person_name()
        await fill_first(page, [
            "#signup-step-name",
            "input[autocomplete='name']",
            "input[placeholder='Jane Creator']",
        ], full_name, timeout=15000)
        await asyncio.sleep(random.uniform(0.4, 0.9))
        print(f"[2] Name: {full_name}")
        await _fire_validation(page, ["#signup-step-name"])
        await asyncio.sleep(0.6)
        if not await click_any(page, ["button:text-is('Continue')"], timeout=6000, scroll=False):
            await _force_enable_and_click(page, "^Continue$")
        await asyncio.sleep(random.uniform(1.5, 3))

        # ---- [3] email ----
        print("[3] Filling email...")
        await fill_first(page, [
            "#signup-step-email",
            "input[autocomplete='email']",
            "input[type='email']",
        ], email, timeout=15000)
        await asyncio.sleep(random.uniform(0.4, 0.9))
        await _fire_validation(page, ["#signup-step-email"])
        await asyncio.sleep(0.6)
        if not await click_any(page, ["button:text-is('Continue')"], timeout=6000, scroll=False):
            await _force_enable_and_click(page, "^Continue$")
        await asyncio.sleep(random.uniform(1.5, 3))

        # ---- [4] password = email, x2 ----
        print("[4] Filling password x2...")
        await fill_first(page, [
            "#signup-step-password",
            "input[autocomplete='new-password']",
        ], password, timeout=15000)
        await asyncio.sleep(random.uniform(0.4, 0.9))

        await fill_first(page, [
            "#signup-step-confirm",
            "input[autocomplete='new-password'][type='password']",
        ], password, timeout=15000)
        await asyncio.sleep(random.uniform(0.4, 0.9))

        await _fire_validation(page, ["#signup-step-password", "#signup-step-confirm"])
        await asyncio.sleep(0.8)
        print("[4] Clicking 'Create account'...")
        if not await click_any(page, [
            "button:text-is('Create account')",
            "button:has-text('Create account')",
        ], timeout=10000):
            await _force_enable_and_click(page, "create account")

        for _probe in range(3):
            try:
                cap = await page.query_selector(
                    "[data-sitekey], iframe[src*='captcha'], iframe[src*='hcaptcha'], "
                    "iframe[src*='turnstile']")
                if cap:
                    if CLOUD_MODE:
                        raise Exception(
                            "captcha shown in headless cloud mode - "
                            "the proxy is flagged for tokboostly; "
                            "aborting this attempt so the pool rotates")
                    print("[4] Captcha - solve it manually in the browser.")
                    await relax_bandwidth_saver(context)
                    await asyncio.to_thread(input, "Press Enter after solving...")
                    break
            except Exception as probe_err:
                if C
