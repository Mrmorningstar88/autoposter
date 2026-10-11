"""
Which posting slot is due right now (standard library only, so the workflow can ask before installing anything).

GitHub's scheduler drops and delays runs, so every slot has several cron attempts (:07, :27, :47). The first
attempt that runs posts the slot and marks it done in state.json; the others see the mark and skip.

  python3 slots.py due [state.json]   -> prints the BRT hour of the slot to run now, or nothing
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


if __name__ == "__main__":
    if sys.argv[1:2] == ["due"]:
        path = sys.argv[2] if len(sys.argv) > 2 else "state.json"
        try:
            st = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
        except ValueError:
            st = {}
        h = due(st)
        print("" if h is None else h)
