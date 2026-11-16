# Rule book (set by the channel owner)

These rules override everything else in this repository. Any change must keep them true.

1. **Never post the same content twice.**
   - A slot posts once per day: it is marked done in memory and every later attempt for it stops (`slots.py`).
   - A trade setup is posted once: a new signal with the same instrument and side and an entry within 0.15% of one
     posted in the last 24 hours counts as a repeat and is skipped (own setups and source signals alike).
   - A signal result is posted once, and never for a signal whose post was deleted from the channel.
   - One open setup per market: no new own setup on a market whose last setup (from the past 24 hours) has no result yet.
   - A news story is used in one carousel only (remembered for 10 days).
   - Last check before every photo or album: if its opening lines already appear in the channel's latest posts, it is not sent.
2. **Never disclose personal information.**
   - Posts never contain the owner's personal Telegram. The DM line uses only the public handle in `channels.json` → `dm`
     (empty = no DM line).
   - `owner_username` is private config, used only to recognise the owner's messages to the bot. It is never posted.
   - Poll reports and alerts go only to the owner by private message and to this private repository.
3. **Never stop posting until the owner says so.**
   - Two independent timers start every slot: GitHub's schedule (:07, :27, :47) and the Claude backup timer (:02).
   - A missed slot is caught up within 90 minutes. Errors in one step (bot check, reports, updates, memory) never block the post.
   - Formatting (bold, animated emoji) never blocks a post: if Telegram rejects it or it fails, the same text goes out plain.
   - A signal slot is never left empty: with no new signal or result, it posts a market panorama of an asset not covered yet that day.
     While forex and gold are closed (Friday from 17:00 New York time, so the Friday 19:00 slot), no new entry is posted:
     the slot shows a result or a panorama.
   - Failures alert the owner by Telegram DM and a GitHub issue. Scheduled runs keep the workflow enabled.
   - Only the owner pauses posting (Actions → Disable workflow, and the backup timer in Claude → Routines).
4. **Keep the schedule.** Monday–Friday, Brazil time (America/Sao_Paulo): 09:00, 11:00, 13:00, 15:00, 19:00, 21:00.
   17:00 is posted by hand by the owner. Saturday and Sunday: 11:00 US and world economy news; the owner posts the rest
   of the weekend (09:00, 13:00, 15:00, 17:00, 19:00) by hand.
