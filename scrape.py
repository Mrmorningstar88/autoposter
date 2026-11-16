"""
Forex channel automation pipeline (single file).

  python scrape.py scrape [max_posts]   -> posts.json   (public t.me/s/ previews)
  python scrape.py analyze              -> analytics.json + dashboard charts in charts/
  python scrape.py charts               -> postable branded charts in charts/post_*.png (+ captions)
  python scrape.py post [--live]        -> sends charts/captions.json charts to every channel in channels.json
  python scrape.py channels             -> checks the bot is admin and can post in each channel
  python scrape.py all                  -> scrape + analyze + charts (+ post if --live)
  python scrape.py slot [hour] [--live] -> post whatever SCHEDULE says for this hour (Brazil time). Cron it hourly:
        CRON_TZ=America/Sao_Paulo
        0 * * * 1-5  cd /path/forex_bot && TG_BOT_TOKEN=... python3 scrape.py slot --live   (channels come from channels.json)

Env for posting: TG_BOT_TOKEN (from @BotFather; bot must be admin of the channel), TG_CHANNEL (@yourchannel or -100id)
"""
import bisect, json, os, re, sys, time, glob, statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------- config
CHANNELS = ["professorklinkov", "wealthytradesacademy", "ATsignalfxPro", "marshallfx_free_channel",
            "FXGOAT1", "firepipsignals", "boomingbullscompany", "tradersparadise", "estorofxs", "TradingForexSPE"]
BRAND = os.getenv("BRAND", "YOUR BRAND")            # shown on every chart
HANDLE = os.getenv("TG_CHANNEL", "@yourchannel")
from zoneinfo import ZoneInfo
TZ = ZoneInfo(os.getenv("POST_TZ", "America/Sao_Paulo"))   # blogger is in Brazil (BRT, UTC-3)
TZ_LABEL = os.getenv("TZ_LABEL", "BRT")
LOOKBACK_H = 72                                     # signals window used for the consensus chart
THEME = dict(bg="#0B1220", panel="#111A2E", grid="#1E2A44", text="#E6EAF2", muted="#8A96AD",
             gold="#F2B33D", buy="#22C38E", sell="#F0506E", accent="#5B8CFF")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
HERE = os.path.dirname(os.path.abspath(__file__))
CHART_DIR = os.path.join(HERE, "charts")


# ================================================================= 1. SCRAPE
def num(s):
    s = (s or "").strip().upper().replace(",", "").replace(" ", "")
    m = re.match(r"([\d.]+)([KM]?)", s)
    return int(float(m.group(1)) * {"K": 1e3, "M": 1e6, "": 1}[m.group(2)]) if m else 0


def parse(html, channel):
    soup = BeautifulSoup(html, "html.parser")
    info = {}
    for c in soup.select(".tgme_channel_info_counter"):
        t = c.select_one(".counter_type")
        if t and "subscriber" in t.text:
            info["subscribers"] = num(c.select_one(".counter_value").text)
    posts = []
    for w in soup.select(".tgme_widget_message"):
        pid = w.get("data-post", "")
        if "/" not in pid:
            continue
        txt_el = w.select_one(".tgme_widget_message_text")
        text = txt_el.get_text("\n", strip=True) if txt_el else ""
        v = w.select_one(".tgme_widget_message_views")
        reactions = {}
        for r in w.select(".tgme_reaction"):
            emo = r.select_one("b, i.emoji, tg-emoji")
            label = (emo.get_text(strip=True) if emo else "") or "custom"
            reactions[label] = reactions.get(label, 0) + num(r.get_text(strip=True).replace(label, ""))
        t = w.select_one("time")
        media = [k for k, sel in [("photo", ".tgme_widget_message_photo_wrap"),
                                  ("video", ".tgme_widget_message_video_player, video"),
                                  ("voice", ".tgme_widget_message_voice"),
                                  ("document", ".tgme_widget_message_document"),
                                  ("poll", ".tgme_widget_message_poll")] if w.select_one(sel)]
        photo_urls = [m.group(1) for el in w.select(".tgme_widget_message_photo_wrap")
                      for m in [re.search(r"url\('([^']+)'\)", el.get("style", ""))] if m]
        posts.append({"channel": channel, "id": int(pid.split("/")[1]), "url": f"https://t.me/{pid}",
                      "photo_urls": photo_urls,
                      "date": t.get("datetime") if t else None, "views": num(v.text) if v else 0,
                      "reactions": reactions, "reactions_total": sum(reactions.values()), "media": media,
                      "text": text, "forwarded": bool(w.select_one(".tgme_widget_message_forwarded_from"))})
    return info, posts


def get(url, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=30, allow_redirects=False)
            if r.status_code == 200:
                return r
            if r.status_code in (301, 302):        # web preview disabled for this channel
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


def scrape_channel(channel, max_posts):
    allp, info, before = {}, {}, None
    while len(allp) < max_posts:
        r = get(f"https://t.me/s/{channel}" + (f"?before={before}" if before else ""))
        if r is None:
            if not allp:
                info["status"] = "web preview disabled / unreachable"
            break
        i, posts = parse(r.text, channel)
        info = info or i
        new = [p for p in posts if p["id"] not in allp]
        if not new:
            break
        allp.update({p["id"]: p for p in new})
        before = min(p["id"] for p in posts)
        if before <= 1:
            break
        time.sleep(1.0)
    return info, sorted(allp.values(), key=lambda p: p["id"])


def cmd_scrape(max_posts=200):
    out = {}
    for c in CHANNELS:
        info, posts = scrape_channel(c, max_posts)
        if not posts:  # fall back to subscriber count from the public profile page
            r = get(f"https://t.me/{c}")
            if r is not None:
                m = re.search(r'tgme_page_extra">([\d\s]+) subscribers', r.text)
                if m:
                    info["subscribers"] = int(m.group(1).replace(" ", ""))
        out[c] = {"info": info, "posts": posts}
        print(f"  {c:26s} {len(posts):4d} posts  subs={info.get('subscribers')}  {info.get('status', '')}", flush=True)
    json.dump(out, open(os.path.join(HERE, "posts.json"), "w"), ensure_ascii=False, indent=1)


# ================================================================= 2. CLASSIFY + EXTRACT
INSTR = [("XAUUSD", r"\b(XAU\s*/?\s*USD|GOLD|ORO|ЗОЛОТ\w*)\b"), ("EURUSD", r"\bEUR\s*/?\s*USD\b"),
         ("GBPUSD", r"\bGBP\s*/?\s*USD\b"), ("USDJPY", r"\bUSD\s*/?\s*JPY\b"), ("BTCUSD", r"\b(BTC|BITCOIN|БИТКОИН)\w*"),
         ("US30", r"\b(US30|DOW)\b"), ("NAS100", r"\b(NAS100|NASDAQ|US100)\b"), ("XAGUSD", r"\b(XAG\s*/?\s*USD|SILVER|СЕРЕБР\w*)\b"),
         ("BRENT", r"\b(BRENT|UKOIL|XBR\s*/?\s*USD)\b"), ("WTI", r"\b(USOIL|WTI|XTI\s*/?\s*USD|CRUDE|OIL|НЕФТ\w*)\b"),
         ("ETHUSD", r"\b(ETH|ETHEREUM|ЭФИР\w*)\b"), ("US500", r"\b(US500|SPX500|SP500|S&P\s*500)\b"),
         ("GER40", r"\b(GER40|GER30|DE40|DAX)\b"), ("UK100", r"\b(UK100|FTSE)\b"), ("JP225", r"\b(JP225|JPN225|NIKKEI)\b")]
CATS = [  # first match wins
    ("personal/story", r"(MY DAUGHTER|MY SON|MY WIFE|BIRTHDAY|MY FAMILY|HIJA|HIJO|CUMPLEAÑOS|MI FAMILIA|ДОЧ|СЫН|ЖЕН|ДЕНЬ РОЖДЕНИЯ|ИСПОЛНИЛОСЬ|ПАПОЙ|ОПЕРАЦИ|RETIRAR|WITHDRAW|ВЫВЕЛ|ВЫВОД)"),
    ("giveaway/contest", r"(GIVEAWAY|REGALAR|REGALO|SORTEO|РОЗЫГРЫШ|ПОДАР|PRIZE|BATTLE|CONTEST|COMPETITION)"),
    ("signal", r"\b(BUY|SELL)\b.*\b(SL|TP\d?|STOP\s*LOSS|TARGET)\b|\b(SL|TP\d?)\b.*\b(BUY|SELL)\b"),
    ("result", r"(TP\s*\d?\s*(HIT|DONE|✅)|\d+\s*PIPS|PROFIT|1:\d\s*DONE|STOP\s*LOSS\s*HIT|SL\s*HIT|RUNNING|\+\$?\d{2,}|BOOK\s*PARTIAL|ПРИБЫЛ|РЕЗУЛЬТАТ)"),
    ("analysis", r"(ANALYSIS|PLAN|SETUP|BIAS|STRUCTURE|CHOCH|BOS|LIQUIDITY|SUPPORT|RESISTANCE|OUTLOOK|FOMC|NFP|CPI|FED|ФРС|ИНФЛЯЦ|НЕФТ|ИРАН|РАЗБОР|АНАЛИЗ)"),
    ("live", r"\b(LIVE|STREAM|WEBINAR|ЭФИР|ТРАНСЛЯЦ|УРОК|ВЕБИНАР)\b"),
    ("promo", r"(HTTPS?://|JOIN|VIP|PREMIUM|DISCOUNT|COUPON|CODE|OFF\b|SLOTS|DM\b|@\w+|ЗАПИС|РЕГИСТРАЦ|ПИШИТЕ|PINCHA|ÚNETE|UNIRTE)"),
    ("greeting", r"^(GOOD\s*(MORNING|EVENING|DAY|NIGHT)|GM\b|HAPPY|ДОБР)"),
]


def classify(p):
    t = p["text"].upper()
    if not t.strip():
        return "media-only"
    for name, rx in CATS:
        if re.search(rx, t, re.S):
            return name
    return "education/other"


def instrument(text):
    t = text.upper()
    for name, rx in INSTR:
        if re.search(rx, t):
            return name
    return None


_N = r"(\d{3,5}(?:\.\d{1,3})?)"


def extract_signal(text):
    """Parse BUY/SELL signals -> dict(side, entry_lo, entry_hi, tps[], sl). Prices in instrument units."""
    t = text.upper().replace(",", ".")
    side = re.search(r"\b(BUY|SELL)\b", t)
    if not side:
        return None
    after = t[side.end():]
    tp_pos = re.search(r"\b(TP|TARGET|SL|STOP)", after)
    entry_zone = after[:tp_pos.start()] if tp_pos else after[:40]
    entries = [float(x) for x in re.findall(_N, entry_zone)][:2]
    tps = [float(x) for x in re.findall(r"(?:TP(?:\s?\d\b)?\s*[:\-]?\s*|TARGET\s*[:\-]?\s*)" + _N, t)]
    sl = re.search(r"(?:SL|STOP\s*LOSS)\s*[:\-]?\s*" + _N, t)
    if not entries and not sl:
        return None
    sig = {"side": side.group(1), "entry_lo": min(entries) if entries else None,
           "entry_hi": max(entries) if entries else None, "tps": tps, "sl": float(sl.group(1)) if sl else None}
    return sig


def enrich(data):
    rows = []
    for ch, v in data.items():
        posts = v["posts"]
        subs = v["info"].get("subscribers") or 0
        views = [p["views"] for p in posts]
        for i, p in enumerate(posts):
            # age-fair baseline: median views of the 30 neighbouring posts (older posts accumulate views)
            nb = views[max(0, i - 15): i + 16]
            base = st.median(nb) or 1
            dt = datetime.fromisoformat(p["date"]).astimezone(TZ) if p["date"] else None
            r = dict(p)
            r.update(subs=subs, cat=classify(p), instr=instrument(p["text"]),
                     sig=extract_signal(p["text"]) if instrument(p["text"]) in (None, "XAUUSD", "EURUSD", "GBPUSD") else None,
                     view_index=p["views"] / base, react_rate=(p["reactions_total"] / p["views"]) if p["views"] else 0,
                     reach=(p["views"] / subs) if subs else None, hour=dt.hour if dt else None,
                     dow=dt.strftime("%a") if dt else None, dt=dt.isoformat() if dt else None,
                     length=len(p["text"]), has_media=bool(p["media"]),
                     emoji_count=len(re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", p["text"])))
            rows.append(r)
    # react-rate index within channel (channels with reactions disabled -> neutral 1.0)
    by = defaultdict(list)
    for r in rows:
        by[r["channel"]].append(r)
    for ch, rs in by.items():
        med = st.median([r["react_rate"] for r in rs]) if any(r["react_rate"] for r in rs) else 0
        for r in rs:
            r["react_index"] = (r["react_rate"] / med) if med else 1.0
            # composite engagement score (1.0 = typical post for that channel)
            r["score"] = 0.5 * r["view_index"] + 0.5 * r["react_index"]
        q75 = sorted(r["score"] for r in rs)[int(len(rs) * 0.75)] if rs else 0
        for r in rs:
            r["top_q"] = r["score"] >= q75          # in the top 25% of its own channel
    return rows


# ================================================================= 3. ANALYTICS
def agg(rows, key, min_n=5):
    g = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            g[r[key]].append(r)
    out = {k: {"n": len(v), "hit_rate": round(100 * sum(x["top_q"] for x in v) / len(v), 1),
               "lift": round(min(5, st.mean([min(x["score"], 5) for x in v])), 3),
               "score": round(st.median([x["score"] for x in v]), 3),
               "view_index": round(st.median([x["view_index"] for x in v]), 3),
               "react_rate": round(st.median([x["react_rate"] for x in v]), 4)} for k, v in g.items() if len(v) >= min_n}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["hit_rate"]))


def channel_table(data, rows):
    out = []
    for ch, v in data.items():
        rs = [r for r in rows if r["channel"] == ch]
        subs = v["info"].get("subscribers")
        if not rs:
            out.append({"channel": ch, "subs": subs, "posts": 0, "status": v["info"].get("status", "no data")})
            continue
        dts = sorted(datetime.fromisoformat(r["dt"]) for r in rs if r["dt"])
        days = max(1, (dts[-1] - dts[0]).days) if dts else 1
        out.append({"channel": ch, "subs": subs, "posts": len(rs), "posts_per_day": round(len(rs) / days, 1),
                    "median_views": int(st.median([r["views"] for r in rs])),
                    "reach_pct": round(100 * st.median([r["reach"] for r in rs if r["reach"]]), 1) if subs else None,
                    "react_per_1k_views": round(1000 * st.median([r["react_rate"] for r in rs]), 1),
                    "top_category": Counter(r["cat"] for r in rs).most_common(1)[0][0]})
    return sorted(out, key=lambda x: -(x.get("reach_pct") or 0) * (x.get("react_per_1k_views") or 0.1))


def cmd_analyze():
    data = json.load(open(os.path.join(HERE, "posts.json")))
    rows = enrich(data)
    # channels whose median post reaches <3% of subscribers have unreliable audiences -> excluded from content stats
    low_reach = {ch for ch, v in data.items() if v["posts"] and v["info"].get("subscribers")
                 and st.median([p["views"] for p in v["posts"]]) / v["info"]["subscribers"] < 0.03}
    pinned_like = [r for r in rows if r["view_index"] > 6]          # pinned/viral outliers
    rows_c = [r for r in rows if r["channel"] not in low_reach]
    res = {
        "generated": datetime.now(TZ).isoformat(timespec="minutes"),
        "posts_analyzed": len(rows_c), "viral_outliers": len(pinned_like),
        "low_reach_channels_excluded": sorted(low_reach),
        "channels": channel_table(data, rows),
        "by_category": agg(rows_c, "cat"),
        "by_media": agg([dict(r, m=(r["media"][0] if r["media"] else "text-only")) for r in rows_c], "m"),
        "by_hour_local": dict(sorted(agg(rows_c, "hour", 8).items())),
        "by_weekday": agg(rows_c, "dow"),
        "by_instrument": agg(rows_c, "instr"),
        "by_length": agg([dict(r, L=("short <80" if r["length"] < 80 else "medium 80-300" if r["length"] < 300 else "long 300+")) for r in rows_c], "L"),
        "top_posts": [{k: r[k] for k in ("channel", "url", "views", "reactions_total", "score", "cat", "media", "dt")}
                      | {"text": r["text"][:180]} for r in sorted(rows_c, key=lambda r: -r["score"])[:25]],
    }
    json.dump(res, open(os.path.join(HERE, "analytics.json"), "w"), ensure_ascii=False, indent=1, default=str)
    json.dump(rows, open(os.path.join(HERE, "rows.json"), "w"), ensure_ascii=False, default=str)
    dashboard_charts(res, rows_c)
    print(f"  analyzed {len(rows_c)} posts (excluded low-reach: {sorted(low_reach)}) -> analytics.json")
    return res


# ================================================================= 4. CHART DESIGN SYSTEM
def _mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.facecolor": THEME["panel"],
                         "figure.facecolor": THEME["bg"], "axes.edgecolor": THEME["grid"], "axes.labelcolor": THEME["muted"],
                         "xtick.color": THEME["muted"], "ytick.color": THEME["muted"], "text.color": THEME["text"],
                         "axes.grid": True, "grid.color": THEME["grid"], "grid.linewidth": 0.6,
                         "axes.spines.top": False, "axes.spines.right": False, "axes.axisbelow": True})
    return plt


def frame(fig, title, subtitle, source="Source: public Telegram channel previews"):
    fig.text(0.04, 0.955, title, fontsize=20, fontweight="bold", color=THEME["text"], va="top")
    fig.text(0.04, 0.905, subtitle, fontsize=11.5, color=THEME["muted"], va="top")
    fig.text(0.04, 0.025, source, fontsize=9, color=THEME["muted"])
    fig.text(0.96, 0.025, f"{BRAND}  ·  {HANDLE}", fontsize=10, color=THEME["gold"], ha="right", fontweight="bold")
    fig.patches.append(__import__("matplotlib").patches.Rectangle((0, 0.985), 1, 0.015, transform=fig.transFigure,
                                                                  color=THEME["gold"]))


def save(fig, name):
    os.makedirs(CHART_DIR, exist_ok=True)
    p = os.path.join(CHART_DIR, name)
    fig.savefig(p, dpi=160, facecolor=THEME["bg"])
    __import__("matplotlib.pyplot").pyplot.close(fig)
    return p


def hbar(ax, labels, values, highlight=None, fmt="{:.2f}×", ref=1.0):
    cols = [THEME["gold"] if (highlight and l in highlight) else THEME["accent"] for l in labels]
    y = range(len(labels))[::-1]
    ax.barh(list(y), values, color=cols, height=0.62)
    ax.set_yticks(list(y)); ax.set_yticklabels(labels)
    ax.grid(axis="y", visible=False)
    if ref is not None:
        ax.axvline(ref, color=THEME["muted"], ls="--", lw=1)
    for yi, v in zip(y, values):
        ax.text(v, yi, "  " + fmt.format(v), va="center", fontsize=10, color=THEME["text"])
    ax.set_xlim(0, max(values) * 1.22)


def dashboard_charts(res, rows):
    plt = _mpl()
    # A. what content wins
    cats = res["by_category"]
    fig, axs = plt.subplots(1, 2, figsize=(13, 6.8)); fig.subplots_adjust(left=0.19, right=0.97, top=0.8, bottom=0.12, wspace=0.42)
    top2 = list(cats)[:2]
    hbar(axs[0], [f"{k}  (n={v['n']})" for k, v in cats.items()], [v["hit_rate"] for v in cats.values()],
         highlight=[f"{k}  (n={cats[k]['n']})" for k in top2], fmt="{:.0f}%", ref=25)
    axs[0].set_title("Hit rate by post type", loc="left", color=THEME["text"], fontsize=12)
    med = res["by_media"]
    hbar(axs[1], [f"{k}  (n={v['n']})" for k, v in med.items()], [v["hit_rate"] for v in med.values()],
         highlight=[f"{k}  (n={v['n']})" for k, v in list(med.items())[:1]], fmt="{:.0f}%", ref=25)
    axs[1].set_title("Hit rate by format", loc="left", color=THEME["text"], fontsize=12)
    frame(fig, "What forex audiences engage with", f"% of posts landing in their channel's top 25% for views + reactions · dashed = 25% (average) · {res['posts_analyzed']} posts")
    save(fig, "dash_1_content.png")

    # B. best hours (local posting timezone)
    hrs = res["by_hour_local"]
    fig, ax = plt.subplots(figsize=(13, 6.2)); fig.subplots_adjust(left=0.07, right=0.97, top=0.8, bottom=0.14)
    xs = [int(h) for h in hrs]; hrs = {int(h): v for h, v in hrs.items()}; ys = [hrs[h]["hit_rate"] for h in xs]
    best = sorted(xs, key=lambda h: -hrs[h]["hit_rate"])[:3]
    ax.bar(xs, ys, color=[THEME["gold"] if h in best else THEME["accent"] for h in xs], width=0.7)
    ax.axhline(25, color=THEME["muted"], ls="--", lw=1)
    ax.set_xticks(xs); ax.set_xticklabels([f"{h:02d}" for h in xs]); ax.set_xlabel(f"Hour posted ({TZ_LABEL})")
    ax.set_ylabel("% of posts in channel's top 25%"); ax.grid(axis="x", visible=False)
    for h in xs:
        ax.text(h, 1, f"n={hrs[h]['n']}", ha="center", va="bottom", fontsize=7.5, color=THEME["bg"])
    for h in best:
        ax.text(h, hrs[h]["hit_rate"], f"{hrs[h]['hit_rate']:.0f}%", ha="center", va="bottom", color=THEME["gold"], fontsize=10, fontweight="bold")
    frame(fig, "When to post", "Share of posts at each hour that beat 75% of their channel · gold = top 3 hours · dashed = average · hours <8 posts hidden")
    save(fig, "dash_2_hours.png")

    # C. channel benchmark
    chs = [c for c in res["channels"] if c.get("posts")]
    fig, ax = plt.subplots(figsize=(13, 6.8)); fig.subplots_adjust(left=0.08, right=0.97, top=0.8, bottom=0.12)
    low = set(res.get("low_reach_channels_excluded", []))
    placed = []
    for c in sorted(chs, key=lambda c: (c["reach_pct"] or 0, c["react_per_1k_views"])):
        x, y = c["reach_pct"] or 0, c["react_per_1k_views"]
        col = THEME["muted"] if c["channel"] in low else THEME["gold"]
        ax.scatter(x, y, s=max(60, (c["subs"] or 1) / 600), color=col, alpha=0.85, edgecolor=THEME["text"], lw=0.6)
        dy = 6
        for px, py in placed:                              # nudge labels that would collide
            if abs(px - x) < 4 and abs(py - y) < 4:
                dy = -30
        placed.append((x, y))
        tag = "  (low reach — likely inactive subs)" if c["channel"] in low else ""
        ax.annotate(f"{c['channel']}{tag}\n{(c['subs'] or 0)/1000:.0f}K subs · {c['posts_per_day']}/day", (x, y),
                    textcoords="offset points", xytext=(10, dy), fontsize=9.5, color=col if tag else THEME["text"])
    ax.set_xlabel("Reach: median views ÷ subscribers (%)"); ax.set_ylabel("Reactions per 1,000 views (median)")
    ax.set_xlim(0, max((c["reach_pct"] or 0) for c in chs) * 1.25); ax.set_ylim(bottom=-1)
    frame(fig, "Competitor benchmark", "Top-right = audience sees AND reacts · bubble size = subscribers · 0 reactions = reactions disabled")
    save(fig, "dash_3_channels.png")


# ================================================================= 5. POSTABLE CHARTS (own design, built on analyst consensus)
def load_price(ticker="GC=F", days=5, interval="1h"):
    import yfinance as yf
    df = yf.download(ticker, period=f"{days}d", interval=interval, progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.tz_convert(TZ)
    return df[["Open", "High", "Low", "Close"]].dropna()


def gold_signals(rows, hours):
    now = datetime.now(TZ)
    out = []
    for r in rows:
        s = r.get("sig")
        if not s or r["instr"] != "XAUUSD" or not r["dt"]:
            continue
        dt = datetime.fromisoformat(r["dt"])
        lv = [x for x in [s["entry_lo"], s["entry_hi"], s["sl"], *s["tps"]] if x]
        if not lv or not all(2500 < x < 7000 for x in lv):
            continue
        if now - dt <= timedelta(hours=hours):
            out.append(dict(s, channel=r["channel"], dt=dt, score=r["score"]))
    # channels repost the same setup many times -> keep one copy per (channel, side, entry, SL)
    seen, uniq = set(), []
    for s in sorted(out, key=lambda x: x["dt"]):
        k = (s["channel"], s["side"], s["entry_lo"], s["entry_hi"], s["sl"])
        if k not in seen:
            seen.add(k); uniq.append(s)
    return uniq


def futures_basis(price, sigs):
    """Gold futures (GC=F) trade at a premium to spot XAUUSD used by signal channels.
    Estimate it from signals: entry mid vs futures close at the time each signal was posted."""
    diffs = []
    for s in sigs:
        if s["entry_lo"] is None:
            continue
        i = price.index.searchsorted(s["dt"])
        if 0 < i < len(price):
            diffs.append(price["Close"].iloc[i - 1] - (s["entry_lo"] + s["entry_hi"]) / 2)
    return st.median(diffs) if len(diffs) >= 3 else 0.0


def cluster(levels, width):
    """1-D clustering of price levels -> list of (center, count)."""
    lv = sorted(levels)
    groups, cur = [], [lv[0]] if lv else []
    for x in lv[1:]:
        if x - cur[-1] <= width:
            cur.append(x)
        else:
            groups.append(cur); cur = [x]
    if cur:
        groups.append(cur)
    return [(st.median(g), len(g)) for g in groups]


def chart_gold_consensus(rows):
    plt = _mpl()
    import matplotlib.patches as mp
    price = load_price("GC=F", days=5, interval="1h")
    sigs = gold_signals(rows, LOOKBACK_H)
    if len(sigs) < 3:
        sigs = gold_signals(rows, 24 * 7)
    basis = futures_basis(price, sigs)
    spot = price - basis
    last = float(spot["Close"].iloc[-1])
    buys = [s for s in sigs if s["side"] == "BUY"]; sells = [s for s in sigs if s["side"] == "SELL"]
    width = max(3.0, last * 0.0012)
    buy_entries = cluster([(s["entry_lo"] + s["entry_hi"]) / 2 for s in buys if s["entry_lo"]], width)
    sell_entries = cluster([(s["entry_lo"] + s["entry_hi"]) / 2 for s in sells if s["entry_lo"]], width)
    sls = cluster([s["sl"] for s in sigs if s["sl"]], width)
    lo, hi = float(spot["Low"].min()), float(spot["High"].max())
    pad = (hi - lo) * 0.08

    fig = plt.figure(figsize=(13, 7.4))
    ax = fig.add_axes([0.07, 0.11, 0.66, 0.72]); side = fig.add_axes([0.77, 0.11, 0.19, 0.72])
    x = range(len(spot))
    for i, (o, h, l, c) in enumerate(spot[["Open", "High", "Low", "Close"]].itertuples(index=False)):
        col = THEME["buy"] if c >= o else THEME["sell"]
        ax.vlines(i, l, h, color=col, lw=0.9)
        ax.add_patch(mp.Rectangle((i - 0.32, min(o, c)), 0.64, max(abs(c - o), 0.15), color=col))
    # consensus zones (only clusters inside / near the visible range)
    def strongest(levels, k):
        lv = [t for t in levels if lo - pad * 3 < t[0] < hi + pad * 3]
        multi = [t for t in lv if t[1] >= 2]                 # consensus = 2+ setups at the same level
        return sorted(multi or lv, key=lambda t: -t[1])[:k]
    labels = []                                            # (price, text, color, style)
    for c, n in strongest(buy_entries, 2):
        labels.append((c, f"BUY {c:,.0f} · {n} setup{'s' if n > 1 else ''}", THEME["buy"], "zone"))
    for c, n in strongest(sell_entries, 2):
        labels.append((c, f"SELL {c:,.0f} · {n} setup{'s' if n > 1 else ''}", THEME["sell"], "zone"))
    for c, n in strongest(sls, 1):
        labels.append((c, f"stops {c:,.0f}", THEME["muted"], "stop"))
    min_gap = (hi - lo + 2 * pad) * 0.045                  # keep labels from overlapping
    placed = []
    for c, text, col, style in sorted(labels, key=lambda t: t[0]):
        end = len(spot) - 0.5                              # lines stop at the last candle; labels live in the margin
        if style == "zone":
            ax.fill_between([-2, end], c - width / 2, c + width / 2, color=col, alpha=0.18, lw=0)
            ax.hlines(c, -2, end, color=col, lw=1.2)
        else:
            ax.hlines(c, -2, end, color=col, lw=1, ls=":")
        y = c if not placed or c - placed[-1] >= min_gap else placed[-1] + min_gap
        placed.append(y)
        ax.annotate(text, xy=(len(spot) - 0.5, c), xytext=(len(spot) + 1.5, y), va="center", fontsize=9.5,
                    color=col, fontweight="bold", arrowprops=dict(arrowstyle="-", color=col, lw=0.6))
    ax.axhline(last, color=THEME["gold"], lw=1.2, ls="--")
    ax.text(len(spot) + 1.5, last, f" NOW {last:,.1f} ", ha="left", va="center", color=THEME["bg"], fontsize=9.5, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.25", fc=THEME["gold"], ec="none"))
    ticks = list(range(0, len(spot), max(1, len(spot) // 6)))
    ax.set_xticks(ticks); ax.set_xticklabels([spot.index[i].strftime("%a %H:%M") for i in ticks])
    ax.set_xlim(-2, len(spot) + 26); ax.set_ylim(lo - pad, hi + pad)
    ax.grid(axis="x", visible=False)

    # side panel: sentiment
    nb, ns = len(buys), len(sells)
    side.set_facecolor(THEME["bg"]); side.axis("off")
    side.text(0, 0.98, "Analyst sentiment", fontsize=13, fontweight="bold", va="top")
    side.text(0, 0.92, f"last {LOOKBACK_H}h · {len(sigs)} gold signals", fontsize=10, color=THEME["muted"], va="top")
    tot = max(1, nb + ns)
    side.add_patch(mp.Rectangle((0, 0.80), nb / tot, 0.06, color=THEME["buy"], transform=side.transAxes))
    side.add_patch(mp.Rectangle((nb / tot, 0.80), ns / tot, 0.06, color=THEME["sell"], transform=side.transAxes))
    side.text(0, 0.76, f"{100*nb/tot:.0f}% BUY", color=THEME["buy"], fontsize=12, fontweight="bold", va="top")
    side.text(1, 0.76, f"{100*ns/tot:.0f}% SELL", color=THEME["sell"], fontsize=12, fontweight="bold", va="top", ha="right")
    bias = "BULLISH" if nb > ns * 1.5 else "BEARISH" if ns > nb * 1.5 else "MIXED"
    side.text(0, 0.64, "Analyst bias", color=THEME["muted"], fontsize=10)
    side.text(0, 0.535, bias, color={"BULLISH": THEME["buy"], "BEARISH": THEME["sell"]}.get(bias, THEME["gold"]), fontsize=22, fontweight="bold")
    chg = 100 * (spot["Close"].iloc[-1] / spot["Close"].iloc[0] - 1)
    side.text(0, 0.43, "Price, 5-day change", color=THEME["muted"], fontsize=10)
    side.text(0, 0.335, f"{chg:+.2f}%", color=THEME["buy"] if chg >= 0 else THEME["sell"], fontsize=18, fontweight="bold")
    side.text(0, 0.22, "5-day range", color=THEME["muted"], fontsize=10)
    side.text(0, 0.125, f"{lo:,.0f} – {hi:,.0f}", fontsize=14, fontweight="bold")
    frame(fig, "GOLD (XAUUSD) · Where the market is positioned",
          f"1H candles · zones = where public signal channels clustered their entries · updated {datetime.now(TZ):%d %b %H:%M} {TZ_LABEL}",
          source=f"Price: COMEX gold futures adjusted to spot (basis {basis:+.1f}) · levels: consensus of public signal channels · not financial advice")
    p = save(fig, "post_1_gold_consensus.png")
    caption = (f"🟡 GOLD — market map ({datetime.now(TZ):%d %b})\n\n"
               f"Price: {last:,.1f}  ({chg:+.2f}% over 5 days)\n"
               f"Analyst bias: {bias} — {nb} buy vs {ns} sell setups in the last {LOOKBACK_H}h\n"
               + (f"Key buy zone: {sorted(buy_entries, key=lambda t: -t[1])[0][0]:,.0f}\n" if buy_entries else "")
               + (f"Key sell zone: {sorted(sell_entries, key=lambda t: -t[1])[0][0]:,.0f}\n" if sell_entries else "")
               + (f"Stops stacked near: {sorted(sls, key=lambda t: -t[1])[0][0]:,.0f}\n" if sls else "")
               + "\nWhat's your read — buy the dip or sell the rally? 👇\n\n⚠️ Not financial advice. Manage your risk.")
    return p, caption, {"buys": nb, "sells": ns, "basis": round(basis, 2), "last": round(last, 1),
                        "buy_zones": buy_entries, "sell_zones": sell_entries, "stop_clusters": sls}


def chart_majors():
    plt = _mpl()
    pairs = [("GOLD fut.", "GC=F"), ("EURUSD", "EURUSD=X"), ("GBPUSD", "GBPUSD=X"), ("USDJPY", "JPY=X"), ("BTCUSD", "BTC-USD")]
    fig, axs = plt.subplots(len(pairs), 1, figsize=(13, 8.6), sharex=False)
    fig.subplots_adjust(left=0.13, right=0.84, top=0.84, bottom=0.08, hspace=0.35)
    lines = []
    for ax, (name, tk) in zip(axs, pairs):
        try:
            df = load_price(tk, days=5, interval="1h")
        except Exception:
            ax.axis("off"); continue
        c = df["Close"]; chg = 100 * (c.iloc[-1] / c.iloc[0] - 1)
        col = THEME["buy"] if chg >= 0 else THEME["sell"]
        ax.plot(range(len(c)), c.values, color=col, lw=1.8)
        ax.fill_between(range(len(c)), c.values, c.min(), color=col, alpha=0.12)
        ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.text(-0.015, 0.5, name, transform=ax.transAxes, ha="right", va="center", fontsize=13, fontweight="bold")
        dec = 4 if c.iloc[-1] < 10 else 2 if c.iloc[-1] < 1000 else 1
        ax.text(1.02, 0.6, f"{c.iloc[-1]:,.{dec}f}", transform=ax.transAxes, fontsize=13, fontweight="bold")
        ax.text(1.02, 0.15, f"{chg:+.2f}%", transform=ax.transAxes, fontsize=12, color=col, fontweight="bold")
        lines.append(f"{'🟢' if chg >= 0 else '🔴'} {name}: {chg:+.2f}%")
    frame(fig, "Weekly pulse · majors & gold", f"Last 5 days, 1H closes · {datetime.now(TZ):%d %b %Y}",
          source="Price data: Yahoo Finance (gold = COMEX futures) · not financial advice")
    p = save(fig, "post_2_weekly_pulse.png")
    caption = "📊 5-day market pulse\n\n" + "\n".join(lines) + "\n\nWhich pair are you watching this week? 👇\n\n⚠️ Not financial advice."
    return p, caption


def cmd_charts():
    rows = json.load(open(os.path.join(HERE, "rows.json")))
    captions, ta_summary = cmd_ta(rows)                     # analyst charts first: they carry the channel
    p1, c1, meta = chart_gold_consensus(rows)
    p2, c2 = chart_majors()
    captions.update({os.path.basename(p1): c1, os.path.basename(p2): c2})
    json.dump(ta_summary, open(os.path.join(CHART_DIR, "ta_summary.json"), "w"), indent=1, default=str)
    json.dump(captions, open(os.path.join(CHART_DIR, "captions.json"), "w"), ensure_ascii=False, indent=1)
    json.dump(meta, open(os.path.join(CHART_DIR, "gold_meta.json"), "w"), default=str, indent=1)
    print("  charts ->", p1, p2)


# ================================================================= 5b. ANALYST CHARTS (technical analysis engine + pro chart)
# Instruments: (display name, yfinance ticker, pip size, decimals). Gold uses COMEX futures shifted to spot by GOLD_BASIS.
TA_MARKETS = [("XAUUSD", "GC=F", 0.1, 2), ("EURUSD", "EURUSD=X", 0.0001, 5), ("GBPUSD", "GBPUSD=X", 0.0001, 5)]
# extra instruments a forwarded signal may use (not part of the own-analysis rotation)
EXTRA_MARKETS = [("USDJPY", "USDJPY=X", 0.01, 3), ("XAGUSD", "SI=F", 0.01, 3), ("BTCUSD", "BTC-USD", 1.0, 0),
                 ("US30", "YM=F", 1.0, 0), ("NAS100", "NQ=F", 1.0, 0),
                 ("WTI", "CL=F", 0.01, 2), ("BRENT", "BZ=F", 0.01, 2), ("ETHUSD", "ETH-USD", 0.1, 1), ("US500", "ES=F", 0.1, 1),
                 ("GER40", "^GDAXI", 1.0, 0), ("UK100", "^FTSE", 1.0, 0), ("JP225", "^N225", 1.0, 0)]
TA_JOBS = [("XAUUSD", "1h", 70), ("XAUUSD", "15m", 70), ("EURUSD", "1h", 70), ("GBPUSD", "1h", 70)]
CHART_STYLE = os.getenv("CHART_STYLE", "clean")      # "clean" = simple entry chart (default) · "pro" = full structure chart


def render_entry_chart(name, tf, bars, basis=0.0, plan=None):
    if CHART_STYLE == "pro" and plan is None:
        return chart_analyst(name, tf, bars * 2, basis)
    return chart_clean(name, tf, bars, basis, plan)
TV = dict(up="#26A69A", down="#EF5350", zone_d="#26A69A", zone_s="#EF5350", level="#7D8BA6", ema="#5B8CFF")


_TF_CACHE = {}


def load_tf(ticker, tf):
    """1h / 15m from Yahoo; 4h resampled from 1h. Cached per run."""
    if (ticker, tf) in _TF_CACHE:
        return _TF_CACHE[(ticker, tf)].copy()
    _TF_CACHE[(ticker, tf)] = _load_tf(ticker, tf)
    return _TF_CACHE[(ticker, tf)].copy()


def _load_tf(ticker, tf):
    import yfinance as yf
    if tf == "4h":
        df = load_tf(ticker, "1h_long")
        return df.resample("4h", offset="0h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    interval, period = {"1h": ("1h", "30d"), "1h_long": ("1h", "120d"), "15m": ("15m", "10d"), "1m": ("1m", "5d")}[tf]
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df.index = df.index.tz_convert(TZ)
    return df[["Open", "High", "Low", "Close"]].dropna()


def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = (h - l).combine((h - c.shift()).abs(), max).combine((l - c.shift()).abs(), max)
    return tr.rolling(n).mean()


def swings(df, k=3):
    """Fractal swing points: a high/low that is the extreme of k bars each side."""
    H, L = df["High"].values, df["Low"].values
    sh, sl = [], []
    for i in range(k, len(df) - k):
        if H[i] == max(H[i - k:i + k + 1]):
            sh.append(i)
        if L[i] == min(L[i - k:i + k + 1]):
            sl.append(i)
    return sh, sl


def structure(df, sh, sl):
    """Walk swing points, emit BOS / CHoCH when a close breaks the last swing high/low."""
    C = df["Close"].values
    H, L = df["High"].values, df["Low"].values
    trend, events = 0, []
    last_h = last_l = None
    pts = sorted([(i, "H") for i in sh] + [(i, "L") for i in sl])
    pi = 0
    for i in range(len(df)):
        while pi < len(pts) and pts[pi][0] + 3 <= i:          # swing confirmed after k bars
            if pts[pi][1] == "H":
                last_h = pts[pi][0]
            else:
                last_l = pts[pi][0]
            pi += 1
        if last_h is not None and C[i] > H[last_h] and (not events or events[-1]["src"] != last_h):
            events.append({"i": i, "src": last_h, "price": H[last_h], "dir": 1, "kind": "BOS" if trend >= 0 else "CHoCH"})
            trend = 1
        elif last_l is not None and C[i] < L[last_l] and (not events or events[-1]["src"] != last_l):
            events.append({"i": i, "src": last_l, "price": L[last_l], "dir": -1, "kind": "BOS" if trend <= 0 else "CHoCH"})
            trend = -1
    return trend, events


def order_blocks(df, a):
    """Supply/demand: last opposite candle before an impulsive move (range > 1.6 ATR over 1-3 bars), still unmitigated."""
    O, H, L, C = (df[c].values for c in ("Open", "High", "Low", "Close"))
    A = a.values
    zones = []
    for i in range(15, len(df) - 3):
        if A[i] != A[i]:
            continue
        up = C[i + 1:i + 4].max() - H[i] > 1.6 * A[i]
        dn = L[i] - C[i + 1:i + 4].min() > 1.6 * A[i]
        if up and C[i] < O[i]:
            zones.append({"type": "demand", "i": i, "lo": L[i], "hi": max(O[i], C[i]) if H[i] - L[i] < 0.4 * A[i] else H[i]})
        if dn and C[i] > O[i]:
            zones.append({"type": "supply", "i": i, "lo": min(O[i], C[i]) if H[i] - L[i] < 0.4 * A[i] else L[i], "hi": H[i]})
    for z in zones:                                   # cap zone height: an OB taller than 1.2 ATR shrinks to its body
        Ai = A[z["i"]]
        if z["hi"] - z["lo"] > 1.2 * Ai:
            z["lo"], z["hi"] = min(O[z["i"]], C[z["i"]]), max(O[z["i"]], C[z["i"]])
    zones = [z for z in zones if 0 < z["hi"] - z["lo"] <= 1.2 * A[z["i"]]]
    live = []
    for z in zones:
        after = C[z["i"] + 4:]
        broken = (after < z["lo"]).any() if z["type"] == "demand" else (after > z["hi"]).any()
        if not broken:
            live.append(z)
    # merge overlapping zones of same type
    live.sort(key=lambda z: (z["type"], z["lo"]))
    merged = []
    for z in live:
        if merged and merged[-1]["type"] == z["type"] and z["lo"] <= merged[-1]["hi"]:
            m = merged[-1]; m["hi"] = max(m["hi"], z["hi"]); m["lo"] = min(m["lo"], z["lo"]); m["i"] = min(m["i"], z["i"])
        else:
            merged.append(dict(z))
    return merged


def key_levels(df, sh, sl, a_last, max_n=6):
    pts = [(df["High"].iloc[i], i) for i in sh] + [(df["Low"].iloc[i], i) for i in sl]
    pts.sort()
    groups, cur = [], []
    for p in pts:
        if cur and p[0] - cur[-1][0] > 0.5 * a_last:
            groups.append(cur); cur = []
        cur.append(p)
    if cur:
        groups.append(cur)
    lv = [{"price": float(st.mean(x[0] for x in g)), "touches": len(g), "first": min(x[1] for x in g)} for g in groups]
    lv = [l for l in lv if l["touches"] >= 2]
    last = df["Close"].iloc[-1]
    lv.sort(key=lambda l: abs(l["price"] - last))
    return sorted(lv[:max_n], key=lambda l: l["price"])


def equal_hl(df, sh, sl, a_last):
    """Liquidity pools: two recent swing highs (or lows) within 0.2 ATR."""
    out = []
    for idx, col, tag in ((sh, "High", "EQH"), (sl, "Low", "EQL")):
        idx = idx[-8:]
        for j in range(len(idx) - 1):
            for k in range(j + 1, len(idx)):
                p1, p2 = df[col].iloc[idx[j]], df[col].iloc[idx[k]]
                if abs(p1 - p2) < 0.2 * a_last and idx[k] - idx[j] >= 5:
                    out.append({"tag": tag, "i1": idx[j], "i2": idx[k], "price": float(max(p1, p2) if tag == "EQH" else min(p1, p2))})
    # keep most recent per tag
    best = {}
    for o in out:
        if o["tag"] not in best or o["i2"] > best[o["tag"]]["i2"]:
            best[o["tag"]] = o
    return list(best.values())


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def analyse(df, htf_trend=0):
    a = atr(df)
    A = float(a.iloc[-1])
    sh, sl = swings(df)
    trend, events = structure(df, sh, sl)
    zones = order_blocks(df, a)
    levels = key_levels(df, sh, sl, A)
    liq = equal_hl(df, sh, sl, A)
    last = float(df["Close"].iloc[-1])
    e50 = ema(df["Close"], 50)
    ema_dir = 1 if last > e50.iloc[-1] else -1
    # bias = this timeframe's structure when the 50 EMA agrees; confidence depends on the H4 structure
    if trend != 0 and trend == ema_dir:
        bias = "BULLISH" if trend == 1 else "BEARISH"
        confidence = "HIGH" if htf_trend == trend else "MEDIUM · counter-H4" if htf_trend == -trend else "MEDIUM"
    else:
        bias, confidence = "NEUTRAL", "LOW"
    plan = trade_plan(df, bias, zones, levels, liq, A, last)
    return dict(atr=A, sh=sh, sl=sl, trend=trend, events=events, zones=zones, levels=levels, liq=liq,
                last=last, ema50=e50, bias=bias, confidence=confidence, plan=plan, htf_trend=htf_trend)


def trade_plan(df, bias, zones, levels, liq, A, last):
    """Entry at the nearest unmitigated zone in the bias direction, stop beyond it, targets at opposing liquidity."""
    if bias == "NEUTRAL":
        return None
    side = 1 if bias == "BULLISH" else -1
    want = "demand" if side == 1 else "supply"
    cand = [z for z in zones if z["type"] == want and (z["hi"] <= last + 0.3 * A if side == 1 else z["lo"] >= last - 0.3 * A)]
    cand = [z for z in cand if abs(((z["hi"] + z["lo"]) / 2) - last) < 4 * A]
    if cand:
        z = min(cand, key=lambda z: abs((z["hi"] + z["lo"]) / 2 - last))
        entry = min(z["hi"], last) if side == 1 else max(z["lo"], last)
        sl = z["lo"] - 0.35 * A if side == 1 else z["hi"] + 0.35 * A
        basis = f"{'demand' if side == 1 else 'supply'} zone"
    else:                                   # fall back to the nearest key level
        lv = [l["price"] for l in levels if (l["price"] < last if side == 1 else l["price"] > last)]
        if not lv:
            return None
        entry = max(lv) if side == 1 else min(lv)
        sl = entry - 1.2 * A if side == 1 else entry + 1.2 * A
        z, basis = None, "key level"
    risk = abs(entry - sl)
    if risk < 0.8 * A:                       # never a stop tighter than 0.8 ATR (noise)
        sl = entry - side * 0.8 * A; risk = abs(entry - sl)
    tgt = sorted({l["price"] for l in levels} | {q["price"] for q in liq}
                 | {zz["lo"] if side == 1 else zz["hi"] for zz in zones if zz["type"] != want})
    tgt = sorted([t for t in tgt if (t - entry) * side >= 1.2 * risk], key=lambda t: (t - entry) * side)
    tp1 = tgt[0] if tgt else entry + side * 1.5 * risk
    far = [t for t in tgt if (t - entry) * side >= max(2.0 * risk, abs(tp1 - entry) + 0.8 * risk)]
    tp2 = far[0] if far and (far[0] - entry) * side <= 6 * risk else entry + side * max(2.5 * risk, abs(tp1 - entry) + risk)
    return {"side": "BUY" if side == 1 else "SELL", "entry": float(entry), "sl": float(sl), "tp1": float(tp1), "tp2": float(tp2),
            "rr1": abs(tp1 - entry) / risk, "rr2": abs(tp2 - entry) / risk, "zone": z, "basis": basis,
            "at_market": abs(entry - last) < 0.25 * A}


def sessions(idx):
    """Session blocks for shading, defined in UTC (London 07-16, New York 12-21) so they are right in any display timezone."""
    out = []
    for name, s, e in (("London", 7, 16), ("New York", 12, 21)):
        cur = None
        for i, t in enumerate(idx):
            inside = s <= t.astimezone(timezone.utc).hour < e
            if inside and cur is None:
                cur = i
            if not inside and cur is not None:
                out.append((name, cur, i - 1)); cur = None
        if cur is not None:
            out.append((name, cur, len(idx) - 1))
    return out


def fmt_p(x, dec):
    return f"{x:.{dec}f}" if LANG in ("pt", "es") else f"{x:,.{dec}f}"


def chart_analyst(name, tf, bars, basis=0.0):
    plt = _mpl()
    import matplotlib.patches as mp
    tk, pip, dec = next((t, p, d) for n, t, p, d in TA_MARKETS if n == name)
    dec_show = 2 if name == "XAUUSD" else 5
    full = load_tf(tk, tf) - basis
    htf = load_tf(tk, "4h") - basis
    htf_trend, _ = structure(htf, *swings(htf))
    res = analyse(full, htf_trend)
    off = max(0, len(full) - bars)
    df = full.iloc[off:]
    n = len(df)
    plan = res["plan"]
    A = res["atr"]
    right = max(30, int(n * 0.30))                               # empty bars on the right for the plan box / labels
    labels = []                                                  # right-margin labels, placed later without overlap

    fig = plt.figure(figsize=(13.5, 7.6))
    ax = fig.add_axes([0.035, 0.09, 0.765, 0.76])
    ax.set_facecolor(THEME["panel"])
    lo, hi = float(df["Low"].min()), float(df["High"].max())
    if plan:
        lo, hi = min(lo, plan["sl"], plan["tp2"]), max(hi, plan["sl"], plan["tp2"])
    pad = (hi - lo) * 0.06
    ax.set_ylim(lo - pad, hi + pad); ax.set_xlim(-1, n + right)

    # sessions
    for sname, s, e in sessions(df.index):                      # shade New York session only (keeps the chart calm)
        if sname == "New York":
            ax.axvspan(s - 0.5, e + 0.5, color=THEME["accent"], alpha=0.05, lw=0)
    # EMA 50
    ax.plot(range(n), res["ema50"].iloc[off:].values, color=TV["ema"], lw=1.3, alpha=0.8)
    ax.text(n - 1, res["ema50"].iloc[-1], "  EMA 50", color=TV["ema"], fontsize=8.5, va="center", alpha=0.9)
    # key levels
    for l in res["levels"]:
        if lo - pad < l["price"] < hi + pad:
            ax.hlines(l["price"], -1, n + right, color=TV["level"], lw=0.8, ls=(0, (6, 4)), alpha=0.7)
    # zones (only those visible / near price)
    shown_z = [z for z in res["zones"] if z["hi"] > lo - pad and z["lo"] < hi + pad and abs((z["hi"] + z["lo"]) / 2 - res["last"]) < 6 * A]
    shown_z = sorted(shown_z, key=lambda z: abs((z["hi"] + z["lo"]) / 2 - res["last"]))[:4]
    for z in shown_z:
        x0 = max(-1, z["i"] - off)
        col = TV["zone_d"] if z["type"] == "demand" else TV["zone_s"]
        ax.add_patch(mp.Rectangle((x0, z["lo"]), n + right - x0, z["hi"] - z["lo"], color=col, alpha=0.13, lw=0))
        ax.hlines([z["lo"], z["hi"]], x0, n + right, color=col, lw=0.6, alpha=0.55)
        ax.text(x0 + 1, (z["hi"] + z["lo"]) / 2, f"{'DEMAND' if z['type'] == 'demand' else 'SUPPLY'} · {tf.upper()} OB",
                color=col, fontsize=8.5, va="center", fontweight="bold", alpha=0.95)
    # candles
    w = 0.62
    for i, (o, h, l, c) in enumerate(df[["Open", "High", "Low", "Close"]].itertuples(index=False)):
        col = TV["up"] if c >= o else TV["down"]
        ax.vlines(i, l, h, color=col, lw=1)
        ax.add_patch(mp.Rectangle((i - w / 2, min(o, c)), w, max(abs(c - o), (hi - lo) * 0.0015), color=col, lw=0))
    # structure breaks
    for ev in res["events"][-4:]:
        s, e = ev["src"] - off, ev["i"] - off
        if e < 0:
            continue
        s = max(s, 0)
        col = TV["up"] if ev["dir"] == 1 else TV["down"]
        ax.hlines(ev["price"], s, e, color=col, lw=1, ls=(0, (3, 2)))
        ax.text((s + e) / 2, ev["price"], ev["kind"], color=col, fontsize=8.5, fontweight="bold", ha="center",
                va="bottom" if ev["dir"] == 1 else "top")
    # liquidity pools
    for q in res["liq"]:
        i1, i2 = q["i1"] - off, q["i2"] - off
        if i2 < 0:
            continue
        ax.hlines(q["price"], max(i1, 0), n, color=THEME["gold"], lw=1, ls=":")
        ax.text(max(i1, 0), q["price"], f"{q['tag']} ", color=THEME["gold"], fontsize=8, va="bottom" if q["tag"] == "EQH" else "top", fontweight="bold")
    # trade plan box
    if plan:
        x0, x1 = n + 1, n + int(right * 0.36)
        up_c, dn_c = (TV["up"], TV["down"]) if plan["side"] == "BUY" else (TV["up"], TV["down"])
        ax.add_patch(mp.Rectangle((x0, min(plan["entry"], plan["tp2"])), x1 - x0, abs(plan["tp2"] - plan["entry"]), color=up_c, alpha=0.22, lw=0))
        ax.add_patch(mp.Rectangle((x0, min(plan["entry"], plan["sl"])), x1 - x0, abs(plan["sl"] - plan["entry"]), color=dn_c, alpha=0.24, lw=0))
        for key, col, lab in (("tp2", up_c, "TP2"), ("tp1", up_c, "TP1"), ("entry", THEME["text"], "ENTRY"), ("sl", dn_c, "SL")):
            ax.hlines(plan[key], x0, x1, color=col, lw=1.4 if key == "entry" else 1)
            pips = abs(plan[key] - plan["entry"]) / pip
            extra = "" if key == "entry" else f"  {pips:,.0f} pips"
            labels.append((plan[key], f"{lab} {fmt_p(plan[key], dec_show)}{extra}", col, x1))
        ax.annotate("", xy=(x0 + 1.5, plan["tp1"]), xytext=(x0 + 1.5, plan["entry"]),
                    arrowprops=dict(arrowstyle="-|>", color=THEME["text"], lw=1.2, alpha=0.8))
    if labels:
        gap = (hi - lo + 2 * pad) * 0.04
        order = sorted(labels, key=lambda t: t[0])
        ys = [t[0] for t in order]
        for _ in range(50):                                     # relax overlapping labels apart
            moved = False
            for k in range(1, len(ys)):
                if ys[k] - ys[k - 1] < gap:
                    mid = (ys[k] + ys[k - 1]) / 2
                    ys[k - 1], ys[k] = mid - gap / 2, mid + gap / 2
                    moved = True
            if not moved:
                break
        for (yv, text, col, xa), yl in zip(order, ys):
            ax.annotate(text, xy=(xa, yv), xytext=(xa + 1.2, yl), va="center", fontsize=9, color=col, fontweight="bold",
                        arrowprops=dict(arrowstyle="-", color=col, lw=0.5, alpha=0.6) if abs(yl - yv) > gap * 0.2 else None)
    # last price tag on the axis
    ax.axhline(res["last"], color=THEME["gold"], lw=0.8, ls="--", alpha=0.8)
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", labelsize=9, pad=2)
    ax.text(1.0, res["last"], f" {fmt_p(res['last'], dec_show)} ", transform=ax.get_yaxis_transform(), ha="left", va="center",
            fontsize=9, fontweight="bold", color=THEME["bg"], bbox=dict(boxstyle="square,pad=0.25", fc=THEME["gold"], ec="none"), clip_on=False)
    ticks = list(range(0, n, max(1, n // 7)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([df.index[i].strftime("%d %b %H:%M" if tf != "15m" else "%a %H:%M") for i in ticks], fontsize=9)
    ax.grid(axis="x", visible=False)
    for s in ("left", "top"):
        ax.spines[s].set_visible(False)
    ax.spines["right"].set_visible(True)
    # watermark
    ax.text(0.5, 0.5, f"{name} · {tf.upper()}", transform=ax.transAxes, ha="center", va="center", fontsize=54,
            color=THEME["text"], alpha=0.035, fontweight="bold")

    # header + plan card
    bias_col = {"BULLISH": TV["up"], "BEARISH": TV["down"]}.get(res["bias"], THEME["gold"])
    fig.text(0.035, 0.955, f"{name}", fontsize=24, fontweight="bold", va="top")
    fig.text(0.035 + 0.012 * len(name) + 0.06, 0.948, f"{tf.upper()}  ·  {datetime.now(TZ):%d %b %Y, %H:%M} {TZ_LABEL}", fontsize=11.5,
             color=THEME["muted"], va="top")
    fig.text(0.035, 0.895, "Market structure · supply & demand · liquidity", fontsize=10.5, color=THEME["muted"], va="top")
    card = fig.add_axes([0.862, 0.09, 0.128, 0.76]); card.axis("off")
    y = 1.0
    def row(label, value, col=THEME["text"], size=13, gap=0.085):
        nonlocal y
        card.text(0, y, label, fontsize=8.5, color=THEME["muted"], va="top", transform=card.transAxes)
        card.text(0, y - 0.03, value, fontsize=size, color=col, va="top", fontweight="bold", transform=card.transAxes)
        y -= gap
    row("BIAS", res["bias"], bias_col, 15)
    row("CONFIDENCE", res["confidence"].replace(" · ", "\n"), THEME["text"], 10.5, 0.11)
    htf_txt = {1: "Bullish", -1: "Bearish", 0: "Ranging"}[htf_trend]
    row("H4 STRUCTURE", htf_txt, {1: TV["up"], -1: TV["down"]}.get(htf_trend, THEME["gold"]), 12)
    last_ev = res["events"][-1] if res["events"] else None
    row(f"{tf.upper()} LAST BREAK", f"{last_ev['kind']} {'▲' if last_ev['dir'] == 1 else '▼'}" if last_ev else "—",
        (TV["up"] if last_ev and last_ev["dir"] == 1 else TV["down"]) if last_ev else THEME["text"], 12)
    y -= 0.02
    if plan:
        card.add_patch(mp.FancyBboxPatch((-0.04, y - 0.47), 1.08, 0.5, boxstyle="round,pad=0.01,rounding_size=0.02",
                                         transform=card.transAxes, fc=THEME["panel"], ec=THEME["grid"]))
        y -= 0.01
        row("SETUP", f"{plan['side']} {'NOW' if plan['at_market'] else 'LIMIT'}", TV["up"] if plan["side"] == "BUY" else TV["down"], 14)
        row("ENTRY", fmt_p(plan["entry"], dec_show), size=12, gap=0.075)
        row("STOP LOSS", fmt_p(plan["sl"], dec_show), TV["down"], 12, 0.075)
        row("TP1  ·  TP2", f"{fmt_p(plan['tp1'], dec_show if name != 'XAUUSD' else 1)} · {fmt_p(plan['tp2'], dec_show if name != 'XAUUSD' else 1)}", TV["up"], 10.5, 0.075)
        row("RISK : REWARD", f"1 : {plan['rr1']:.1f}  ·  1 : {plan['rr2']:.1f}", THEME["gold"], 12, 0.08)
    else:
        row("SETUP", "WAIT", THEME["gold"], 14)
        card.text(0, y - 0.01, "No clean edge.\nWait for a break of\nstructure.", fontsize=9.5, color=THEME["muted"], va="top", transform=card.transAxes)
    fig.text(0.035, 0.03, "Educational analysis, not financial advice. Manage your risk.", fontsize=9, color=THEME["muted"])
    fig.text(0.985, 0.03, f"{BRAND}  ·  {HANDLE}", fontsize=10.5, color=THEME["gold"], ha="right", fontweight="bold")
    fig.patches.append(mp.Rectangle((0, 0.988), 1, 0.012, transform=fig.transFigure, color=THEME["gold"]))
    p = save(fig, f"ta_{name}_{tf}.png")
    return p, analyst_caption(name, tf, res, dec_show, pip), res


def analyst_caption(name, tf, res, dec, pip):
    plan, last_ev = res["plan"], (res["events"][-1] if res["events"] else None)
    flag = {"XAUUSD": "🟡", "EURUSD": "🇪🇺", "GBPUSD": "🇬🇧"}.get(name, "📈")
    htf = {1: "bullish", -1: "bearish", 0: "ranging"}[res["htf_trend"]]
    lines = [f"{flag} {name} · {tf.upper()} analysis", ""]
    why = [f"H4 structure is {htf}"]
    if last_ev:
        why.append(f"last {tf.upper()} break was a {'bullish' if last_ev['dir'] == 1 else 'bearish'} {last_ev['kind']} at {fmt_p(last_ev['price'], dec)}")
    why.append(f"price is {'above' if res['last'] > res['ema50'].iloc[-1] else 'below'} the 50 EMA")
    lines.append("Why: " + ", ".join(why) + ".")
    for q in res["liq"]:
        where = "above" if q["price"] > res["last"] else "below"
        lines.append(f"Liquidity {where} price at {fmt_p(q['price'], dec)} ({'equal highs' if q['tag'] == 'EQH' else 'equal lows'}).")
    lines.append("")
    if plan:
        risk_p = abs(plan["entry"] - plan["sl"]) / pip
        lines += [f"{'🟢' if plan['side'] == 'BUY' else '🔴'} {plan['side']} {'NOW' if plan['at_market'] else 'LIMIT'} from the {plan['basis']}",
                  f"Entry: {fmt_p(plan['entry'], dec)}",
                  f"SL: {fmt_p(plan['sl'], dec)}  ({risk_p:,.0f} pips)",
                  f"TP1: {fmt_p(plan['tp1'], dec)}  (1:{plan['rr1']:.1f})",
                  f"TP2: {fmt_p(plan['tp2'], dec)}  (1:{plan['rr2']:.1f})",
                  "", f"Invalidation: a {tf.upper()} close {'below' if plan['side'] == 'BUY' else 'above'} {fmt_p(plan['sl'], dec)}."]
    else:
        lines.append("⏳ No trade yet — waiting for a clean break of structure.")
    lines += ["", "Move SL to breakeven at TP1. Risk 1% max.", "⚠️ Educational, not financial advice."]
    return "\n".join(lines)


BASIS_SRC = ""                                       # where the last gold_basis() value came from (for the run log)
DEFAULT_GOLD_BASIS = 21.0                            # last resort only: GC=F (Dec 2026 contract) minus spot, October 2026


def spot_now():
    """(unix time, price) of spot gold now from gold-api.com, or None. Saved by each run for sample_basis()."""
    try:
        j = requests.get("https://api.gold-api.com/price/XAU", headers=UA, timeout=(5, 10)).json()
        t, p = datetime.fromisoformat(j["updatedAt"].replace("Z", "+00:00")).timestamp(), float(j["price"])
        return (t, p) if 1000 < p < 20000 and abs(time.time() - t) < 600 else None
    except Exception as e:
        print(f"  ! spot gold price not read: {e}")
        return None


def sample_basis(samples, fut=None):
    """Gold futures minus spot from the spot prices saved by earlier runs, each set against the GC=F 1-minute bar of
    the same moment (Yahoo's futures arrive about 10 minutes late, so only earlier samples can be matched). Up to the 6
    newest matches; those more than $3 from the newest three (an older level, or a contract roll, which moves it by $10
    or more) are left out. None with fewer than 3 matches. ($3, not $1: near expiry the gap sits around zero and two
    1-minute readings of it differ by a dollar or two, which left the offset unmeasured for days.)"""
    fut = load_tf("GC=F", "1m") if fut is None else fut
    starts = [t.tz_convert("UTC").to_pydatetime() for t in fut.index]
    diffs = []
    for t, p in sorted(samples or [], reverse=True):
        when = datetime.fromtimestamp(t, timezone.utc)
        i = bisect.bisect_right(starts, when) - 1
        if i < 0 or i >= len(starts) - 1 or when - starts[i] >= timedelta(minutes=1):
            continue                                  # no futures bar at that minute: market closed or not published yet
        o, c = float(fut["Open"].iloc[i]), float(fut["Close"].iloc[i])
        diffs.append(o + (c - o) * (when - starts[i]).total_seconds() / 60 - p)
        if len(diffs) >= 6:
            break
    if len(diffs) < 3:
        return None
    anchor = st.median(diffs[:3])                     # the newest samples lead: the gap moves by dollars in a day
    keep = [x for x in diffs if abs(x - anchor) <= 3.0]
    if len(keep) < 3:
        print(f"  · gold spot samples disagree (newest first): {', '.join(f'{x:.2f}' for x in diffs)}")
        return None
    return round(st.median(keep), 2)


def gold_basis(rows=None, last=None, samples=None):
    """Futures→spot offset for gold. GOLD_BASIS env wins; else measured from spot samples saved by earlier runs (`samples`,
    default: memory "_gold_spot"); else `last` (default: the value measured in the last 4 days, kept in memory as
    "_gold_basis" by daily.run); else estimated from recent channel signals; else the default."""
    global BASIS_SRC
    mem = _state() if last is None or samples is None else {}
    old = None                                        # measured more than 4 days ago: still closer than the default
    if last is None:
        gb = mem.get("_gold_basis") or {}
        last = gb.get("b") if time.time() - gb.get("t", 0) < 4 * 86400 else None
        old = gb.get("b")
    if samples is None:
        samples = mem.get("_gold_spot") or []
    if os.getenv("GOLD_BASIS"):
        BASIS_SRC = "env"
        return float(os.getenv("GOLD_BASIS"))
    try:
        b = sample_basis(samples)
        if b is not None and -10 < b < 80:
            BASIS_SRC = "spot samples"
            return b
    except Exception as e:
        print(f"  ! gold offset from spot samples not measured: {e}")
    if last is not None:
        BASIS_SRC = "last measured"
        return float(last)
    try:
        b = futures_basis(load_price("GC=F", 5, "1h"), gold_signals(rows or [], 24 * 5))
        if 5 < b < 60:
            BASIS_SRC = "signals"
            return b
    except Exception:
        pass
    if old is not None:
        BASIS_SRC = "last measured, over 4 days ago"
        return float(old)
    BASIS_SRC = "default"
    return DEFAULT_GOLD_BASIS


def cmd_ta(rows=None):
    basis = gold_basis(rows)
    caps, summary = {}, {}
    for name, tf, bars in TA_JOBS:
        p, cap, res = render_entry_chart(name, tf, bars, basis if name == "XAUUSD" else 0.0)
        if res["plan"] or os.getenv("POST_WAIT_CHARTS"):        # a "no setup" chart is not posted by default
            caps[os.path.basename(p)] = cap
        summary[f"{name}_{tf}"] = {"bias": res["bias"], "plan": res["plan"] and {k: v for k, v in res["plan"].items() if k != "zone"},
                                   "last": res["last"], "atr": res["atr"]}
        print(f"  {name} {tf}: {res['bias']}  plan={summary[f'{name}_{tf}']['plan']}")
    summary["gold_basis_used"] = basis
    return caps, summary


# ================================================================= 5d. CLEAN ENTRY CHART (default posting style)
# One idea per chart: candles + the entry zone + the trade box + four numbers. Square, phone-first, big type.
CLEAN = dict(bg="#0E1525", panel="#0E1525", grid="#1A2438", text="#F2F5FA", muted="#8C98AE",
             buy="#22C38E", sell="#F0506E", gold="#F2B33D")
NAMES = {"XAUUSD": "GOLD", "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY", "XAGUSD": "PRATA",
         "BTCUSD": "BITCOIN", "US30": "DOW JONES", "NAS100": "NASDAQ",
         "WTI": "WTI", "BRENT": "BRENT", "ETHUSD": "ETHEREUM", "US500": "S&P 500", "GER40": "DAX", "UK100": "FTSE 100", "JP225": "NIKKEI"}
LANG = os.getenv("LANG_CODE", "en")                  # set per channel via channels.json "lang": "en" | "pt"
PT_MONTHS = ["", "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
ES_MONTHS = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
TXT = {
    "en": {"GOLD": "GOLD", "BUY": "BUY", "SELL": "SELL", "NOW": "NOW", "LIMIT": "LIMIT", "WAIT": "WAIT", "chart": "{tf} chart",
           "zone": "{side} ZONE", "ENTRY": "ENTRY", "STOP LOSS": "STOP LOSS", "last_n": "Last {n} candles",
           "limit order": "limit order", "at market": "at market", "pips_risk": "{p} pips risk",
           "up": "up", "down": "down", "reason": "Price is trending {trend} on the {tf}. Entry on a {where}.",
           "where_buy": "dip into the zone below", "where_sell": "pullback into the zone above", "where_now": "the current price",
           "no_setup": "No clean setup right now. Waiting for price to pick a direction.",
           "footer": "Move SL to entry when TP1 hits · risk max 1% · not financial advice",
           "c_entry": "Entry", "c_tp1": "When TP1 hits, move SL to entry.",
           "c_nfa": "⚠️ Not financial advice. Risk max 1% per trade.", "c_wait": "No clean setup right now. Waiting for direction.",
           "c_nfa_short": "⚠️ Not financial advice."},
    "pt": {"GOLD": "OURO", "BUY": "COMPRA", "SELL": "VENDA", "NOW": "AGORA", "LIMIT": "LIMITE", "WAIT": "AGUARDAR", "chart": "Gráfico {tf}",
           "zone": "ZONA DE {side}", "ENTRY": "ENTRADA", "STOP LOSS": "STOP LOSS", "last_n": "Últimos {n} candles",
           "limit order": "ordem limitada", "at market": "a mercado", "pips_risk": "{p} pips de risco",
           "up": "alta", "down": "baixa", "reason": "Preço em tendência de {trend} no {tf}. Entrada {where}.",
           "where_buy": "num recuo até a zona abaixo", "where_sell": "num repique até a zona acima", "where_now": "no preço atual",
           "no_setup": "Sem setup claro agora. Aguardando o preço definir a direção.",
           "footer": "Mova o SL para a entrada quando o TP1 bater · risco máx. 1%\nNão é recomendação de investimento",
           "c_entry": "Entrada", "c_tp1": "Quando o TP1 bater, mova o SL para a entrada.",
           "c_nfa": "⚠️ Não é recomendação de investimento. Arrisque no máximo 1% por operação.",
           "c_wait": "Sem setup claro agora. Aguardando direção.", "c_nfa_short": "⚠️ Não é recomendação de investimento."},
    "es": {"GOLD": "ORO", "BUY": "COMPRA", "SELL": "VENTA", "NOW": "AHORA", "LIMIT": "LÍMITE", "WAIT": "ESPERAR", "chart": "Gráfico {tf}",
           "zone": "ZONA DE {side}", "ENTRY": "ENTRADA", "STOP LOSS": "STOP LOSS", "last_n": "Últimas {n} velas",
           "limit order": "orden límite", "at market": "a mercado", "pips_risk": "{p} pips de riesgo",
           "up": "alza", "down": "baja", "reason": "Precio en tendencia de {trend} en {tf}. Entrada {where}.",
           "where_buy": "en un retroceso a la zona de abajo", "where_sell": "en un rebote a la zona de arriba", "where_now": "al precio actual",
           "no_setup": "Sin setup claro ahora. Esperando que el precio defina la dirección.",
           "footer": "Mueve el SL a la entrada cuando toque el TP1 · riesgo máx. 1%\nNo es recomendación de inversión",
           "c_entry": "Entrada", "c_tp1": "Cuando toque el TP1, mueve el SL a la entrada.",
           "c_nfa": "⚠️ No es recomendación de inversión. Arriesga como máximo 1% por operación.",
           "c_wait": "Sin setup claro ahora. Esperando dirección.", "c_nfa_short": "⚠️ No es recomendación de inversión."},
}


def T(key, **kw):
    t = TXT.get(LANG, TXT["en"]).get(key, TXT["en"].get(key, key))
    return t.format(**kw) if kw else t


def pname(name):
    return T("GOLD") if name == "XAUUSD" else NAMES.get(name, name)


def stamp():
    d = datetime.now(TZ)
    if LANG in ("pt", "es"):
        return f"{d.day:02d} {(PT_MONTHS if LANG == 'pt' else ES_MONTHS)[d.month]}, {d:%H:%M}"
    return f"{d:%d %b, %H:%M}"


def side_word(side):
    return T(side)


def _pill(ax, x, y, text, fc, tc, size=12, ha="left", transform=None):
    ax.text(x, y, text, ha=ha, va="center", fontsize=size, fontweight="bold", color=tc, zorder=6,
            transform=transform or ax.transData, clip_on=False,
            bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.25", fc=fc, ec="none"))


def plain_reason(res, tf):
    plan = res["plan"]
    if plan.get("reason"):
        return plan["reason"]
    up = plan["side"] == "BUY"
    where = T("where_now") if plan["at_market"] else T("where_buy") if up else T("where_sell")
    return T("reason", trend=T("up") if res["trend"] == 1 else T("down"), tf=tf.upper(), where=where)


def with_rr(plan, last, A):
    """Complete an outside plan (side, entry, sl, tp1, tp2) with the fields the chart needs."""
    risk = abs(plan["entry"] - plan["sl"]) or 1e-9
    return dict(plan, rr1=abs(plan["tp1"] - plan["entry"]) / risk, rr2=abs(plan["tp2"] - plan["entry"]) / risk,
                zone=None, at_market=plan.get("at_market", abs(plan["entry"] - last) < 0.25 * A))


def chart_clean(name, tf, bars, basis=0.0, plan=None):
    """plan: optional outside trade (e.g. a forwarded signal) drawn instead of the engine's own setup."""
    plt = _mpl()
    import matplotlib.patches as mp
    tk, pip, d = next((t, p, d) for n, t, p, d in TA_MARKETS + EXTRA_MARKETS if n == name)
    dec = 1 if name == "XAUUSD" else d
    full = load_tf(tk, tf) - basis
    htf = load_tf(tk, "4h") - basis
    htf_trend, _ = structure(htf, *swings(htf))
    res = analyse(full, htf_trend)
    if plan:
        res["plan"] = with_rr(plan, res["last"], res["atr"])
    plan = res["plan"]
    df = full.iloc[-bars:]
    n = len(df)
    up = bool(plan) and plan["side"] == "BUY"
    side_col = CLEAN["buy"] if up else CLEAN["sell"] if plan else CLEAN["gold"]

    fig = plt.figure(figsize=(8, 8), facecolor=CLEAN["bg"])
    # ---- header
    fig.text(0.06, 0.945, pname(name), fontsize=30, fontweight="bold", color=CLEAN["text"], va="top")
    fig.text(0.06, 0.875, f"{T('chart', tf=tf.upper())}  ·  {stamp()} {TZ_LABEL}", fontsize=12, color=CLEAN["muted"], va="top")
    head = f"{side_word(plan['side'])} {T('NOW') if plan['at_market'] else T('LIMIT')}" if plan else T("WAIT")
    fig.text(0.94, 0.935, f"  {head}  ", fontsize=22 if len(head) < 12 else 19, fontweight="bold", color=CLEAN["bg"], ha="right", va="top",
             bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.4", fc=side_col, ec="none"))
    if plan:
        fig.text(0.06, 0.835, plain_reason(res, tf), fontsize=12.5, color=CLEAN["text"], va="top")

    # ---- chart
    ax = fig.add_axes([0.11, 0.30, 0.79, 0.50]); ax.set_facecolor(CLEAN["panel"])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.grid(axis="y", color=CLEAN["grid"], lw=0.8); ax.grid(axis="x", visible=False)
    right = int(n * 0.6)
    lo, hi = float(df["Low"].min()), float(df["High"].max())
    if plan:
        lo, hi = min(lo, plan["sl"]), max(hi, plan["tp2"]) if up else max(hi, plan["sl"])
        lo = min(lo, plan["tp2"]) if not up else lo
    pad = (hi - lo) * 0.07
    ax.set_ylim(lo - pad, hi + pad); ax.set_xlim(-1, n + right)
    w = 0.7
    for i, (o, h, l, c) in enumerate(df[["Open", "High", "Low", "Close"]].itertuples(index=False)):
        col = CLEAN["buy"] if c >= o else CLEAN["sell"]
        ax.vlines(i, l, h, color=col, lw=1.1, alpha=0.9)
        ax.add_patch(mp.Rectangle((i - w / 2, min(o, c)), w, max(abs(c - o), (hi - lo) * 0.002), color=col, lw=0))
    last = res["last"]
    ax.hlines(last, -1, n + right, color=CLEAN["gold"], lw=1, ls=(0, (4, 3)), alpha=0.9)

    if plan:
        x0, x1 = n + 1, n + int(right * 0.5)
        e, sl, t1, t2 = plan["entry"], plan["sl"], plan["tp1"], plan["tp2"]
        # entry zone band across the chart (the "where")
        z = plan.get("zone")
        zlo, zhi = (z["lo"], z["hi"]) if z else (e - res["atr"] * 0.25, e + res["atr"] * 0.25)
        ax.add_patch(mp.Rectangle((-1, zlo), n + right, zhi - zlo, color=side_col, alpha=0.16, lw=0))
        _pill(ax, 0.5, (zlo + zhi) / 2, T("zone", side=side_word("BUY" if up else "SELL")), CLEAN["bg"], side_col, 10.5)
        # trade box
        ax.add_patch(mp.Rectangle((x0, min(e, t2)), x1 - x0, abs(t2 - e), color=CLEAN["buy"], alpha=0.22, lw=0))
        ax.add_patch(mp.Rectangle((x0, min(e, sl)), x1 - x0, abs(sl - e), color=CLEAN["sell"], alpha=0.28, lw=0))
        for y, col, lw in ((t2, CLEAN["buy"], 1.6), (t1, CLEAN["buy"], 1.6), (e, CLEAN["text"], 2.2), (sl, CLEAN["sell"], 1.6)):
            ax.hlines(y, x0, x1, color=col, lw=lw)
        ax_x = x0 + 0.8 * (x1 - x0)
        ax.annotate("", xy=(ax_x, t1), xytext=(ax_x, e),
                    arrowprops=dict(arrowstyle="-|>,head_width=0.5,head_length=0.8", color=CLEAN["text"], lw=2))
        # right-side pills, spaced so they never overlap
        items = [(t2, f"TP2  {fmt_p(t2, dec)}", CLEAN["buy"], CLEAN["bg"]), (t1, f"TP1  {fmt_p(t1, dec)}", CLEAN["buy"], CLEAN["bg"]),
                 (e, f"{T('ENTRY')}  {fmt_p(e, dec)}", CLEAN["text"], CLEAN["bg"]), (sl, f"SL  {fmt_p(sl, dec)}", CLEAN["sell"], CLEAN["bg"])]
        items.sort(key=lambda t: t[0])
        gap = (hi - lo + 2 * pad) * 0.075
        ys = [t[0] for t in items]
        for _ in range(60):
            moved = False
            for k in range(1, len(ys)):
                if ys[k] - ys[k - 1] < gap:
                    m = (ys[k] + ys[k - 1]) / 2; ys[k - 1], ys[k] = m - gap / 2, m + gap / 2; moved = True
            if not moved:
                break
        for (y, text, fc, tc), yl in zip(items, ys):
            _pill(ax, x1 + 1.2, yl, text, fc, tc, 12)
    # current price pill on the left of the box
    # current price lives on the left axis like a terminal price tag; ticks too close to it are dropped
    ax.yaxis.tick_left(); ax.tick_params(axis="y", labelsize=10, colors=CLEAN["muted"], length=0)
    loc = __import__("matplotlib").ticker.MaxNLocator(5)
    ticks = [t for t in loc.tick_values(lo - pad, hi + pad) if lo - pad < t < hi + pad and abs(t - last) > (hi - lo) * 0.07]
    ax.set_yticks(ticks)
    ax.set_yticklabels([fmt_p(t, 0 if dec <= 1 else dec - 1) for t in ticks])
    _pill(ax, -0.012, last, f"{fmt_p(last, dec)}", CLEAN["gold"], CLEAN["bg"], 10, ha="right", transform=ax.get_yaxis_transform())
    ax.text(n + 0.8, last, T("NOW"), color=CLEAN["gold"], fontsize=9, fontweight="bold", va="bottom")
    ax.set_xticks([])
    fig.text(0.11, 0.285, T("last_n", n=n), fontsize=9.5, color=CLEAN["muted"], va="top")

    # ---- the four numbers
    if plan:
        risk = abs(plan["entry"] - plan["sl"])
        tiles = [(T("ENTRY"), fmt_p(plan["entry"], dec), CLEAN["text"], T("limit order") if not plan["at_market"] else T("at market")),
                 (T("STOP LOSS"), fmt_p(plan["sl"], dec), CLEAN["sell"], T("pips_risk", p=f"{risk / pip:,.0f}".replace(",", "."))),
                 ("TP1", fmt_p(plan["tp1"], dec), CLEAN["buy"], f"1 : {plan['rr1']:.1f}"),
                 ("TP2", fmt_p(plan["tp2"], dec), CLEAN["buy"], f"1 : {plan['rr2']:.1f}")]
        tw, gx = 0.205, 0.02
        for k, (lab, val, col, sub) in enumerate(tiles):
            x = 0.06 + k * (tw + gx)
            fig.patches.append(mp.FancyBboxPatch((x, 0.105), tw, 0.145, transform=fig.transFigure,
                                                 boxstyle="round,pad=0,rounding_size=0.012", fc="#16203A", ec="none"))
            fig.text(x + 0.016, 0.228, lab, fontsize=10.5, color=CLEAN["muted"], fontweight="bold", va="top")
            fig.text(x + 0.016, 0.19, val, fontsize=17 if dec == 2 else 15.5, color=col, fontweight="bold", va="top")
            fig.text(x + 0.016, 0.137, sub, fontsize=10, color=CLEAN["muted"], va="top")
    else:
        fig.text(0.06, 0.2, T("no_setup"), fontsize=13, color=CLEAN["text"])
    fig.text(0.06, 0.07, T("footer"), fontsize=9.5, va="top", linespacing=1.5, color=CLEAN["muted"])
    if BRAND:                                           # empty brand/handle = an unbranded card (relay channels)
        fig.text(0.94, 0.055, BRAND, fontsize=12, color=CLEAN["gold"], fontweight="bold", ha="right")
    if HANDLE:
        fig.text(0.94, 0.03, HANDLE, fontsize=9.5, color=CLEAN["muted"], ha="right")
    p = save_clean(fig, f"entry_{name}_{tf}{'' if LANG == 'en' else '_' + LANG}.png")
    return p, clean_caption(name, tf, res, dec, pip), res


def save_clean(fig, name):
    os.makedirs(CHART_DIR, exist_ok=True)
    p = os.path.join(CHART_DIR, name)
    fig.savefig(p, dpi=150, facecolor=CLEAN["bg"])          # 1200 x 1200 px
    __import__("matplotlib.pyplot").pyplot.close(fig)
    return p


def clean_caption(name, tf, res, dec, pip):
    plan = res["plan"]
    title = pname(name)
    if not plan:
        return f"⏳ {title} · {tf.upper()}\n\n{T('c_wait')}\n\n{T('c_nfa_short')}"
    icon = "🟢" if plan["side"] == "BUY" else "🔴"
    risk = f"{abs(plan['entry'] - plan['sl']) / pip:,.0f}".replace(",", ".")
    return "\n".join([
        f"{icon} {title} {side_word(plan['side'])} {T('NOW') if plan['at_market'] else T('LIMIT')} · {tf.upper()}",
        "",
        f"📍 {T('c_entry')}: {fmt_p(plan['entry'], dec)}",
        f"🛑 SL: {fmt_p(plan['sl'], dec)}  ({risk} pips)",
        f"🎯 TP1: {fmt_p(plan['tp1'], dec)}  (1:{plan['rr1']:.1f})",
        f"🏁 TP2: {fmt_p(plan['tp2'], dec)}  (1:{plan['rr2']:.1f})",
        "",
        plain_reason(res, tf),
        T("c_tp1"),
        "",
        T("c_nfa"),
    ])


# ================================================================= 5c. POSTING SCHEDULE (Brazil time, from the engagement analysis)
# Hours = top hit-rate hours of competitor posts converted to BRT (UTC-3). n = posts behind each number.
SCHEDULE = {
    # Brazil audience peaks (finance content: 07-09h and 12-13h; prime time 19-21h) x market activity.
    7:  {"why": "Brazil morning peak · day plan before NY", "jobs": [("XAUUSD", "1h")]},
    9:  {"why": "US data / NY open · most volatile hour for gold", "jobs": [("XAUUSD", "15m")]},
    12: {"why": "Brazil lunch peak · NY session live", "jobs": [("EURUSD", "1h"), ("GBPUSD", "1h"), ("XAUUSD", "15m")]},
    19: {"why": "Brazil prime time · plan for Asia/overnight", "jobs": [("XAUUSD", "1h")]},
    # original competitor-data slots (non-Brazilian audiences), kept for English channels:
    4:  {"why": "London open · 33% hit rate (n=43)", "jobs": [("XAUUSD", "1h")]},
    8:  {"why": "London/NY build-up · 30% (n=67)", "jobs": [("XAUUSD", "15m")]},
    11: {"why": "New York session · 34% (n=70)", "jobs": [("GBPUSD", "1h"), ("EURUSD", "1h"), ("XAUUSD", "15m")]},
    15: {"why": "late NY · 35% (n=34)", "jobs": ["consensus"]},
    22: {"why": "Asia open · 64% (n=14, test slot)", "jobs": [("XAUUSD", "1h")]},
}
WEEKLY_PULSE = ("Wed", 11)          # Wed/Thu are the strongest days


# ---------------------------------------------------------------- destination channels
# channels.json (next to this file) lists every channel the charts are posted into. Each entry:
#   chat        "@handle" or numeric "-100..." id (required)
#   brand       name printed on the charts for that channel
#   markets     which instruments this channel gets (default: all)
#   slots       which BRT hours it posts at (default: every SCHEDULE hour)
#   footer      extra caption line, e.g. a VIP link
#   quiet_hours BRT hours that post silently (no notification sound)
#   token_env   name of the env var holding that channel's bot token (default TG_BOT_TOKEN)
#   enabled     false = skip
CHANNELS_FILE = os.path.join(HERE, "channels.json")
DEFAULT_CHANNEL = {"chat": os.getenv("TG_CHANNEL", "@yourchannel"), "brand": BRAND, "lang": "en", "markets": None, "slots": None,
                   "footer": "", "quiet_hours": [22, 23, 0, 1, 2, 3, 4, 5, 6], "token_env": "TG_BOT_TOKEN", "enabled": True}


def _from_env(v):
    """channels.json values written as "$NAME" come from the environment (GitHub secrets), so private ids and names
    stay out of the public repo and are masked in run logs."""
    if isinstance(v, str) and v.startswith("$") and len(v) > 1:
        val = os.getenv(v[1:], "").strip()
        return int(val) if val.lstrip("-").isdigit() else val
    return v


def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        return [dict(DEFAULT_CHANNEL)]
    out = []
    for c in json.load(open(CHANNELS_FILE)):
        ch = dict(DEFAULT_CHANNEL); ch.update({k: _from_env(v) for k, v in c.items()})
        if ch["chat"] in ("", None):
            print("  ! a channel's chat id secret is not set: channel skipped"); continue
        if ch["enabled"]:
            out.append(ch)
    return out


def tg(token, method, **kw):
    """Telegram Bot API call with flood-control retry."""
    for _ in range(4):
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=60, **kw)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": False, "description": r.text[:200]}
        if j.get("ok") or j.get("error_code") != 429:
            return j
        time.sleep(int(j.get("parameters", {}).get("retry_after", 5)) + 1)
    return j


FAILURES = []                                        # any problem here makes the GitHub run go red


def cmd_channels_check():
    """Verify the bot can post in every configured channel."""
    for ch in [c for c in load_channels() if c.get("mode") != "relay"]:
        ok_before = len(FAILURES); FAILURES.append(ch["chat"])  # removed again below if the channel is ready
        token = os.getenv(ch["token_env"])
        if not token:
            print(f"  {str(ch['chat']):24s} ✗ env {ch['token_env']} not set"); continue
        me = tg(token, "getMe")
        if not me.get("ok"):
            print(f"  {str(ch['chat']):24s} ✗ bad token: {me.get('description')}"); continue
        chat = tg(token, "getChat", data={"chat_id": ch["chat"]})
        if not chat.get("ok"):
            print(f"  {str(ch['chat']):24s} ✗ bot can't see channel ({chat.get('description')}) — add @{me['result']['username']} as admin"); continue
        mem = tg(token, "getChatMember", data={"chat_id": ch["chat"], "user_id": me["result"]["id"]})
        r = mem.get("result", {})
        can = r.get("status") == "creator" or (r.get("status") == "administrator" and r.get("can_post_messages", True))
        if can:
            del FAILURES[ok_before:]
        print(f"  {str(ch['chat']):24s} {'✓ ready' if can else '✗ bot is not an admin with Post Messages'}  "
              f"({chat['result'].get('title')}, bot @{me['result']['username']})")


def post_to(ch, items, live, hour=None):
    """items: list of (png path, caption)."""
    token = os.getenv(ch["token_env"])
    silent = hour is not None and hour in (ch.get("quiet_hours") or [])
    for it in items:
        path, cap, key = (it + (None,))[:3]
        cap = cap + (f"\n\n{ch['footer']}" if ch.get("footer") else "")
        if live and not token:
            print(f"  ✗ {ch['chat']}: no bot token — add the secret {ch['token_env']} in GitHub Settings → Secrets → Actions")
            FAILURES.append(ch["chat"]); continue
        if not live:
            print(f"  [dry-run] → {ch['chat']} {os.path.basename(path)}{' (silent)' if silent else ''}\n{cap}\n")
            continue
        with open(path, "rb") as f:
            j = tg(token, "sendPhoto", data={"chat_id": ch["chat"], "caption": cap[:1024], "disable_notification": silent},
                   files={"photo": f})
        print(f"  → {ch['chat']} {os.path.basename(path)}: {'ok' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
        if j.get("ok") and key:
            remember(ch, key)
        if not j.get("ok"):
            FAILURES.append(ch["chat"])
        time.sleep(2)


def use_branding(ch):
    global BRAND, HANDLE, LANG
    if "brand" in ch and not ch["brand"]:              # "brand": "" = no signature on the cards at all
        BRAND, HANDLE = "", ""
    else:
        BRAND, HANDLE = ch.get("brand") or BRAND, ch["chat"] if str(ch["chat"]).startswith("@") else HANDLE
    LANG = ch.get("lang") or "en"


STATE_FILE = os.path.join(HERE, "state.json")       # remembers what each channel already got (no repeats)


def _state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def already_posted(ch, key, hours=12):
    now = time.time()
    return any(k == key and now - t < hours * 3600 for k, t in _state().get(ch["chat"], []))


def remember(ch, key):
    st_ = _state()
    lst = [(k, t) for k, t in st_.get(ch["chat"], []) if time.time() - t < 3 * 86400] + [(key, time.time())]
    st_[ch["chat"]] = lst
    json.dump(st_, open(STATE_FILE, "w"))


def setup_key(name, plan):
    return f"{name}|{plan['side']}|{plan['entry']:.5g}"


def build_slot(hour, ch, basis, rows_ready):
    """Charts for one channel at one hour -> list of (path, caption)."""
    now = datetime.now(TZ)
    slot = SCHEDULE.get(hour)
    items = []
    if slot and (ch["slots"] is None or hour in ch["slots"]):
        fallback = [(m, tf) for m, _, _, _ in TA_MARKETS for tf in ("1h", "15m")]   # if the planned chart has no setup
        jobs = list(slot["jobs"]) + [j for j in fallback if j not in slot["jobs"]
                                     and ("consensus" not in slot["jobs"] or ch.get("lang", "en") != "en")]
        for job in jobs:
            if job == "consensus":
                if ch.get("lang", "en") != "en":
                    continue                                   # English-only chart; fall back to entry charts
                if ch["markets"] is None or "XAUUSD" in ch["markets"]:
                    p, c, _ = chart_gold_consensus(rows_ready())
                    items.append((p, c))
                break
            name, tf = job
            if ch["markets"] is not None and name not in ch["markets"]:
                continue
            p, c, res = render_entry_chart(name, tf, 70, basis if name == "XAUUSD" else 0.0)
            if res["plan"]:                                    # first market with a NEW setup wins the slot
                key = setup_key(name, res["plan"])
                if already_posted(ch, key):
                    continue
                items.append((p, c, key)); break
    if (now.strftime("%a"), hour) == WEEKLY_PULSE and ch.get("lang", "en") == "en":
        p, c = chart_majors(); items.append((p, c))
    return items


def cmd_slot(hour=None, live=False):
    """Run from cron every hour: builds and posts what SCHEDULE says for this BRT hour, to every channel."""
    hour = datetime.now(TZ).hour if hour is None else hour
    rows_path = os.path.join(HERE, "rows.json")
    basis = gold_basis(json.load(open(rows_path)) if os.path.exists(rows_path) else None)
    fresh = {}

    def rows_ready():                                          # scrape once per run, only if a channel needs it
        if "rows" not in fresh:
            cmd_scrape(60); cmd_analyze(); fresh["rows"] = json.load(open(rows_path))
        return fresh["rows"]

    slot = SCHEDULE.get(hour)
    print(f"  {hour:02d}:00 {TZ_LABEL} · {slot['why'] if slot else 'no slot'}")
    for ch in [c for c in load_channels() if c.get("mode") != "relay"]:
        use_branding(ch)
        items = build_slot(hour, ch, basis, rows_ready)
        if not items:
            print(f"  {ch['chat']}: nothing to post"); continue
        post_to(ch, items, live, hour)


def cmd_test(live=False):
    """Connection test: post the best setup available right now (or a WAIT chart) to every channel."""
    basis = gold_basis(None)
    for ch in [c for c in load_channels() if c.get("mode") != "relay"]:
        use_branding(ch)
        pick = None
        for name, tf in [(m, t) for m, _, _, _ in TA_MARKETS for t in ("1h", "15m")]:
            if ch["markets"] is not None and name not in ch["markets"]:
                continue
            p, c, res = render_entry_chart(name, tf, 70, basis if name == "XAUUSD" else 0.0)
            pick = pick or (p, c)
            if res["plan"]:
                pick = (p, c); break
        if pick:
            post_to(ch, [pick], live)


# ================================================================= 6. POST
def cmd_post(live=False):
    """Post the charts already in charts/captions.json to every configured channel."""
    caps = json.load(open(os.path.join(CHART_DIR, "captions.json")))
    items = [(os.path.join(CHART_DIR, fn), cap) for fn, cap in caps.items()]
    for ch in [c for c in load_channels() if c.get("mode") != "relay"]:
        post_to(ch, items, live)


if __name__ == "__main__":
    a = sys.argv[1:] or ["all"]
    live = "--live" in a
    if a[0] in ("scrape", "all"):
        cmd_scrape(int(a[1]) if len(a) > 1 and a[1].isdigit() else 200)
    if a[0] in ("analyze", "all"):
        cmd_analyze()
    if a[0] in ("charts", "all"):
        cmd_charts()
    if a[0] == "post" or (a[0] == "all" and live):
        cmd_post(live)
    if a[0] == "preview":                                     # python scrape.py preview [en|pt] -> render entry charts only
        LANG = a[1] if len(a) > 1 else "en"
        b_ = gold_basis(None)
        for nm, tf_, bars_ in TA_JOBS:
            p_, c_, r_ = render_entry_chart(nm, tf_, bars_, b_ if nm == "XAUUSD" else 0.0)
            print(p_, "setup" if r_["plan"] else "wait"); print(c_, "\n")
    if a[0] == "test":                                        # python scrape.py test [--live] -> one post per channel now
        cmd_test(live)
    if a[0] == "channels":                                    # python scrape.py channels   -> check bot access
        cmd_channels_check()
    if a[0] == "slot":                                        # python scrape.py slot [hour] [--live]
        cmd_slot(int(a[1]) if len(a) > 1 and a[1].isdigit() else None, live)
    if FAILURES:
        print(f"\n  ✗ FAILED for: {sorted(set(map(str, FAILURES)))}")
        sys.exit(1)
