#!/usr/bin/env python3
"""
Boner Coin watcher - Railway + Telegram edition.

Runs 24/7 on Railway. Polls DexScreener and sends a Telegram message each time
market cap crosses a $5M mark (up or down). Thresholds live in the ALERTS variable.

Variables (Railway -> your service -> Variables):

  TELEGRAM_BOT_TOKEN   from @BotFather                                  (required)
  TELEGRAM_CHAT_ID     the chat/group id the bot replies with           (required for alerts)
  ALERTS               alerts, one per line or separated by ';'          (required)
                         mcap every 5M            <- ping at each $5M mark, up or down
                         above 0.06               <- one-shot: price above $0.06
                         below 0.03               <- one-shot: price below $0.03
                         mcap above 100M          <- one-shot on market cap

  Optional:
  TOKEN_ADDRESS  default: Boner Coin      CHAIN          default: robinhood
  TOKEN_NAME     default: BONER           POLL_SECONDS   default: 60 (min 2)
  REARM_PERCENT  default: 5               HEARTBEAT      default: on
  HEARTBEAT_HOUR default: 9               TIMEZONE       default: America/Puerto_Rico
  STEP_CUSHION_PERCENT  default: 20  (how far past a mark, as % of the step, a
                                      DOWN-cross must go before it pings; up-crosses ping at the mark)

Standard library only (plus tzdata for time zones).
"""

import html
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("STATE_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or HERE)
STATE_FILE = STATE_DIR / "state.json"
FAILURES_BEFORE_WARNING = 5
HTTP_TIMEOUT = 15
TELEGRAM_LONG_POLL = 20
DEXSCREENER_MIN_POLL = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
log = logging.getLogger("watcher")


# --------------------------------------------------------------------------- settings

def env(name, default=""):
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


def load_settings():
    cfg = {
        "chain": env("CHAIN", "robinhood").lower(),
        "address": env("TOKEN_ADDRESS", "0x98096d17e191b3da1d5f99a6d7b3584351b11e18"),
        "name": env("TOKEN_NAME", "BONER"),
        "alerts_text": os.environ.get("ALERTS", ""),
        "rearm": float(env("REARM_PERCENT", "5")) / 100.0,
        "cushion": float(env("STEP_CUSHION_PERCENT", "20")) / 100.0,
        "repeats": max(1, int(env("ALERT_REPEATS", "3"))),
        "repeat_gap": max(0.0, float(env("ALERT_REPEAT_GAP", "1"))),
        "bot_token": env("TELEGRAM_BOT_TOKEN"),
        "chat_id": env("TELEGRAM_CHAT_ID"),
        "poll": max(DEXSCREENER_MIN_POLL, int(env("POLL_SECONDS", "60"))),
        "heartbeat": env("HEARTBEAT", "on").lower() in ("on", "yes", "true", "1"),
        "heartbeat_hour": int(env("HEARTBEAT_HOUR", "8")),
        "timezone": env("TIMEZONE", "America/New_York"),
    }
    try:
        from zoneinfo import ZoneInfo
        cfg["tz"] = ZoneInfo(cfg["timezone"])
    except Exception as e:  # noqa: BLE001
        log.warning("TIMEZONE %r not usable (%s); daily check-in will use UTC", cfg["timezone"], e)
        cfg["tz"] = timezone.utc
    return cfg


# --------------------------------------------------------------------------- formatting

def fmt_price(p):
    if p is None:
        return "?"
    if p <= 0:
        return "$0"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return "$" + f"{p:.4f}".rstrip("0").rstrip(".")
    digits = max(4, -int(math.floor(math.log10(p))) + 3)
    return "$" + f"{p:.{digits}f}".rstrip("0").rstrip(".")


def fmt_big(n):
    if n is None:
        return "?"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            s = f"{v:.0f}" if v >= 100 else f"{v:.1f}" if v >= 10 else f"{v:.2f}"
            return f"${s}{suf}"
    return f"${n:,.0f}"


def fmt_value(metric, v):
    return fmt_price(v) if metric == "price" else fmt_big(v)


def fmt_mark(metric, v):
    """A round mark like $5M / $50M / $2.5M with no trailing zeros."""
    if metric == "price":
        return fmt_price(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:g}{suf}"
    return f"${v:g}"


def human_secs(s):
    return f"{s}sec" if s < 60 else f"{s // 60}min"


def human_mins(m):
    return f"{m}min" if m < 120 else f"{m // 60}hr"


# --------------------------------------------------------------------------- alerts

LINE_RE = re.compile(
    r"""^\s*(?:(?P<metric>price|mcap|mc|market\s*cap|marketcap)\s*)?
        (?P<dir>above|below|over|under|>=|<=|>|<)\s*\$?\s*
        (?P<num>\d[\d,]*\.?\d*|\.\d+)\s*(?P<suf>[kmb])?(?![\w.])\s*(?P<note>.*?)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
STEP_RE = re.compile(
    r"""^\s*(?:(?P<metric>price|mcap|mc|market\s*cap|marketcap)\s+)?
        every\s+\$?\s*(?P<num>\d[\d,]*\.?\d*|\.\d+)\s*(?P<suf>[kmb])?(?![\w.])\s*(?P<note>.*?)\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def _metric_of(raw_metric, value, suffix):
    m = (raw_metric or "").lower().replace(" ", "")
    if m in ("mcap", "mc", "marketcap"):
        return "mcap"
    if m == "price":
        return "price"
    return "mcap" if (suffix or value >= 1000) else "price"


class Threshold:
    __slots__ = ("metric", "direction", "value", "note", "key", "raw")

    def __init__(self, metric, direction, value, note, raw):
        self.metric, self.direction, self.value, self.note, self.raw = metric, direction, value, note, raw
        self.key = f"{metric}:{direction}:{value:.10g}"

    def crossed(self, v):
        return v >= self.value if self.direction == "above" else v <= self.value

    def back_on_safe_side(self, v, rearm):
        if self.direction == "above":
            return v < self.value * (1 - rearm)
        return v > self.value * (1 + rearm)

    def describe(self):
        s = f"{self.metric} {self.direction} {fmt_value(self.metric, self.value)}"
        return s + (f" - {self.note}" if self.note else "")


class StepAlert:
    """Pings each time the metric crosses another multiple of `step`."""
    __slots__ = ("metric", "step", "note", "key", "raw")

    def __init__(self, metric, step, note, raw):
        self.metric, self.step, self.note, self.raw = metric, step, note, raw
        self.key = f"step:{metric}:{step:.10g}"

    def band(self, v):
        return math.floor(v / self.step)

    def describe(self):
        s = f"{self.metric} every {fmt_mark(self.metric, self.step)}"
        return s + (f" - {self.note}" if self.note else "")


def parse_alerts(text):
    """ALERTS may use newlines or ';' as separators. Returns (thresholds, steps, bad_lines)."""
    thresholds, steps, bad, seen = [], [], [], set()
    for chunk in text.replace(";", "\n").splitlines():
        body = chunk.split("#", 1)[0].strip()
        if not body:
            continue
        sm = STEP_RE.match(body)
        if sm:
            try:
                value = float(sm.group("num").replace(",", ""))
            except ValueError:
                bad.append(body)
                continue
            if sm.group("suf"):
                value *= SUFFIX[sm.group("suf").lower()]
            if value <= 0:
                bad.append(body)
                continue
            s = StepAlert(_metric_of(sm.group("metric"), value, sm.group("suf")), value, sm.group("note").strip(), body)
            if s.key not in seen:
                seen.add(s.key)
                steps.append(s)
            continue
        m = LINE_RE.match(body)
        if not m:
            bad.append(body)
            continue
        d = m.group("dir").lower()
        direction = "above" if d in ("above", "over", ">", ">=") else "below"
        try:
            value = float(m.group("num").replace(",", ""))
        except ValueError:
            bad.append(body)
            continue
        if m.group("suf"):
            value *= SUFFIX[m.group("suf").lower()]
        if value <= 0:
            bad.append(body)
            continue
        metric = _metric_of(m.group("metric"), value, m.group("suf")) if m.group("metric") else "price"
        t = Threshold(metric, direction, value, m.group("note").strip(), body)
        if t.key not in seen:
            seen.add(t.key)
            thresholds.append(t)
    return thresholds, steps, bad


# --------------------------------------------------------------------------- price feed

def make_fetcher(cfg):
    url = f"https://api.dexscreener.com/tokens/v1/{cfg['chain']}/{cfg['address']}"

    def fetch():
        req = request.Request(url, headers={"User-Agent": "boner-watcher/3.0", "Accept": "application/json"})
        with request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            data = json.load(r)
        pairs = data if isinstance(data, list) else (data.get("pairs") or [])
        pairs = [p for p in pairs if p.get("priceUsd")]
        if not pairs:
            raise LookupError("DexScreener returned no trading pairs for this token")
        pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        mcap = pair.get("marketCap") or pair.get("fdv")
        return {
            "price": float(pair["priceUsd"]),
            "mcap": float(mcap) if mcap is not None else None,
            "symbol": (pair.get("baseToken") or {}).get("symbol") or cfg["name"],
        }

    return fetch


# --------------------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.offset = 0

    def call(self, method, http_timeout=HTTP_TIMEOUT, **params):
        data = json.dumps(params).encode("utf-8")
        req = request.Request(self.base + method, data=data, headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=http_timeout) as r:
                res = json.load(r)
        except error.HTTPError as e:
            try:
                res = json.load(e)
            except ValueError:
                res = {"ok": False, "description": f"HTTP {e.code}"}
            if e.code == 429:
                time.sleep(min(int((res.get("parameters") or {}).get("retry_after", 5)), 60))
            raise RuntimeError(f"telegram {method}: {res.get('description', 'error')} (HTTP {e.code})") from None
        if not res.get("ok"):
            raise RuntimeError(f"telegram {method}: {res.get('description', 'error')}")
        return res["result"]

    def send(self, chat_id, text_html, silent=False):
        return self.call("sendMessage", chat_id=chat_id, text=text_html, parse_mode="HTML",
                         disable_web_page_preview=True, disable_notification=silent)

    def updates(self, wait=TELEGRAM_LONG_POLL):
        res = self.call("getUpdates", http_timeout=wait + HTTP_TIMEOUT,
                        offset=self.offset, timeout=wait, allowed_updates=["message"])
        for u in res:
            self.offset = max(self.offset, u["update_id"] + 1)
        return [u["message"] for u in res if "message" in u]


# --------------------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("thresholds", {})
    st.setdefault("steps", {})
    st.setdefault("last_heartbeat", None)
    st.setdefault("failing", False)
    return st


def save_state(st):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("could not save state: %s", e)


# --------------------------------------------------------------------------- watcher

class Watcher:
    def __init__(self, cfg, fetch=None, tg=None, now=None):
        self.cfg = cfg
        self.fetch = fetch or make_fetcher(cfg)
        self.tg = tg or Telegram(cfg["bot_token"])
        self.now = now or (lambda: datetime.now(cfg["tz"]))
        self.state = load_state()
        self.thresholds, self.steps, self.bad_lines = parse_alerts(cfg["alerts_text"])
        self.tg_wait = max(1, min(TELEGRAM_LONG_POLL, cfg["poll"]))
        live_t = {t.key for t in self.thresholds}
        live_s = {s.key for s in self.steps}
        self.state["thresholds"] = {k: v for k, v in self.state["thresholds"].items() if k in live_t}
        self.state["steps"] = {k: v for k, v in self.state["steps"].items() if k in live_s}
        self.last_quote = None
        self.fail_count = 0
        self.started = time.time()

    # -- messaging (never raises) -------------------------------------------
    def send(self, text_html, silent=False):
        if not self.cfg["chat_id"]:
            log.warning("no TELEGRAM_CHAT_ID - not sent: %s", re.sub("<[^>]+>", "", text_html)[:120])
            return False
        try:
            self.tg.send(self.cfg["chat_id"], text_html, silent)
            log.info("sent%s: %s", " (silent)" if silent else "", re.sub("<[^>]+>", "", text_html).replace("\n", " / ")[:160])
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("send failed: %s", e)
            return False

    def mc(self, q):
        return fmt_big(q.get("mcap"))

    def repeat_send(self, text_html):
        """Send an important alert several times (1s apart) so it can't be missed."""
        ok = self.send(text_html)
        for _ in range(self.cfg["repeats"] - 1):
            if self.cfg["repeat_gap"] > 0:
                time.sleep(self.cfg["repeat_gap"])
            self.send(text_html)
        return ok

    # -- evaluation ---------------------------------------------------------
    def evaluate(self, q, startup=False):
        already, changed = [], False
        for t in self.thresholds:
            v = q.get(t.metric)
            if v is None:
                continue
            st = self.state["thresholds"].get(t.key)
            if st is None:
                st = {"fired": False}
                self.state["thresholds"][t.key] = st
                changed = True
                if t.crossed(v):
                    st["fired"] = True
                    already.append(t)
                continue
            if not st["fired"]:
                if t.crossed(v) and self.fire_threshold(t, q):
                    st["fired"] = True
                    changed = True
            elif t.back_on_safe_side(v, self.cfg["rearm"]):
                st["fired"] = False
                changed = True
        for s in self.steps:
            v = q.get(s.metric)
            if v is None or v <= 0:
                continue
            st = self.state["steps"].get(s.key)
            if st is None:
                self.state["steps"][s.key] = {"band": s.band(v)}
                changed = True
                continue
            last = st["band"]
            cushion = s.step * self.cfg["cushion"]
            new_band = None
            if v >= (last + 1) * s.step:                 # UP: the instant it touches the next mark
                new_band = s.band(v)
            elif v <= last * s.step - cushion:           # DOWN: only once a full cushion below the mark
                new_band = s.band(v)
            if new_band is None or new_band == last:
                continue
            if startup:
                st["band"] = new_band
                changed = True
                continue
            if self.fire_step(s, q, last, new_band):
                st["band"] = new_band
                changed = True
        if changed:
            save_state(self.state)
        return already

    def fire_threshold(self, t, q):
        arrow = "📈" if t.direction == "above" else "📉"
        live = fmt_value(t.metric, q.get(t.metric))
        text = f"{arrow} <b>{live}</b> · {t.metric} {t.direction} {fmt_value(t.metric, t.value)}"
        if t.note:
            text += f"\n▶️ <b>{html.escape(t.note)}</b>"
        return self.repeat_send(text)

    def fire_step(self, s, q, last, cur):
        up = cur > last
        edge = cur * s.step if up else (cur + 1) * s.step
        arrow = "🔼" if up else "🔽"
        text = f"{arrow} <b>{fmt_value(s.metric, q.get(s.metric))}</b> · passed {fmt_mark(s.metric, edge)}"
        if s.note:
            text += f"\n▶️ <b>{html.escape(s.note)}</b>"
        return self.repeat_send(text)

    # -- heartbeat ----------------------------------------------------------
    def maybe_heartbeat(self, q, startup=False):
        if not self.cfg["heartbeat"]:
            return
        now = self.now()
        today = now.date().isoformat()
        if now.hour < self.cfg["heartbeat_hour"] or self.state["last_heartbeat"] == today:
            return
        if startup:
            # A deploy/restart is not the 8am heartbeat. Today's slot already passed, so
            # mark it done and wait for tomorrow - the "watcher is live" banner covers launch.
            self.state["last_heartbeat"] = today
            save_state(self.state)
            return
        if self.send(f"❤️ <b>Alive</b> · {self.mc(q)}", silent=True):
            self.state["last_heartbeat"] = today
            save_state(self.state)

    # -- one price check ----------------------------------------------------
    def tick(self, startup=False):
        try:
            q = self.fetch()
        except Exception as e:  # noqa: BLE001
            self.fail_count += 1
            log.warning("fetch failed (%d in a row): %s", self.fail_count, e)
            # Only warn about a feed we had actually been receiving - never at cold start.
            if (self.fail_count == FAILURES_BEFORE_WARNING and not self.state["failing"]
                    and self.last_quote is not None):
                if self.send(f"⚠️ <b>No feed</b> · {human_secs(FAILURES_BEFORE_WARNING * self.cfg['poll'])} "
                             f"— DexScreener not responding, still retrying"):
                    self.state["failing"] = True
                    save_state(self.state)
            return None
        if self.state["failing"]:
            if not startup:
                self.send("✅ <b>Feed back</b>", silent=True)
            self.state["failing"] = False
            save_state(self.state)
        self.fail_count = 0
        self.last_quote = q
        log.info("%s %s mcap %s", q.get("symbol"), fmt_price(q["price"]), fmt_big(q.get("mcap")))
        already = self.evaluate(q, startup=startup)
        self.maybe_heartbeat(q, startup=startup)
        return q, already

    # -- telegram commands --------------------------------------------------
    def handle_messages(self):
        try:
            msgs = self.tg.updates(self.tg_wait)
        except Exception as e:  # noqa: BLE001
            log.warning("getUpdates failed: %s", e)
            time.sleep(5)
            return
        for m in msgs:
            chat_id = str((m.get("chat") or {}).get("id", ""))
            text = (m.get("text") or "").strip()
            if not chat_id:
                continue
            if not self.cfg["chat_id"]:
                self.reply(chat_id, f"Your chat id is <code>{chat_id}</code>.\n\nIn Railway add "
                                    f"<code>TELEGRAM_CHAT_ID</code> = <code>{chat_id}</code> and deploy. "
                                    f"Alerts will come here.")
                continue
            if chat_id != self.cfg["chat_id"]:
                continue
            cmd = text.split()[0].lower() if text else ""
            if cmd in ("/status", "/start", "/alive"):
                dur = human_mins(int((time.time() - self.started) / 60))
                mc = f" · {self.mc(self.last_quote)}" if self.last_quote else ""
                if self.state.get("failing"):
                    self.reply(chat_id, f"⚠️ {dur}{mc} · feed down, retrying")
                else:
                    self.reply(chat_id, f"✅ {dur}{mc}")
            else:
                self.reply(chat_id, "Send /status to check I'm alive and see the current market cap. "
                                    "Alerts are set in Railway (ALERTS).")

    def reply(self, chat_id, text_html):
        try:
            self.tg.send(chat_id, text_html, silent=True)
        except Exception as e:  # noqa: BLE001
            log.warning("reply failed: %s", e)

    # -- startup banner -----------------------------------------------------
    def startup_message(self, q):
        mc = self.mc(q) if q else "?"
        lines = ["🚀 <b>BONER watcher is live</b>",
                 f"Market cap now: <b>{mc}</b> · checking every {self.cfg['poll']} seconds"]
        s = self.steps[0] if self.steps else None
        if s:
            mark = fmt_mark(s.metric, s.step)
            cush = fmt_mark(s.metric, s.step * self.cfg["cushion"])
            lines += ["",
                      "<b>How alerts work</b>",
                      f"• A ping each time market cap crosses a {mark} mark, up or down.",
                      "• Going up: pings the moment it touches a mark.",
                      f"• Coming down: pings once it's a full {cush} below the mark, "
                      "so wobble on a line won't double-ping."]
            if self.cfg["repeats"] > 1:
                lines.append(f"• Each crossing is sent {self.cfg['repeats']}× (1s apart) so you can't miss it.")
        lines += ["", "<b>Messages you'll see</b>"]
        if s:
            lines += ["🔼 <b>$50.0M</b> · passed $50M — crossed a mark going up",
                      "🔽 <b>$49.0M</b> · passed $50M — crossed a mark going down"]
        for t in self.thresholds:
            lines.append(f"📈/📉 one-shot: {html.escape(t.describe())}")
        try:
            tzabbr = self.now().tzname() or ""
        except Exception:  # noqa: BLE001
            tzabbr = ""
        when = f"{self.cfg['heartbeat_hour']}:00" + (f" {tzabbr}" if tzabbr else "")
        lines += [f"❤️ Alive · {mc} — daily silent check around {when}, means I'm still running",
                  f"✅ · {mc} — reply to /status (✅ = feed healthy, ⚠️ = feed down)",
                  "⚠️ No feed — data dropped · ✅ Feed back — data restored"]
        return "\n".join(lines)

    # -- main loop ----------------------------------------------------------
    def run(self):
        # Wait (quietly) for the first good price so the launch banner is the first message,
        # even if DexScreener is slow to answer at cold start.
        q = None
        for _ in range(40):
            result = self.tick(startup=True)
            if result:
                q = result[0]
                break
            time.sleep(self.cfg["poll"])
        if not self.cfg["chat_id"]:
            log.warning("TELEGRAM_CHAT_ID is not set. Message the bot and it will reply with the chat id.")
        else:
            self.send(self.startup_message(q), silent=True)
        next_fetch = time.monotonic() + self.cfg["poll"]
        while True:
            try:
                self.handle_messages()          # blocks up to tg_wait seconds
                if time.monotonic() >= next_fetch:
                    self.tick()
                    next_fetch = time.monotonic() + self.cfg["poll"]
            except Exception:  # noqa: BLE001
                log.exception("unexpected error; continuing")
                time.sleep(5)


# --------------------------------------------------------------------------- entry

def main():
    cfg = load_settings()
    if not cfg["bot_token"]:
        log.error("TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and add the variable in Railway.")
        time.sleep(300)
        return 1
    tg = Telegram(cfg["bot_token"])
    try:
        me = tg.call("getMe")
        log.info("telegram bot: @%s", me.get("username"))
    except Exception as e:  # noqa: BLE001
        log.error("Telegram rejected the bot token: %s", e)
        time.sleep(300)
        return 1
    if not cfg["alerts_text"].strip():
        log.warning("ALERTS is empty - nothing to watch until you add it.")
    log.info("watching %s on %s, state in %s", cfg["address"], cfg["chain"], STATE_DIR)
    Watcher(cfg, tg=tg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
