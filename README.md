# Gabriel Santos · Telegram autoposter

Posts to @sanstostrades in Portuguese, Monday–Friday, Brazil time (BRT):

| Time  | Post |
|-------|------|
| 09:00 | Good-morning card with the main markets (gold, FX, dollar/real, bitcoin, oil, silver, S&P 500, Ibovespa) |
| 11:00 | Overview of one asset (4H chart, trend, support/resistance) + 3 related markets. The asset is yesterday's poll winner; with no votes it is the biggest mover |
| 13:00 | Trading signal from the source channel (only if it was posted in the last 90 min, during Brazilian daytime). If none: the result of an earlier signal. If none: our own chart setup. Every signal and result ends with the DM hook |
| 15:00 | Native Telegram poll "which asset should I analyse tomorrow?". It closes at the next 11:00 run; the result is sent to you and decides the 11:00 asset |
| 17:00 | Manual |
| 19:00 | Same as 13:00 |
| 21:00 | News carousel (album): cover with the day's 5 stories, one slide per story with the market's reaction, market-close card; roundup text as caption |

## Setup

1. **Bot token**: repo **Settings → Secrets and variables → Actions → `TG_BOT_TOKEN`**. The bot must be an admin of @sanstostrades.
2. **Signal source**: add the same bot as an admin of the source channel (AFX | Сигналы). `signal_source` in `channels.json` is matched against the source channel's title or id.
3. **DM hook**: put your Telegram handle in `channels.json` → `"dm": "@yourhandle"`.
4. **Poll reports and alerts by Telegram**: open the bot in Telegram and press **Start** (as the `dm` handle). The next run learns your chat id automatically. Reports are also posted as comments on the "📊 Relatório de enquetes" issue.
5. **AI copywriting** (optional): add the secret `ANTHROPIC_API_KEY`. Without it every post uses the built-in Portuguese templates.

## When something breaks

A failed run opens (or comments on) a GitHub issue labelled `autoposter-alert`, which GitHub emails to you, and sends a Telegram DM if `OWNER_CHAT_ID` is set. Scheduled runs also re-enable the workflow so GitHub never pauses it for inactivity.

## Test and control

- **Actions → Post charts to Telegram → Run workflow**: pick `slot` and an hour to post that slot now. Tick *dry run* to only build the images (they are attached to the run as `charts`).
- Pause everything: **⋯ → Disable workflow**.
- GitHub can start scheduled runs 5–20 minutes late; signal freshness is measured at the moment the run actually starts.
- Locally: `python daily.py run 9` (dry run) or `python daily.py run 9 --live`.
