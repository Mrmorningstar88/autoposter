# Rule book (set by the channel owner)

These rules override everything else in this repository. Any change must keep them true.

1. **Never post the same content twice.**
   - A slot posts once per day: it is marked done in memory and every later attempt for it stops (`slots.py`).
   - A trade setup is posted once: signals are remembered by instrument, side and entry; a repeated AFX signal is skipped.
   - A signal result is posted once, and never for a signal whose post was deleted from the channel.
   - A news story is used in one carousel only (remembered for 10 days).
   - Last check before every photo or album: if its opening lines already appear in the channel's latest posts, it is not sent.
2. **Never disclose personal information.**
   - Posts never contain the owner's personal Telegram. The DM line uses only the public handle in `channels.json` → `dm`
     (empty = no DM line).
   - `owner_username` is private config, used only to recognise the owner's messages to the bot. It is never posted.
   - Poll reports and alerts go only to the owner by private message and to this private repository.
3. **Never stop posting until the owner says so.**
   - Two independent timers start every slot: GitHub's schedule (:00, :20, :40) and the Claude backup timer (:02).
   - A missed slot is caught up within 90 minutes. Errors in one step (bot check, reports, updates, memory) never block the post.
   - Failures alert the owner by Telegram DM and a GitHub issue. Scheduled runs keep the workflow enabled.
   - Only the owner pauses posting (Actions → Disable workflow, and the backup timer in Claude → Routines).
4. **Keep the schedule.** Monday–Friday, Brazil time (America/Sao_Paulo): 09:00, 11:00, 13:00, 15:00, 19:00, 21:00.
   17:00 is posted by hand by the owner.
