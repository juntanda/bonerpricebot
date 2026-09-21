#!/usr/bin/env python3
"""
Boner Coin watcher - Railway + Telegram edition.

Runs 24/7 on Railway. Polls DexScreener, compares price / market cap against the
thresholds in the ALERTS variable, and sends a Telegram message when one is crossed.

Variables (set in Railway -> your service -> Variables):

  TELEGRAM_BOT_TOKEN   from @BotFather                                  (required)
  TELEGRAM_CHAT_ID     your chat id - message the bot once and it tells you (required for alerts)
  ALERTS               thresholds, one per line or separated by ';'      (required)
                         above 0.06
                         below 0.03
                         mcap above 100M
                         mcap every 5M            <- fires each time mcap crosses another $5M, up or down
                         above 0.08 sell half     <- text after the number is shown in the alert

  Optional:
  TOKEN_ADDRESS        default: Boner Coin        CHAIN            default: robinhood
  TOKEN_NAME           default: BONER             POLL_SECONDS     default: 60
  REARM_PERCENT        default: 5                 HEARTBEAT        default: on
  HEARTBEAT_HOUR       default: 9                 TIMEZONE         default: America/Puerto_Rico

Changing a variable redeploys the service; the bot sends "Watcher started" with the
new alert list so you know it took. Standard library only (plus tzdata for time zones).
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
from urllib import error, parse, request

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("STATE_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or HERE)
STATE_FILE = STATE_DIR / "state.json"
FAILURES_BEFORE_WARNING = 5
HTTP_TIMEOUT = 15
STEP_DEADBAND = 0.05  # for "every X" alerts: value must clear a band edge by 5% of the step before firing
TELEGRAM_LONG_POLL = 20  # seconds getUpdates waits for a message; also paces the main loop

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
        "bot_token": env("TELEGRAM_BOT_TOKEN"),
        "chat_id": env("TELEGRAM_CHAT_ID"),
        "poll": max(10, int(env("POLL_SECONDS", "60"))),
        "heartbeat": env("HEARTBEAT", "on").lower() in ("on", "yes", "true", "1"),
        "heartbeat_hour": int(env("HEARTBEAT_HOUR", "9")),
        "timezone": env("TIMEZONE", "America/Puerto_Rico"),
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
    digits = max(4, -int(math.floor(math.log10(p))) + 3)  # 4 significant figures
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


def fmt_pct(x):
    return "?" if x is None else f"{x:+.1f}%"


def fmt_value(metric, v):
    return fmt_price(v) if metric == "price" else fmt_big(v)


# --------------------------------------------------------------------------- thresholds

LINE_RE = re.compile(
    r"""
    ^\s*
    (?:(?P<metric>price|mcap|mc|market\s*cap|marketcap)\s*)?
    (?P<dir>above|below|over|under|>=|<=|>|<)\s*
    \$?\s*(?P<num>\d[\d,]*\.?\d*|\.\d+)\s*
    (?P<suf>[kmb])?
    (?![\w.])
    \s*(?P<note>.*?)\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
STEP_RE = re.compile(
    r"""
    ^\s*
    (?:(?P<metric>price|mcap|mc|market\s*cap|marketcap)\s+)?
    every\s+\$?\s*(?P<num>\d[\d,]*\.?\d*|\.\d+)\s*
    (?P<suf>[kmb])?
    (?![\w.])
    \s*(?P<note>.*?)\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


class StepAlert:
    """Fires each time the metric crosses another multiple of `step` (up or down)."""
    __slots__ = ("metric", "step", "note", "key", "raw")

    def __init__(self, metric, step, note, raw):
        self.metric, self.step, self.note, self.raw = metric, step, note, raw
        self.key = f"step:{metric}:{step:.10g}"

    def band(self, v):
        return math.floor(v / self.step)

    def describe(self):
        s = f"{self.metric} every {fmt_value(self.metric, self.step)}"
        if self.note:
            s += f" - {self.note}"
        return s


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
        if self.note:
            s += f" - {self.note}"
        return s


def _metric_of(raw_metric, value, suffix):
    """Normalise the metric word. When unspecified, a K/M/B or >=1000 value means market cap."""
    m = (raw_metric or "").lower().replace(" ", "")
    if m in ("mcap", "mc", "marketcap"):
        return "mcap"
    if m == "price":
        return "price"
    return "mcap" if (suffix or value >= 1000) else "price"


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
            metric = _metric_of(sm.group("metric"), value, sm.group("suf"))
            s = StepAlert(metric, value, sm.group("note").strip(), body)
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
        req = request.Request(url, headers={"User-Agent": "boner-watcher/2.0", "Accept": "application/json"})
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
            "change24": (pair.get("priceChange") or {}).get("h24"),
            "liq": (pair.get("liquidity") or {}).get("usd"),
            "url": pair.get("url") or f"https://dexscreener.com/{cfg['chain']}/{cfg['address']}",
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
                wait = (res.get("parameters") or {}).get("retry_after", 5)
                log.warning("telegram rate limit, waiting %ss", wait)
                time.sleep(min(int(wait), 60))
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
        live_t = {t.key for t in self.thresholds}
        live_s = {s.key for s in self.steps}
        self.state["thresholds"] = {k: v for k, v in self.state["thresholds"].items() if k in live_t}
        self.state["steps"] = {k: v for k, v in self.state["steps"].items() if k in live_s}
        self.last_quote = None
        self.fail_count = 0
        self.started = time.time()

    # -- messaging (never raises) --------------------------------------------
    def send(self, text_html, silent=False):
        if not self.cfg["chat_id"]:
            log.warning("TELEGRAM_CHAT_ID not set - message not sent: %s", re.sub("<[^>]+>", "", text_html)[:120])
            return False
        try:
            self.tg.send(self.cfg["chat_id"], text_html, silent)
            log.info("sent%s: %s", " (silent)" if silent else "", re.sub("<[^>]+>", "", text_html).replace("\n", " / ")[:160])
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("send failed: %s", e)
            return False

    def alert_lines(self, mark_state=True):
        if not self.thresholds and not self.steps:
            return ["No alerts set - add the ALERTS variable in Railway."]
        out = []
        for t in self.thresholds:
            fired = self.state["thresholds"].get(t.key, {}).get("fired")
            tag = "⏸ " if (fired and mark_state) else "• "
            out.append(tag + html.escape(t.describe()))
        for s in self.steps:
            at = ""
            band = self.state["steps"].get(s.key, {}).get("band")
            if band is not None and mark_state:
                at = f" (now ~{fmt_value(s.metric, band * s.step)})"
            out.append("🔁 " + html.escape(s.describe()) + at)
        for raw in self.bad_lines:
            out.append("⚠️ not understood: <code>" + html.escape(raw) + "</code>")
        return out

    # -- evaluation ----------------------------------------------------------
    def evaluate(self, q, startup=False):
        """Returns the list of thresholds that were already crossed when first seen."""
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
                    st["fired"] = True     # reported in the startup message, not as a separate alert
                    already.append(t)
                continue
            if not st["fired"]:
                if t.crossed(v) and self.fire(t, q):
                    st["fired"] = True
                    changed = True
            elif t.back_on_safe_side(v, self.cfg["rearm"]):
                st["fired"] = False
                changed = True
                log.info("re-armed: %s (now %s)", t.describe(), fmt_value(t.metric, v))
        for s in self.steps:
            v = q.get(s.metric)
            if v is None or v <= 0:
                continue
            cur = s.band(v)
            st = self.state["steps"].get(s.key)
            if st is None:                       # first sighting - record band, never fire
                self.state["steps"][s.key] = {"band": cur}
                changed = True
                continue
            last = st["band"]
            if cur == last:
                continue
            d = s.step * STEP_DEADBAND           # ignore chatter right on a band edge
            if cur > last and v < (last + 1) * s.step + d:
                continue
            if cur < last and v > last * s.step - d:
                continue
            if startup:                          # crossed while we were offline - adopt silently
                st["band"] = cur
                changed = True
                continue
            if self.fire_step(s, q, last, cur):
                st["band"] = cur
                changed = True
        if changed:
            save_state(self.state)
        return already

    def fire(self, t, q):
        sym = html.escape(q.get("symbol") or self.cfg["name"])
        arrow = "📈" if t.direction == "above" else "📉"
        text = (f"{arrow} <b>{sym} {t.metric} {t.direction} {fmt_value(t.metric, t.value)}</b>\n"
                f"Now {fmt_price(q['price'])} | mcap {fmt_big(q.get('mcap'))} | 24h {fmt_pct(q.get('change24'))}")
        if t.note:
            text += f"\n▶️ <b>{html.escape(t.note)}</b>"
        text += f'\n<a href="{html.escape(q["url"])}">Chart</a>'
        return self.send(text)

    def fire_step(self, s, q, last, cur):
        sym = html.escape(q.get("symbol") or self.cfg["name"])
        up = cur > last
        edge = cur * s.step if up else (cur + 1) * s.step
        arrow = "🔼" if up else "🔽"
        word = "up through" if up else "down through"
        text = (f"{arrow} <b>{sym} {s.metric} {word} {fmt_value(s.metric, edge)}</b>\n"
                f"Now {fmt_price(q['price'])} | mcap {fmt_big(q.get('mcap'))} | 24h {fmt_pct(q.get('change24'))}")
        jumped = abs(cur - last)
        if jumped > 1:
            text += f"\n({jumped} × {fmt_value(s.metric, s.step)} bands in one move)"
        if s.note:
            text += f"\n▶️ <b>{html.escape(s.note)}</b>"
        text += f'\n<a href="{html.escape(q["url"])}">Chart</a>'
        return self.send(text)

    def quote_line(self, q):
        return (f"{html.escape(q.get('symbol') or self.cfg['name'])} {fmt_price(q['price'])} | "
                f"24h {fmt_pct(q.get('change24'))} | mcap {fmt_big(q.get('mcap'))} | liq {fmt_big(q.get('liq'))}")

    # -- heartbeat -----------------------------------------------------------
    def maybe_heartbeat(self, q):
        if not self.cfg["heartbeat"]:
            return
        now = self.now()
        today = now.date().isoformat()
        if now.hour < self.cfg["heartbeat_hour"] or self.state["last_heartbeat"] == today:
            return
        text = "💚 <b>Daily check-in</b>\n" + self.quote_line(q) + "\n" + "\n".join(self.alert_lines())
        if self.send(text, silent=True):
            self.state["last_heartbeat"] = today
            save_state(self.state)

    # -- one price check -----------------------------------------------------
    def tick(self, startup=False):
        try:
            q = self.fetch()
        except Exception as e:  # noqa: BLE001
            self.fail_count += 1
            log.warning("fetch failed (%d in a row): %s", self.fail_count, e)
            if self.fail_count == FAILURES_BEFORE_WARNING and not self.state["failing"]:
                mins = FAILURES_BEFORE_WARNING * self.cfg["poll"] // 60
                if self.send(f"⚠️ <b>Can't reach DexScreener</b>\nNo price for {mins} min. Last error: "
                             f"{html.escape(str(e))}\nStill running, will keep retrying."):
                    self.state["failing"] = True
                    save_state(self.state)
            return None
        if self.state["failing"]:
            self.send(f"✅ <b>Price feed back</b>\n{self.quote_line(q)}", silent=True)
            self.state["failing"] = False
            save_state(self.state)
        self.fail_count = 0
        self.last_quote = q
        log.info("%s %s mcap %s 24h %s", q.get("symbol"), fmt_price(q["price"]), fmt_big(q.get("mcap")), fmt_pct(q.get("change24")))
        already = self.evaluate(q, startup=startup)
        self.maybe_heartbeat(q)
        return q, already

    # -- telegram commands ---------------------------------------------------
    def handle_messages(self):
        try:
            msgs = self.tg.updates()
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
                self.tg_reply(chat_id, f"Your chat id is <code>{chat_id}</code>.\n\nIn Railway add the variable "
                                       f"<code>TELEGRAM_CHAT_ID</code> = <code>{chat_id}</code> and deploy. "
                                       f"Alerts will come to this chat.")
                continue
            if chat_id != self.cfg["chat_id"]:
                log.info("ignoring message from chat %s", chat_id)
                continue
            cmd = text.split()[0].lower() if text else ""
            if cmd in ("/status", "/start", "/alerts"):
                up = int((time.time() - self.started) / 60)
                head = f"✅ Running {up} min" if up < 120 else f"✅ Running {up // 60} h"
                if self.last_quote:
                    head += "\n" + self.quote_line(self.last_quote)
                self.tg_reply(chat_id, head + "\n" + "\n".join(self.alert_lines()) +
                              "\n\n<i>⏸ = fired, waiting to re-arm. Edit ALERTS in Railway to change.</i>")
            else:
                self.tg_reply(chat_id, "Commands: /status - what I'm watching and the last price.\n"
                                       "Thresholds are set in Railway (ALERTS variable).")

    def tg_reply(self, chat_id, text_html):
        try:
            self.tg.send(chat_id, text_html, silent=True)
        except Exception as e:  # noqa: BLE001
            log.warning("reply failed: %s", e)

    # -- main loop -----------------------------------------------------------
    def run(self):
        result = self.tick(startup=True)
        lines = ["🚀 <b>Watcher started</b>"]
        already = []
        if result:
            q, already = result
            lines.append(self.quote_line(q))
        lines.append(f"Checking every {self.cfg['poll']}s. Alerts:")
        lines += self.alert_lines(mark_state=False)
        if already:
            lines.append("\n⚠️ <b>Already past:</b> " + "; ".join(html.escape(t.describe()) for t in already) +
                         f"\n(No separate alert. Fires again once it re-arms and crosses.)")
        if not self.cfg["chat_id"]:
            log.warning("TELEGRAM_CHAT_ID is not set. Send the bot any message and it will reply with your chat id.")
        else:
            self.send("\n".join(lines), silent=not already)
        next_fetch = time.monotonic() + self.cfg["poll"]
        while True:
            try:
                self.handle_messages()          # blocks up to TELEGRAM_LONG_POLL seconds
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
        time.sleep(300)  # avoid a hot restart loop; Railway restarts us
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
