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

Saturday and Sunday (BRT):

| Time  | Post |
|-------|------|
| 09:00 | Manual: good morning, motivational sport post with images |
| 11:00 | US and world economy carousel: up to 5 stories from the last two days (Fed, Wall Street, China, Europe, oil…), one slide each; stories already used in a carousel are skipped |
| 13:00 | Manual: motivational video |
| 15:00 | Manual: luxury car post |
| 17:00 | Manual: luxury life post |
| 19:00 | Manual: luxury dinner, family vibes post |

See **RULES.md** for the owner's rule book (no repeats, no personal info, never stop, keep the schedule).

## Setup

1. **Bot token**: repo **Settings → Secrets and variables → Actions → `TG_BOT_TOKEN`**. The bot must be an admin of @sanstostrades.
2. **Signal source**: add the same bot as an admin of the source channel (AFX | Сигналы). `signal_source` in `channels.json` is matched against the source channel's title or id.
3. **DM line**: the public handle for DM requests goes in `channels.json` → `"dm"` (empty = no DM line). Never a personal account.
4. **Poll reports and alerts by Telegram**: open the bot in Telegram and press **Start** (as the `dm` handle). The next run learns your chat id automatically. Reports are also posted as comments on the "📊 Relatório de enquetes" issue.
5. **AI copywriting** (optional): add the secret `ANTHROPIC_API_KEY`. Without it every post uses the built-in Portuguese templates.
6. **Emoji**: every asset line starts with its icon (🥇 Ouro, 🥈 Prata, 🇪🇺 EUR/USD, 🇬🇧 GBP/USD, 🇯🇵 USD/JPY, 💵 Dólar, 💰 Bitcoin, 🛢 Petróleo, 🇺🇸 S&P 500, 🇧🇷 Ibovespa), with 📈/📉 for direction, a bold title and bold labels. `emoji.json` maps these emoji to animated (custom) ones from Telegram packs. Telegram shows animated emoji in a channel only when the bot has a collectible username bought on Fragment and assigned to it. Until then readers see the ordinary emoji in the same places, and the daily report says which one is showing. Run the workflow in `emoji` mode to list packs (numbered sheets in the run's `charts` artifact) when you want to change an emoji.

## Gold prices

Charts use gold futures (GC=F) and the posts show spot gold, so every run measures the gap between the two (about $21 in
October 2026): first from Dukascopy spot ticks against 15-minute futures closes over the latest trading session; if that
feed can't be reached, from the spot prices (gold-api.com) saved by earlier runs, each set against the futures minute bar
of the same moment. If neither works, the last value measured in the previous 4 days is used. The run log shows which. Each gold signal
keeps the gap it was posted with, so its result and the repeat check judge it on the same prices. To force a value, set
the `GOLD_BASIS` environment variable.

## When something breaks

A failed run opens (or comments on) a GitHub issue labelled `autoposter-alert`, which GitHub emails to you, and sends a Telegram DM if `OWNER_CHAT_ID` is set. Scheduled runs also re-enable the workflow so GitHub never pauses it for inactivity.

## Test and control

- **Actions → Post charts to Telegram → Run workflow**: pick `slot` and an hour to post that slot now. Tick *dry run* to only build the images (they are attached to the run as `charts`).
- Pause everything: **⋯ → Disable workflow**.
- GitHub drops and delays scheduled runs, so each slot has three attempts (:07, :27, :47 past the hour; GitHub is busiest at :00). The first one that runs posts the slot; the others see it is done and stop after a few seconds. A slot missed entirely is caught up within 90 minutes.
- Locally: `python daily.py run 9` (dry run) or `python daily.py run 9 --live`.
