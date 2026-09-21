# Boner Coin watcher — Railway + Telegram

Runs 24/7 on Railway. Sends you a Telegram message when BONER crosses a threshold.
Thresholds live in one Railway variable, `ALERTS`.

## Setup (15 minutes)

1. **Create the bot** — in Telegram open **@BotFather**, send `/newbot`, pick a name and a
   username. Copy the token it gives you. Then open your new bot and press **Start**.
2. **Put this folder on GitHub** — new repo, upload these files (drag and drop works).
3. **Railway** — New Project → *Deploy from GitHub repo* → pick the repo.
4. **Variables** tab → add:
   - `TELEGRAM_BOT_TOKEN` = the token from step 1
   - `ALERTS` = your thresholds, e.g. `above 0.06; below 0.03; mcap above 100M`
   Click **Deploy**.
5. **Get your chat id** — send the bot any message. It replies with your chat id.
   Add `TELEGRAM_CHAT_ID` = that number → Deploy. "🚀 Watcher started" arrives in Telegram.

## Changing thresholds

Railway → Variables → edit `ALERTS` → Deploy. The bot restarts (a few seconds) and
sends "Watcher started" with the new list, so you know it took.

Valid lines (separate with `;` or new lines; `#` starts a comment):

```
above 0.06
below 0.03
mcap above 100M
mcap below 25M
above 0.08 sell half        <- text after the number is shown bold in the alert
```

Each alert fires once, then re-arms after price moves 5% back the other way
(`REARM_PERCENT`), so hovering at your line does not spam you.

## What you get in Telegram

- 📈 / 📉 **Threshold hit** — loud. Price, mcap, 24h change, your note, a Chart link.
- 💚 **Daily check-in** — silent, once a day after 9:00 (`HEARTBEAT_HOUR`, `TIMEZONE`).
  If this stops arriving, the service is down.
- ⚠️ **Can't reach DexScreener** after 5 failed checks, then ✅ when the feed is back.
- Send `/status` to the bot any time: last price, which alerts are armed (⏸ = fired).

## Variables

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — |
| `TELEGRAM_CHAT_ID` | yes (for alerts) | — (bot tells you) |
| `ALERTS` | yes | — |
| `TOKEN_ADDRESS` | no | Boner Coin `0x9809…1e18` |
| `CHAIN` | no | `robinhood` |
| `TOKEN_NAME` | no | `BONER` |
| `POLL_SECONDS` | no | `60` |
| `REARM_PERCENT` | no | `5` |
| `HEARTBEAT` / `HEARTBEAT_HOUR` | no | `on` / `9` |
| `TIMEZONE` | no | `America/Puerto_Rico` |

Optional: attach a **Volume** to the service (Railway → service → Volume, any mount path).
Fired/armed state then survives redeploys. Without it, the state resets on each deploy;
the startup message still tells you which alerts are already past, so nothing is lost.

## Run locally

```
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... ALERTS="above 0.06; below 0.03"
python3 main.py
```
