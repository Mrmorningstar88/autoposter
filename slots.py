"""
Which posting slot is due right now (standard library only, so the workflow can ask before installing anything).

GitHub's scheduler drops and delays runs, so every slot has several cron attempts (:07, :27, :47). The first
attempt that runs posts the slot and marks it done in state.json; the others see the mark and skip.

  python3 slots.py due [state.json]   -> prints the BRT hour of the slot to run now, or nothing
  python3 slots.py relay [state.json] -> prints 1 when a relay channel may have a new source post to relay
"""
import json, os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
SLOT_HOURS = (9, 11, 13, 15, 19, 21)     # Mon–Fri; 17:00 is posted by hand
WEEKEND_SLOT_HOURS = (11,)               # Sat–Sun: US/world economy news; the other weekend posts are manual
WINDOW_MIN = 90                          # a slot can still be caught up this long after its time


def done_key(hour, now=None):
    now = now or datetime.now(TZ)
    return f"{now:%Y-%m-%d}:{hour}"


def weekend(now):
    return now.weekday() >= 5             # Brazil time


def due(state, now=None):
    now = now or datetime.now(TZ)
    done = state.get("_done", {})
    for h in sorted(WEEKEND_SLOT_HOURS if weekend(now) else SLOT_HOURS, reverse=True):
        start = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if start <= now < start + timedelta(minutes=WINDOW_MIN):
            return None if done_key(h, now) in done else h
    return None


def relay_due(state, channels="channels.json"):
    """Cheap check before installing anything: is there an unread bot update (channel post) or a source signal the
    relay channels have not had yet? Peeking at getUpdates with the saved offset deletes nothing that is unread."""
    import urllib.parse, urllib.request
    try:
        chans = [c for c in json.load(open(channels)) if c.get("enabled", True)]
    except Exception:
        return False
    relays = [c for c in chans if c.get("mode") == "relay"]
    d = state.get("_daily", {})
    for rc in relays:
        owner = next((c for c in chans if c.get("mode") != "relay" and c.get("token_env") == rc.get("token_env")), rc)
        od, done = d.get(str(owner["chat"]), {}), d.get(str(rc["chat"]), {}).get("relayed", {})
        if any(str(s.get("id")) not in done for s in od.get("src", [])):
            return True
        token = os.getenv(rc.get("token_env", "TG_BOT_TOKEN"), "")
        if not token:
            continue
        q = urllib.parse.urlencode({"offset": od.get("offset", 0), "limit": 100, "timeout": 0,
                                    "allowed_updates": json.dumps(["channel_post", "message", "my_chat_member", "poll"])}).encode()
        try:
            j = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", q, timeout=30))
        except Exception:
            return True                                   # can't tell: run the full relay
        if any("channel_post" in u for u in j.get("result", [])):
            return True
    return False


if __name__ == "__main__":
    if sys.argv[1:2] == ["relay"]:
        path = sys.argv[2] if len(sys.argv) > 2 else "state.json"
        try:
            st = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
        except ValueError:
            st = {}
        print("1" if relay_due(st) else "")
    if sys.argv[1:2] == ["due"]:
        path = sys.argv[2] if len(sys.argv) > 2 else "state.json"
        try:
            st = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
        except ValueError:
            st = {}
        h = due(st)
        print("" if h is None else h)
