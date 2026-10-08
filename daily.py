"""
Daily content plan for the channel (Brazil time, Mon–Fri). Built on the chart engine in scrape.py.

  09:00 morning   good-morning card with the main markets
  11:00 overview  focus asset (yesterday's poll winner, else the biggest mover) + related markets
  13:00 signal    fresh signal from the source channel, else the result of an earlier signal, else an own setup
  15:00 poll      "which asset should I review?" card + Telegram poll; results go to the owner and steer 11:00
  17:00           posted by hand
  19:00 signal    same as 13:00
  21:00 news      market-close card + short news roundup in Portuguese

  python daily.py run HOUR [--live]   build (and with --live post) the slot for that BRT hour
  python daily.py report              poll history (also written to the GitHub job summary)

Env: TG_BOT_TOKEN (per channel via token_env), OWNER_CHAT_ID (optional, poll reports by DM),
     ANTHROPIC_API_KEY (optional, AI copywriting; without it the posts use built-in templates).
channels.json per channel: dm (public handle for the DM line in posts; empty = no DM line), owner_username (the owner's
     private Telegram username, only used to recognise the owner's messages to the bot; never posted),
     signal_source (id or title of the source channel).
"""
import json, os, re, sys, time, random
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
import scrape as S
import slots

TZ = S.TZ
C = S.CLEAN
TILE = "#16203A"
FRESH_MIN = int(os.getenv("SIGNAL_FRESH_MIN", "90"))   # a source signal older than this is never forwarded
AI_MODEL = os.getenv("AI_MODEL", "claude-opus-5-5")
WEEKDAYS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

# key, label on cards, yfinance ticker, decimals
ASSETS = [("XAUUSD", "Ouro", "GC=F", 1), ("EURUSD", "EUR/USD", "EURUSD=X", 4), ("GBPUSD", "GBP/USD", "GBPUSD=X", 4),
          ("USDJPY", "USD/JPY", "USDJPY=X", 2), ("USDBRL", "Dólar", "BRL=X", 3), ("BTCUSD", "Bitcoin", "BTC-USD", 0),
          ("WTI", "Petróleo", "CL=F", 2), ("XAGUSD", "Prata", "SI=F", 2), ("SPX", "S&P 500", "^GSPC", 0),
          ("IBOV", "Ibovespa", "^BVSP", 0)]
A = {k: dict(key=k, label=l, ticker=t, dec=d) for k, l, t, d in ASSETS}
TRADABLE = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "WTI", "XAGUSD"]     # can be the 11:00 focus
POLL_POOL = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "WTI", "USDJPY", "XAGUSD"]
RELATED = {"XAUUSD": ["XAGUSD", "EURUSD", "USDBRL"], "EURUSD": ["GBPUSD", "USDJPY", "XAUUSD"],
           "GBPUSD": ["EURUSD", "USDJPY", "XAUUSD"], "USDJPY": ["EURUSD", "XAUUSD", "SPX"],
           "BTCUSD": ["SPX", "XAUUSD", "USDBRL"], "WTI": ["XAUUSD", "SPX", "USDBRL"], "XAGUSD": ["XAUUSD", "EURUSD", "SPX"]}
NEWS_FEEDS = ["https://br.investing.com/rss/news_1.rss",
              "https://news.google.com/rss/search?q=d%C3%B3lar+OR+ouro+OR+petr%C3%B3leo+OR+ibovespa+when:1d&hl=pt-BR&gl=BR&ceid=BR:pt-419",
              "https://www.infomoney.com.br/feed/"]

HOOKS = ["📩 Quer receber as próximas entradas em tempo real? Me chama no privado: {dm}",
         "💬 Dúvida sobre essa operação ou sobre gestão de risco? Manda mensagem: {dm}",
         "🔒 No privado eu explico a gestão completa da operação. Chama: {dm}"]
HOOKS_RESULT = ["📩 Quer acompanhar as próximas em tempo real? Me chama no privado: {dm}",
                "💬 Quer entender como eu faço a gestão de cada entrada? Chama no privado: {dm}"]


# ---------------------------------------------------------------- state (shared file with scrape.py)
def load():
    st = S._state()
    return st.setdefault("_daily", {}), st


def save(st):
    json.dump(st, open(S.STATE_FILE, "w"))


# ---------------------------------------------------------------- market data
def quotes(keys, basis):
    """key -> dict(price, chg %, spark list, label, dec). Change is against the previous BRT day's close."""
    import yfinance as yf
    out = {}
    for k in keys:
        a = A[k]
        try:
            h = yf.download(a["ticker"], period="7d", interval="1h", progress=False, auto_adjust=False)
            if hasattr(h.columns, "levels"):
                h.columns = h.columns.get_level_values(0)
            c = h["Close"].dropna()
            c.index = c.index.tz_convert(TZ)
            if k == "XAUUSD":
                c = c - basis                                  # futures -> spot, like the entry charts
            today = c.index[-1].date()
            prev = c[c.index.date < today]
            ref = float(prev.iloc[-1]) if len(prev) else float(c.iloc[0])
            last = float(c.iloc[-1])
            out[k] = dict(a, price=last, chg=(last / ref - 1) * 100, spark=[float(x) for x in c.iloc[-48:]])
        except Exception as e:
            print(f"  ! no data for {k}: {e}")
    return out


def pct(x):
    return f"{x:+.2f}%"


def fmt(x, dec):
    return f"{x:,.{dec}f}".replace(",", " ") if dec == 0 else f"{x:.{dec}f}"


def pick(options):
    """Same choice all day, a different one the next day (no repeats on consecutive days)."""
    day = datetime.now(TZ).toordinal()
    return options[(day * 7 + len(options[0])) % len(options)]


# ---------------------------------------------------------------- AI copywriting (optional)
VOICE = ("Você escreve posts de Telegram para Gabriel Santos, trader brasileiro de forex e ouro. "
         "Português do Brasil, tom respeitoso, confiante e direto, frases curtas, no máximo 3 emojis. "
         "Nunca prometa lucro nem use exageros; não invente números, preços ou notícias além dos dados fornecidos. "
         "Responda só com o texto do post, sem aspas, sem markdown (sem ** nem #).")


def ai(task, facts, max_chars):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        r = anthropic.Anthropic().beta.messages.create(
            model=AI_MODEL, max_tokens=4000, system=VOICE, betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default", "output_config": {"effort": "low"}},
            messages=[{"role": "user", "content": f"{task}\nLimite: {max_chars} caracteres.\n\nDados:\n{facts}"}])
        if r.stop_reason == "refusal":
            return None
        txt = "".join(b.text for b in r.content if b.type == "text").strip()
        if len(txt) > max_chars:                       # cut at a line break, never mid-sentence
            txt = txt[:max_chars].rsplit("\n", 1)[0].strip()
        print(f"  · AI copy ok ({r.model}, {len(txt)} chars)")
        return txt or None
    except Exception as e:
        print(f"  ! AI copy failed, using template: {e}")
        return None


# ---------------------------------------------------------------- telegram
_RECENT = {}


def recent_texts(ch):
    """Texts of the channel's latest public posts (read once per run)."""
    handle = str(ch["chat"]).lstrip("@")
    if handle not in _RECENT:
        _RECENT[handle] = []
        if str(ch["chat"]).startswith("@"):
            try:
                from bs4 import BeautifulSoup
                html = requests.get(f"https://t.me/s/{handle}", headers=S.UA, timeout=30).text
                for el in BeautifulSoup(html, "html.parser").select(".tgme_widget_message_text"):
                    _RECENT[handle].append(re.sub(r"\s+", " ", el.get_text(" ")).strip())
            except Exception as e:
                print(f"  ! could not read recent posts: {e}")
    return _RECENT[handle]


def already_in_channel(ch, cap):
    """True when the caption's opening (first 3 non-empty lines) already appears in a recent post."""
    head = [re.sub(r"\s+", " ", l).strip() for l in cap.splitlines() if l.strip()][:3]
    if len(" ".join(head)) < 25:
        return False
    return any(all(l in t for l in head) for t in recent_texts(ch))


def send_photo(ch, path, cap, live, hour):
    cap = cap + (f"\n\n{ch['footer']}" if ch.get("footer") else "")
    if already_in_channel(ch, cap):
        print(f"  · {os.path.basename(path)}: same content is already in the channel, not posted again")
        return {"ok": False, "duplicate": True}
    if not live:
        print(f"  [dry-run] → {ch['chat']} {os.path.basename(path)}\n{cap}\n")
        return {"ok": True, "result": {"message_id": 0}}
    with open(path, "rb") as f:
        j = S.tg(os.getenv(ch["token_env"]), "sendPhoto", files={"photo": f},
                 data={"chat_id": ch["chat"], "caption": cap[:1024], "disable_notification": hour in (ch.get("quiet_hours") or [])})
    print(f"  → {ch['chat']} {os.path.basename(path)}: {'ok' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
    if not j.get("ok"):
        S.FAILURES.append(ch["chat"])
    return j


def send_album(ch, paths, cap, live, hour):
    cap = cap + (f"\n\n{ch['footer']}" if ch.get("footer") else "")
    if already_in_channel(ch, cap):
        print("  · album: same content is already in the channel, not posted again")
        return {"ok": False, "duplicate": True}
    if not live:
        print(f"  [dry-run] album → {ch['chat']} {[os.path.basename(p) for p in paths]}\n{cap}\n")
        return {"ok": True}
    files = {f"f{i}": open(p, "rb") for i, p in enumerate(paths[:10])}
    media = [dict(type="photo", media=f"attach://f{i}", **({"caption": cap[:1024]} if i == 0 else {})) for i in range(len(files))]
    try:
        j = S.tg(os.getenv(ch["token_env"]), "sendMediaGroup", files=files,
                 data={"chat_id": ch["chat"], "media": json.dumps(media), "disable_notification": hour in (ch.get("quiet_hours") or [])})
    finally:
        for f in files.values():
            f.close()
    print(f"  → {ch['chat']} album of {len(files)}: {'ok' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
    if not j.get("ok"):
        S.FAILURES.append(ch["chat"])
    return j


REPORT_FILE = os.path.join(S.HERE, "report.md")      # picked up by the workflow and posted to the "Poll reports" issue


def owner_chat(ch, ctx):
    """Who gets the reports: OWNER_CHAT_ID / channels.json owner_chat (numeric ids only), else the dm handle once it has
    pressed Start on the bot, else the channel's creator (found via the bot). Bots cannot message a user by @username."""
    numeric = [v for v in (os.getenv("OWNER_CHAT_ID"), ch.get("owner_chat")) if v and str(v).lstrip("-").isdigit()]
    owner = (numeric or [None])[0] or ctx["d"].get("owner_id") or ctx["d"].get("creator_id")
    if owner or not ctx["live"]:
        return owner
    j = S.tg(os.getenv(ch["token_env"]), "getChatAdministrators", data={"chat_id": ch["chat"]})
    creator = next((m["user"] for m in j.get("result", []) if m.get("status") == "creator"), None)
    if creator:
        ctx["d"]["creator_id"] = creator["id"]
        return creator["id"]
    return None


def notify_owner(ch, text, ctx):
    print("  [report]\n" + text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("```\n" + text + "\n```\n")
    if not ctx["live"]:
        return
    with open(REPORT_FILE, "a") as f:
        f.write(text + "\n\n")
    owner = owner_chat(ch, ctx)
    if owner:
        j = S.tg(os.getenv(ch["token_env"]), "sendMessage", data={"chat_id": owner, "text": text})
        if not j.get("ok"):
            print(f"  ! report DM not delivered ({j.get('description')}): open the bot in Telegram and press Start once")


def hook(ch, result=False):
    dm = (ch.get("dm") or "").strip()
    return random.choice(HOOKS_RESULT if result else HOOKS).format(dm=dm) if dm else ""


# ---------------------------------------------------------------- card helpers
def fig_base(title, subtitle):
    plt = S._mpl()
    fig = plt.figure(figsize=(8, 8), facecolor=C["bg"])
    fig.text(0.06, 0.945, title, fontsize=30, fontweight="bold", color=C["text"], va="top")
    fig.text(0.06, 0.875, subtitle, fontsize=12, color=C["muted"], va="top")
    return fig


def footer(fig, note=""):
    if note:
        fig.text(0.06, 0.055, note, fontsize=9.5, color=C["muted"], va="top", linespacing=1.5)
    fig.text(0.94, 0.055, S.BRAND, fontsize=12, color=C["gold"], fontweight="bold", ha="right")
    fig.text(0.94, 0.03, S.HANDLE, fontsize=9.5, color=C["muted"], ha="right")


def tile(fig, x, y, w, h, q, spark=True):
    import matplotlib.patches as mp
    fig.patches.append(mp.FancyBboxPatch((x, y), w, h, transform=fig.transFigure,
                                         boxstyle="round,pad=0,rounding_size=0.012", fc=TILE, ec="none"))
    up = q["chg"] >= 0
    col = C["buy"] if up else C["sell"]
    fig.text(x + 0.018, y + h - 0.022, q["label"].upper(), fontsize=10, color=C["muted"], fontweight="bold", va="top")
    fig.text(x + 0.018, y + 0.022, fmt(q["price"], q["dec"]), fontsize=17, color=C["text"], fontweight="bold", va="bottom")
    fig.text(x + w - 0.018, y + h - 0.02, f" {'▲' if up else '▼'} {pct(q['chg'])} ", fontsize=10, color=C["bg"], fontweight="bold",
             ha="right", va="top", bbox=dict(boxstyle="round,pad=0.3,rounding_size=0.3", fc=col, ec="none"))
    if spark and len(q["spark"]) > 3:
        ax = fig.add_axes([x + w * 0.56, y + 0.018, w * 0.40, h * 0.42], zorder=3); ax.axis("off")
        s = q["spark"]
        ax.plot(range(len(s)), s, color=col, lw=1.6)
        ax.fill_between(range(len(s)), s, min(s), color=col, alpha=0.12, lw=0)
        ax.set_xlim(0, len(s) - 1)


def market_card(qs, title, name):
    now = datetime.now(TZ)
    fig = fig_base(title, f"{WEEKDAYS[now.weekday()]}, {S.stamp()} {S.TZ_LABEL}  ·  variação desde o fechamento anterior")
    keys = [k for k, *_ in ASSETS if k in qs]
    cols, gx, gy, top, bottom = 2, 0.025, 0.018, 0.82, 0.11
    rows = (len(keys) + 1) // 2
    h = (top - bottom - gy * (rows - 1)) / rows
    w = (0.88 - gx) / cols
    for i, k in enumerate(keys):
        r, c = divmod(i, cols)
        tile(fig, 0.06 + c * (w + gx), top - (r + 1) * h - r * gy, w, h, qs[k])
    footer(fig, "Ouro em preço spot aproximado · dados de mercado com atraso\nNão é recomendação de investimento")
    return S.save_clean(fig, name)


def movers_text(qs, n=4):
    ks = sorted(qs, key=lambda k: -abs(qs[k]["chg"]))[:n]
    return "\n".join(f"{'🟢' if qs[k]['chg'] >= 0 else '🔴'} {qs[k]['label']} {fmt(qs[k]['price'], qs[k]['dec'])} ({pct(qs[k]['chg'])})" for k in ks)


def facts_text(qs):
    return "\n".join(f"{q['label']}: {fmt(q['price'], q['dec'])} ({pct(q['chg'])} no dia)" for q in qs.values())


# ================================================================= 09:00 morning
MORNING = ["☀️ Bom dia, traders!\n\nDestaques desta manhã:\n{m}\n\nPlano definido, risco controlado. Bons trades! 💪",
           "☀️ Bom dia!\n\nAssim os mercados começam o dia:\n{m}\n\nPaciência para esperar a entrada certa vale mais que pressa. 🎯",
           "🌅 Bom dia, pessoal!\n\nO que se moveu desde ontem:\n{m}\n\nOpere o plano, não a emoção. Bom dia de trades!",
           "☕ Bom dia, traders!\n\nPanorama da manhã:\n{m}\n\nStop sempre definido antes da entrada. Bora pro dia! 💪",
           "☀️ Bom dia!\n\nOs números de abertura:\n{m}\n\nMenos operações, mais qualidade. Excelente dia a todos."]
def slot_morning(ch, ctx):
    qs = quotes([k for k, *_ in ASSETS], ctx["basis"])
    p = market_card(qs, "Bom dia, traders", "morning.png")
    cap = ai("Escreva um post curto de bom dia para o canal: saudação, 2-3 destaques do mercado nesta manhã com os números, "
             "e uma frase sobre disciplina ou gestão de risco para o dia.", facts_text(qs), 700)
    cap = cap or pick(MORNING).format(m=movers_text(qs))
    return [(p, cap)]


# ================================================================= polls (15:00) and their results
def close_poll(ch, ctx, late=False):
    """Stop the open poll, store the result, report it to the owner. Returns the closed poll record or None.
    late=True when the 15:00 run closes it (the 11:00 run was missed)."""
    d = ctx["d"]
    op = d.get("open_poll")
    if not op or not ctx["live"]:
        return None
    j = S.tg(os.getenv(ch["token_env"]), "stopPoll", data={"chat_id": ch["chat"], "message_id": op["message_id"]})
    desc = str(j.get("description", "")).lower()
    last = op.get("last") or {}
    by_hand = last.get("is_closed") or "already been closed" in desc     # stopped in the app: use the last poll update
    poll = j.get("result") if j.get("ok") else (last if by_hand and last.get("options") else None)
    if not poll:                                                           # temporary error: keep it open, retry next run
        print(f"  ! could not close poll {op['message_id']}: {j.get('description')}")
        if any(x in desc for x in ("not found", "can't be stopped", "already")) or time.time() - op["ts"] > 3 * 86400:
            d.pop("open_poll", None)
            safe_report(ch, ctx, f"⚠️ A enquete de {datetime.fromtimestamp(op['ts'], TZ):%d/%m} foi encerrada fora do bot "
                                 f"({j.get('description')}); o resultado não pôde ser lido.")
        return None
    counts = {k: o["voter_count"] for k, o in zip(op["keys"], poll["options"])}
    total = poll.get("total_voter_count", sum(counts.values()))
    winner = max(counts, key=counts.get) if total else None
    rec = dict(date=datetime.fromtimestamp(op["ts"], TZ).strftime("%Y-%m-%d"), counts=counts, total=total, winner=winner,
               reviewed=False)
    d.setdefault("polls", []).append(rec); d["polls"] = d["polls"][-120:]
    d.pop("open_poll", None)
    pref = d.setdefault("pref", {})                       # running audience preference (decays so tastes can change)
    for k in pref:
        pref[k] *= 0.85
    for k, v in counts.items():
        pref[k] = pref.get(k, 0) + (v / total if total else 0)
    try:
        members = S.tg(os.getenv(ch["token_env"]), "getChatMemberCount", data={"chat_id": ch["chat"]}).get("result")
    except Exception:
        members = None
    rec["members"] = members
    prev = [p["total"] for p in d["polls"][-8:-1]]
    avg = sum(prev) / len(prev) if prev else None
    trend = " · ".join(str(p["total"]) for p in d["polls"][-6:])
    top = sorted(pref.items(), key=lambda x: -x[1])[:3]
    if not winner:
        adjust = "Sem votos: a análise das 11h usa o ativo que mais se moveu."
    elif late:
        adjust = (f"Encerrada às 15h (a execução das 11h não rodou). {A[winner]['label']} será analisado na próxima 11h "
                  f"se a nova enquete não tiver votos.")
    else:
        adjust = (f"Ajuste: a análise das 11h é sobre {A[winner]['label']}; as próximas enquetes e os setups priorizam "
                  + ", ".join(A[k]["label"] for k, _ in top[:2]) + ".")
    lines = [f"📊 Enquete de {rec['date']} · {ch['chat']}",
             f"Votantes: {total}" + (f" de {members} inscritos ({total / members:.0%} engajados)" if members else "")
             + (f" · média anterior {avg:.1f}" if avg is not None else ""),
             *[f"  {POLL_ICON.get(k, '')} {A[k]['label']}: {v} ({v / total:.0%})" if total else f"  {A[k]['label']}: 0"
               for k, v in sorted(counts.items(), key=lambda x: -x[1])],
             f"Últimas enquetes (votantes): {trend}",
             "Preferência acumulada: " + ", ".join(f"{A[k]['label']} {v:.2f}" for k, v in top),
             adjust]
    safe_report(ch, ctx, "\n".join(lines))
    return rec


def safe_report(ch, ctx, text):
    try:
        notify_owner(ch, text, ctx)
    except Exception as e:                                # a report must never block the slot's post
        print(f"  ! report not delivered: {e}")


def poll_options(d):
    """Two audience favourites + the two assets offered longest ago (never-offered first), so every asset rotates in."""
    pref = d.get("pref", {})
    ranked = sorted(POLL_POOL, key=lambda k: -pref.get(k, 0))
    polls = d.get("polls", [])
    age = lambda k: max((i for i, p in enumerate(polls) if k in p.get("counts", {})), default=-1)
    rest = sorted((k for k in POLL_POOL if k not in ranked[:2]), key=age)
    return ranked[:2] + rest[:2]


POLL_ICON = {"XAUUSD": "🥇", "EURUSD": "💶", "GBPUSD": "💷", "BTCUSD": "🪙", "WTI": "🛢", "USDJPY": "💴", "XAGUSD": "🥈"}
POLL_Q = ["📊 Qual ativo você quer que eu analise amanhã às 11h?",
          "🗳 Você escolhe: qual ativo ganha análise completa amanhã às 11h?",
          "📈 Amanhã às 11h eu analiso o mais votado. Qual vai ser?",
          "🔎 Qual gráfico você quer ver destrinchado amanhã às 11h?"]


def slot_poll(ch, ctx):
    d = ctx["d"]
    close_poll(ch, ctx, late=True)
    q = pick(POLL_Q)
    if datetime.now(TZ).weekday() == 4:                      # Friday: the next 11:00 run is on Monday
        q = q.replace("amanhã", "segunda-feira").replace("Amanhã", "Segunda-feira")
    return [{"poll": dict(question=q, keys=poll_options(d))}]


def send_poll(ch, spec, ctx):
    if not ctx["live"]:
        print(f"  [dry-run] poll → {ch['chat']}: {spec['question']} {[A[k]['label'] for k in spec['keys']]}\n")
        return
    j = S.tg(os.getenv(ch["token_env"]), "sendPoll",
             data={"chat_id": ch["chat"], "question": spec["question"], "is_anonymous": True,
                   "options": json.dumps([{"text": f"{POLL_ICON.get(k, '')} {A[k]['label']}".strip()} for k in spec["keys"]]),
                   "disable_notification": ctx["hour"] in (ch.get("quiet_hours") or [])})
    print(f"  → {ch['chat']} poll: {'ok' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
    if j.get("ok"):
        ctx["d"]["open_poll"] = dict(message_id=j["result"]["message_id"], keys=spec["keys"], ts=time.time(),
                                     poll_id=j["result"].get("poll", {}).get("id"))
    else:
        S.FAILURES.append(ch["chat"])


# ================================================================= 11:00 overview
def slot_overview(ch, ctx):
    d = ctx["d"]
    rec = close_poll(ch, ctx)
    if not (rec and rec.get("winner")):
        cutoff = (datetime.now(TZ) - timedelta(days=4)).strftime("%Y-%m-%d")
        rec = next((p for p in reversed(d.get("polls", [])) if not p["reviewed"] and p["winner"] and p["date"] >= cutoff), None)
    qs = quotes(list(A), ctx["basis"])
    if rec and rec.get("winner") in TRADABLE:
        focus, voted = rec["winner"], rec
    else:
        cand = [k for k in TRADABLE if k in qs and k != d.get("last_focus")]
        focus, voted = max(cand, key=lambda k: abs(qs[k]["chg"]) * (1 + d.get("pref", {}).get(k, 0))), None
    p, facts = overview_card(focus, qs, ctx["basis"])

    def posted():
        d["last_focus"] = focus
        d.setdefault("covered", {})[f"{datetime.now(TZ):%Y-%m-%d}:11"] = focus
        if voted:
            voted["reviewed"] = True
    intro = f"🗳 Vocês escolheram: {A[focus]['label'].upper()} ({voted['counts'][focus]} votos). Aqui está a análise.\n\n" if voted else ""
    cap = ai(f"Escreva uma visão geral curta de {A[focus]['label']} para hoje: tendência, níveis principais (suporte/resistência) "
             f"e como os ativos relacionados se moveram. Termine com o que observar no dia.", facts, 800 - len(intro))
    cap = intro + (cap or template_overview(focus, facts))
    return [(p, cap, {"after": posted})]


def template_overview(focus, f):
    return (f"🔎 {A[focus]['label'].upper()} · visão do dia\n\n{f['trend_line']}\n"
            f"Resistência: {f['res']} · Suporte: {f['sup']}\n\nRelacionados:\n{f['rel']}\n\n⚠️ Não é recomendação de investimento.")


def overview_card(focus, qs, basis):
    import matplotlib.patches as mp
    a = A[focus]
    df = S.load_tf(a["ticker"], "4h") - (basis if focus == "XAUUSD" else 0)
    res = S.analyse(df)
    view = df.iloc[-60:]
    n, last, dec = len(view), res["last"], a["dec"]
    trend = res["trend"] if res["bias"] != "NEUTRAL" else 0
    tword, tcol = {1: ("TENDÊNCIA DE ALTA", C["buy"]), -1: ("TENDÊNCIA DE BAIXA", C["sell"]), 0: ("LATERAL", C["gold"])}[trend]
    lv = [l["price"] for l in res["levels"]]
    above = sorted(x for x in lv if x > last)[:2]
    below = sorted((x for x in lv if x < last), reverse=True)[:2]
    q = qs.get(focus, dict(chg=0.0))
    fig = fig_base(a["label"].upper(), f"Visão geral · gráfico 4H  ·  {S.stamp()} {S.TZ_LABEL}")
    fig.text(0.94, 0.935, f"  {tword}  ", fontsize=17, fontweight="bold", color=C["bg"], ha="right", va="top",
             bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.4", fc=tcol, ec="none"))
    fig.text(0.06, 0.835, f"Preço {fmt(last, dec)}  ·  {pct(q['chg'])} hoje", fontsize=13, color=C["text"], va="top")
    ax = fig.add_axes([0.11, 0.33, 0.79, 0.47]); ax.set_facecolor(C["panel"])
    for s in ax.spines.values():
        s.set_visible(False)
    lo, hi = float(view["Low"].min()), float(view["High"].max())
    pad = (hi - lo) * 0.08
    ax.set_ylim(lo - pad, hi + pad); ax.set_xlim(-1, n + 20)
    ax.grid(axis="y", color=C["grid"], lw=0.8); ax.grid(axis="x", visible=False)
    for i, (o, h, l, c) in enumerate(view[["Open", "High", "Low", "Close"]].itertuples(index=False)):
        col = C["buy"] if c >= o else C["sell"]
        ax.vlines(i, l, h, color=col, lw=1.1)
        ax.add_patch(mp.Rectangle((i - 0.35, min(o, c)), 0.7, max(abs(c - o), (hi - lo) * 0.002), color=col, lw=0))
    e = res["ema50"].iloc[-n:].values
    ax.plot(range(n), e, color=S.TV["ema"], lw=1.4, alpha=0.9)
    for y, txt, col in [(x, "RESISTÊNCIA", C["sell"]) for x in above] + [(x, "SUPORTE", C["buy"]) for x in below]:
        if lo - pad < y < hi + pad:
            ax.hlines(y, -1, n + 20, color=col, lw=1.3, ls=(0, (5, 3)), alpha=0.9)
            S._pill(ax, n + 2, y, f"{txt}  {fmt(y, dec)}", col, C["bg"], 9.5)
    ax.hlines(last, -1, n + 20, color=C["gold"], lw=1, ls=(0, (2, 2)))
    S._pill(ax, -0.012, last, fmt(last, dec), C["gold"], C["bg"], 10, ha="right", transform=ax.get_yaxis_transform())
    ax.yaxis.tick_left(); ax.tick_params(axis="y", labelsize=10, colors=C["muted"], length=0); ax.set_xticks([])
    ticks = [t for t in ax.get_yticks() if lo - pad < t < hi + pad]
    ax.set_yticks(ticks); ax.set_yticklabels([f"{t:.{max(dec - 1, 0)}f}" for t in ticks])
    fig.text(0.11, 0.315, f"Últimos {n} candles de 4H · linha azul = média de 50", fontsize=9.5, color=C["muted"], va="top")
    rel = [k for k in RELATED.get(focus, []) if k in qs][:3]
    w = (0.88 - 0.02 * (len(rel) - 1)) / max(len(rel), 1)
    for i, k in enumerate(rel):
        tile(fig, 0.06 + i * (w + 0.02), 0.11, w, 0.15, qs[k], spark=False)
    footer(fig, "Não é recomendação de investimento")
    p = S.save_clean(fig, f"overview_{focus}.png")
    tl = {1: "Estrutura de alta no 4H, preço acima da média de 50.", -1: "Estrutura de baixa no 4H, preço abaixo da média de 50.",
          0: "Sem direção clara no 4H, preço lateralizado."}[trend]
    facts = dict(trend_line=tl, res=" / ".join(fmt(x, dec) for x in above) or "—", sup=" / ".join(fmt(x, dec) for x in below) or "—",
                 rel="\n".join(f"{'🟢' if qs[k]['chg'] >= 0 else '🔴'} {qs[k]['label']} {pct(qs[k]['chg'])}" for k in rel))
    facts_str = (f"Ativo: {a['label']}, preço {fmt(last, dec)}, {pct(q['chg'])} hoje. {tl} "
                 f"Resistências: {facts['res']}. Suportes: {facts['sup']}. Relacionados: "
                 + ", ".join(f"{qs[k]['label']} {pct(qs[k]['chg'])}" for k in rel))
    facts["__str__"] = facts_str
    return p, Facts(facts)


class Facts(dict):
    def __str__(self):
        return self["__str__"]


# ================================================================= 13:00 / 19:00 signals
RU = [(r"ПОКУПК\w*|ПОКУПАЕМ|ЛОНГ|LONG", "BUY"), (r"ПРОДАЖ\w*|ПРОДАЕМ|ПРОДАЁМ|ШОРТ|SHORT", "SELL"), (r"СТОП[\s-]*ЛОСС|СТОП|СЛ\b", "SL"),
      (r"ТЕЙК[\s-]*ПРОФИТ|ТЕЙК|ТП", "TP"), (r"ВХОД|ENTRY\s*(PRICE|ZONE)?|ENTRADA|ЦЕНА", "ENTRY"), (r"STOP\s*LOSS", "SL"),
      (r"TAKE\s*PROFIT|TARGET", "TP")]
NUM = r"(\d+(?:\.\d+)?)"


def parse_signal(text):
    """Forex signal text (EN/RU/PT) -> dict(name, side, entry, sl, tp1, tp2, at_market) or None."""
    t = re.sub(r"(?<=\d),(?=\d)", ".", text.upper())
    for rx, rep in RU:
        t = re.sub(rx, rep, t)
    name = S.instrument(t)
    side = re.search(r"\b(BUY|SELL)\b", t)
    if not name or not side:
        return None
    sl = re.search(r"\bSL\b\s*[:\-=@]?\s*" + NUM, t)
    tps = [float(x) for x in re.findall(r"\bTP(?:\s?\d(?![\d.]))?\s*[:\-=@.)]?\s*" + NUM, t)]
    m = (re.search(r"\bENTRY\b\s*[:\-=@]?\s*" + NUM + r"(?:\s*[-–/]\s*" + NUM + ")?", t)
         or re.search(r"\b(?:BUY|SELL)\b(?:\s*(?:NOW|LIMIT|STOP))?\s*(?:@|AT|:|-)?\s*" + NUM + r"(?:\s*[-–/]\s*" + NUM + ")?", t))
    if not (sl and tps and m):
        return None
    ent = [float(x) for x in m.groups() if x]
    entry, s, sd = sum(ent) / len(ent), float(sl.group(1)), side.group(1)
    tp1 = tps[0]
    tp2 = tps[1] if len(tps) > 1 else tp1 + (tp1 - entry)
    ok = (s < entry < tp1 <= tp2) if sd == "BUY" else (s > entry > tp1 >= tp2)
    if not ok:
        return None
    return dict(name=name, side=sd, entry=entry, sl=s, tp1=tp1, tp2=tp2, at_market="LIMIT" not in t)


def pull_updates(ch, ctx):
    """Every run: read the bot's updates. Source-channel signals go to state, the owner's chat id is learnt from a
    private message by the dm handle, and poll updates keep the latest vote counts (used if a poll is closed by hand)."""
    d, token = ctx["d"], os.getenv(ch["token_env"])
    if not token:
        return
    want = str(ch.get("signal_source") or "AFX").lower()
    targets = {str(c["chat"]).lstrip("@").lower() for c in S.load_channels()}
    off = d.get("offset", 0)
    for _ in range(20):                                   # page through backlogs (busy polls), at most 2000 updates
        j = S.tg(token, "getUpdates", data={"offset": off, "limit": 100, "timeout": 0,
                                             "allowed_updates": json.dumps(["channel_post", "message", "my_chat_member", "poll"])})
        if not j.get("ok"):
            print(f"  ! getUpdates: {j.get('description')}"); break
        for u in j["result"]:
            off = u["update_id"] + 1
            if ctx["live"]:
                d["offset"] = off
            pl = u.get("poll")
            op = d.get("open_poll")
            if pl and op and pl.get("id") == op.get("poll_id"):
                op["last"] = pl
                continue
            m = u.get("message")
            if m and m.get("chat", {}).get("type") == "private":
                who = str(m["chat"].get("username", "")).lower()
                print(f"  · private message from @{who} — chat id {m['chat']['id']}")
                if who and who == str(ch.get("owner_username", "")).lstrip("@").lower() and ctx["live"]:
                    if d.get("owner_id") != m["chat"]["id"]:
                        S.tg(token, "sendMessage", data={"chat_id": m["chat"]["id"], "text":
                             f"✅ Conectado ao {ch['chat']}. Você vai receber aqui os relatórios das enquetes e qualquer alerta do autoposter."})
                    d["owner_id"] = m["chat"]["id"]
                    print("    → saved as the owner: poll reports and alerts go here")
                continue
            cm = u.get("my_chat_member")
            if cm:
                print(f"  · bot is now '{cm['new_chat_member']['status']}' in {cm['chat'].get('title')} (id {cm['chat']['id']})")
                continue
            post = u.get("channel_post")
            if not post:
                continue
            chat = post["chat"]
            if str(chat.get("username", "")).lower() in targets:
                continue
            if want not in (str(chat["id"]), str(chat.get("title", "")).lower()) and want not in str(chat.get("title", "")).lower():
                print(f"  · ignored post from {chat.get('title')} (id {chat['id']}): not the signal source '{want}'")
                continue
            d["source_seen"] = dict(title=chat.get("title"), id=chat["id"], ts=post["date"])
            sig = parse_signal(post.get("text") or post.get("caption") or "")
            print(f"  · source post {post['message_id']} from {chat.get('title')}: {'signal ' + sig['name'] + ' ' + sig['side'] if sig else 'no signal'}")
            if sig:
                d.setdefault("src", []).append(dict(sig, ts=post["date"], id=post["message_id"], posted=False))
        if len(j["result"]) < 100 or not ctx["live"]:   # a dry run never pages: a higher offset deletes updates
            break
    d["src"] = [s for s in d.get("src", []) if time.time() - s["ts"] < 3 * 86400]


def tf_for(name):
    return "15m" if name in ("XAUUSD", "XAGUSD", "BTCUSD", "US30", "NAS100") else "1h"


def slot_signal(ch, ctx):
    d = ctx["d"]
    now = time.time()
    # 1. a fresh signal from the source channel, posted inside Brazilian daytime
    for s in sorted(d.get("src", []), key=lambda s: -s["ts"]):
        brt = datetime.fromtimestamp(s["ts"], TZ)
        if s["posted"] or now - s["ts"] > FRESH_MIN * 60 or not 7 <= brt.hour < 23:
            continue
        if ch["markets"] is not None and s["name"] not in ch["markets"] and s["name"] not in [m for m, *_ in S.EXTRA_MARKETS]:
            continue
        item = signal_post(ch, ctx, s, source=True)
        if item and seen_setup(d, s["name"], item[2]["signal"]):
            print(f"  · source signal {item[2]['signal']['key']} was already posted: skipped")
            if ctx["live"]:
                s["posted"] = True
            continue
        if item:
            item[2]["after"] = lambda s=s: s.update(posted=True)
            return [item]
    # 2. how an earlier signal played out
    item = followup_post(ch, ctx)
    if item:
        return [item]
    # 3. own setup from the chart engine
    pref = d.get("pref", {})
    for name in sorted(ch["markets"] or ["XAUUSD", "EURUSD", "GBPUSD"], key=lambda m: -pref.get(m, 0)):
        for tf in ("15m", "1h") if name == "XAUUSD" else ("1h",):
            p, c, res = S.render_entry_chart(name, tf, 70, ctx["basis"] if name == "XAUUSD" else 0.0)
            if res["plan"] and not seen_setup(d, name, res["plan"]):
                return [finish_signal(ch, ctx, name, tf, res["plan"], p, c, "own")]
    # 4. never leave the slot empty: a market panorama of an asset not covered yet today
    print("  no fresh signal, no result to show and no new setup: posting a market panorama")
    return [panorama_item(ch, ctx)]


def seen_setup(d, name, plan, hours=24):
    """Rule 1: same instrument and side with an entry within 0.15% of a signal posted in the last day is a repeat."""
    for x in d.get("signals", []):
        if (x["name"] == name and x["side"] == plan["side"] and time.time() - x["ts"] < hours * 3600
                and abs(x["entry"] - plan["entry"]) / plan["entry"] < 0.0015):
            return True
    return False


PANORAMA_OPEN = ["📊 Sem entrada limpa neste momento. Paciência também é posição.",
                 "📊 Nenhum setup novo agora, então segue o panorama.",
                 "📊 Mercado sem gatilho claro no momento. Vale acompanhar este ativo."]


def panorama_item(ch, ctx):
    d = ctx["d"]
    qs = quotes(list(A), ctx["basis"])
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    covered = {v for k, v in d.get("covered", {}).items() if k.startswith(today)} | {d.get("last_focus")}
    cand = [k for k in TRADABLE if k in qs and k not in covered] or [k for k in TRADABLE if k in qs]
    focus = max(cand, key=lambda k: abs(qs[k]["chg"]))
    p, facts = overview_card(focus, qs, ctx["basis"])
    cap = ai(f"Escreva um panorama curto de {A[focus]['label']} para agora: tendência, níveis principais e o que esperar "
             f"nas próximas horas. Comece dizendo que não há entrada limpa neste momento.", facts, 750)
    cap = cap or f"{pick(PANORAMA_OPEN)}\n\n" + template_overview(focus, facts)
    h = hook(ch)
    if h:
        cap = cap.replace("\n\n⚠️", f"\n\n{h}\n\n⚠️", 1) if "\n\n⚠️" in cap else cap + f"\n\n{h}"

    def posted():
        d.setdefault("covered", {})[f"{today}:{ctx['hour']}"] = focus
        for k in [k for k in d["covered"] if k[:10] < (datetime.now(TZ) - timedelta(days=3)).strftime("%Y-%m-%d")]:
            del d["covered"][k]
    return (p, cap, {"after": posted})


def signal_post(ch, ctx, s, source):
    name, tf = s["name"], tf_for(s["name"])
    basis = ctx["basis"] if name == "XAUUSD" else 0.0
    plan = dict(side=s["side"], entry=s["entry"], sl=s["sl"], tp1=s["tp1"], tp2=s["tp2"])
    try:
        df = S.load_tf(next(t for n, t, *_ in S.TA_MARKETS + S.EXTRA_MARKETS if n == name), tf) - basis
    except Exception as e:
        print(f"  ! no chart data for {name}: {e}"); return None
    last = float(df["Close"].iloc[-1])
    if not all(abs(v / last - 1) < 0.06 for v in plan.values() if isinstance(v, float)):
        print(f"  ! source signal {name} prices are far from the market ({last:.5g}), skipped"); return None
    plan["at_market"] = s.get("at_market", True) and abs(plan["entry"] - last) / last < 0.002
    word = "Entrada a mercado." if plan["at_market"] else ("Entrada num recuo até a zona marcada." if s["side"] == "BUY"
                                                          else "Entrada num repique até a zona marcada.")
    plan["reason"] = f"Sinal {'de compra' if s['side'] == 'BUY' else 'de venda'} no {tf.upper()}. {word}"
    p, c, res = S.render_entry_chart(name, tf, 70, basis, plan=plan)
    return finish_signal(ch, ctx, name, tf, res["plan"], p, c, "source")


def finish_signal(ch, ctx, name, tf, plan, p, cap, origin):
    h = hook(ch)
    cap = cap.replace("\n\n⚠️", (f"\n\n{h}" if h else "") + "\n\n⚠️", 1)
    rec = dict(key=S.setup_key(name, plan), name=name, tf=tf, side=plan["side"], entry=plan["entry"], sl=plan["sl"],
               tp1=plan["tp1"], tp2=plan["tp2"], at_market=plan["at_market"], ts=time.time(), origin=origin, followed=False)
    return (p, cap, {"signal": rec})


def outcome(sig, basis):
    """Walk 15m candles since the post: (status, best price reached). status: open | TP1 | TP2 | SL | BE | expired."""
    tk = next(t for n, t, *_ in S.TA_MARKETS + S.EXTRA_MARKETS if n == sig["name"])
    df = S.load_tf(tk, "15m") - basis
    df = df[df.index >= datetime.fromtimestamp(sig["ts"], TZ)]
    up = sig["side"] == "BUY"
    live, tp1 = sig["at_market"], False
    for hi, lo in zip(df["High"], df["Low"]):
        if not live:
            live = lo <= sig["entry"] <= hi
            if not live:
                continue
        stop = sig["entry"] if tp1 else sig["sl"]
        if (lo <= stop) if up else (hi >= stop):
            return ("BE" if tp1 else "SL"), df
        if (hi >= sig["tp2"]) if up else (lo <= sig["tp2"]):
            return "TP2", df
        if (hi >= sig["tp1"]) if up else (lo <= sig["tp1"]):
            tp1 = True
    if not live:
        return ("expired" if time.time() - sig["ts"] > 86400 else "open"), df
    return ("TP1" if tp1 else "open"), df


def post_exists(ch, mid):
    """Is channel message `mid` still published? (a deleted signal must not get a result post)"""
    handle = str(ch["chat"]).lstrip("@")
    if not mid or not str(ch["chat"]).startswith("@"):
        return bool(mid)
    try:
        html = requests.get(f"https://t.me/s/{handle}?before={mid + 1}", headers=S.UA, timeout=30).text
        return f'data-post="{handle}/{mid}"' in html
    except Exception:
        return True                                        # can't tell: don't block the post


def followup_post(ch, ctx):
    d = ctx["d"]
    for sig in sorted(d.get("signals", []), key=lambda s: -s["ts"]):
        if sig["followed"] or time.time() - sig["ts"] < 2 * 3600 or time.time() - sig["ts"] > 3 * 86400:
            continue
        if not post_exists(ch, sig.get("msg_id")):
            print(f"  · signal {sig['key']} is no longer in the channel: no result post")
            if ctx["live"]:
                sig["followed"] = True
            continue
        basis = ctx["basis"] if sig["name"] == "XAUUSD" else 0.0
        try:
            st_, df = outcome(sig, basis)
        except Exception as e:
            print(f"  ! result check failed for {sig['key']}: {e}"); continue
        if st_ in ("open",):
            continue
        if st_ == "expired":
            if ctx["live"]:
                sig["followed"] = True
            continue
        p, cap = result_card(ch, sig, st_, basis)
        return (p, cap, {"after": lambda sig=sig: sig.update(followed=True)})
    return None


def result_card(ch, sig, status, basis):
    import matplotlib.patches as mp
    name = sig["name"]
    tk, pip, d = next((t, p, d) for n, t, p, d in S.TA_MARKETS + S.EXTRA_MARKETS if n == name)
    dec = 1 if name == "XAUUSD" else d
    hit = {"TP2": sig["tp2"], "TP1": sig["tp1"], "BE": sig["entry"], "SL": sig["sl"]}[status]
    pips = ((sig["tp1"] if status == "BE" else hit) - sig["entry"]) * (1 if sig["side"] == "BUY" else -1) / pip
    ptxt = f"{'TP1 ' if status == 'BE' else ''}{pips:+.0f} pips"
    label, col = {"TP2": ("TP2 ATINGIDO", C["buy"]), "TP1": ("TP1 ATINGIDO", C["buy"]),
                  "BE": ("TP1 + SAÍDA NO ZERO", C["buy"]), "SL": ("STOP ATINGIDO", C["sell"])}[status]
    before = S.load_tf(tk, "15m") - basis
    start = datetime.fromtimestamp(sig["ts"], TZ)
    view = before[before.index >= start - timedelta(hours=8)].iloc[-90:]
    n = len(view)
    t0 = int(view.index.searchsorted(start))
    fig = fig_base(S.pname(name), f"Resultado da operação · {sig['tf'].upper()}  ·  sinal de {start.strftime('%d/%m %H:%M')} {S.TZ_LABEL}")
    fig.text(0.94, 0.935, f"  {label}  ", fontsize=19, fontweight="bold", color=C["bg"], ha="right", va="top",
             bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.4", fc=col, ec="none"))
    fig.text(0.06, 0.835, f"{S.side_word(sig['side'])} em {fmt(sig['entry'], dec)}  →  {ptxt}",
             fontsize=15, color=col, fontweight="bold", va="top")
    ax = fig.add_axes([0.11, 0.30, 0.79, 0.49]); ax.set_facecolor(C["panel"])
    for s in ax.spines.values():
        s.set_visible(False)
    lv = [sig["sl"], sig["entry"], sig["tp1"], sig["tp2"]]
    lo, hi = min(float(view["Low"].min()), *lv), max(float(view["High"].max()), *lv)
    pad = (hi - lo) * 0.07
    ax.set_ylim(lo - pad, hi + pad); ax.set_xlim(-1, n + 22)
    ax.grid(axis="y", color=C["grid"], lw=0.8); ax.grid(axis="x", visible=False)
    for i, (o, h, l, c) in enumerate(view[["Open", "High", "Low", "Close"]].itertuples(index=False)):
        cc = C["buy"] if c >= o else C["sell"]
        ax.vlines(i, l, h, color=cc, lw=1.1, alpha=0.9 if i >= t0 else 0.45)
        ax.add_patch(mp.Rectangle((i - 0.35, min(o, c)), 0.7, max(abs(c - o), (hi - lo) * 0.002), color=cc, lw=0,
                                  alpha=1 if i >= t0 else 0.45))
    ax.axvline(t0 - 0.5, color=C["muted"], lw=1, ls=(0, (3, 3)))
    ax.text(t0, hi + pad * 0.6, " SINAL", color=C["muted"], fontsize=9, fontweight="bold", va="center")
    for y, txt, cc in ((sig["tp2"], "TP2", C["buy"]), (sig["tp1"], "TP1", C["buy"]), (sig["entry"], S.T("ENTRY"), C["text"]),
                       (sig["sl"], "SL", C["sell"])):
        ax.hlines(y, t0 - 0.5, n + 22, color=cc, lw=1.5 if y != hit else 2.6)
        mark = sig["tp1"] if status == "BE" else hit
        S._pill(ax, n + 1.5, y, f"{txt}  {fmt(y, dec)}{'  ✓' if y == mark and status != 'SL' else ''}", cc, C["bg"], 10.5)
    ax.yaxis.tick_left(); ax.tick_params(axis="y", labelsize=10, colors=C["muted"], length=0); ax.set_xticks([])
    ticks = [t for t in ax.get_yticks() if lo - pad < t < hi + pad]
    ax.set_yticks(ticks); ax.set_yticklabels([f"{t:.{0 if dec <= 1 else dec - 1}f}" for t in ticks])
    msg = {"TP2": "Alvo final atingido. Operação encerrada com lucro máximo do plano.",
           "TP1": "Primeiro alvo atingido. Stop movido para a entrada: o restante segue sem risco.",
           "BE": "TP1 atingido e o restante saiu no zero, como manda a gestão.",
           "SL": "Stop atingido. Faz parte: perda pequena e controlada, dentro do plano."}[status]
    fig.text(0.06, 0.25, msg, fontsize=13, color=C["text"], va="top")
    fig.text(0.06, 0.20, "Gestão: risco máximo de 1% por operação, SL para a entrada após o TP1.", fontsize=11, color=C["muted"], va="top")
    footer(fig, "Resultados passados não garantem resultados futuros\nNão é recomendação de investimento")
    p = S.save_clean(fig, f"result_{name}.png")
    icon = "✅" if status != "SL" else "❌"
    cap = f"{icon} {S.pname(name)} · {label}\n\n{S.side_word(sig['side'])} em {fmt(sig['entry'], dec)} → {ptxt}\n{msg}"
    h = hook(ch, result=True)
    cap += (f"\n\n{h}" if h else "") + "\n\n⚠️ Não é recomendação de investimento."
    return (p, cap)


# ================================================================= 21:00 news roundup (carousel)
SOURCES = {"br.investing.com": "Investing.com", "www.infomoney.com.br": "InfoMoney"}
TOPICS = [  # tag on the slide, keywords, asset shown as "impact"
    ("DÓLAR", r"D[ÓO]LAR|C[ÂA]MBIO", "USDBRL"), ("OURO", r"\bOURO\b", "XAUUSD"), ("PETRÓLEO", r"PETR[ÓO]LEO|OPEP|BRENT", "WTI"),
    ("BOLSA", r"IBOVESPA|BOLSA|B3\b", "IBOV"), ("CRIPTO", r"BITCOIN|CRIPTO", "BTCUSD"), ("EURO", r"\bEURO\b|BCE\b", "EURUSD"),
    ("LIBRA", r"LIBRA|BOE\b|REINO UNIDO", "GBPUSD"), ("IENE", r"IENE|JAP[ÃA]O|BOJ\b", "USDJPY"),
    ("EUA", r"WALL STREET|S&P|NASDAQ|\bFED\b|TREASUR|PAYROLL|EUA\b", "SPX"), ("JUROS", r"JUROS|COPOM|SELIC|INFLA|IPCA|PIB\b", None)]
TRUSTED = r"FOLHA|G1|CNN|VALOR|ESTAD|INFOMONEY|EXAME|REUTERS|BLOOMBERG|MONEY TIMES|INVESTING|UOL|PODER360|O GLOBO|INVEST NEWS|BTG|XP|E-INVESTIDOR|SEU DINHEIRO|BRAZIL JOURNAL"
OFF_TOPIC = r"FUTSAL|FUTEBOL|MEDALHA|CAMPEONATO|OL[ÍI]MPI|COPA DO|JOGOS|PARTIDA|PREMIA[ÇC][ÃA]O|BODAS|JOIA"
TAG_COL = {"DÓLAR": "#22C38E", "OURO": "#F2B33D", "PETRÓLEO": "#F08A4B", "BOLSA": "#5B8CFF", "CRIPTO": "#F7931A",
           "EURO": "#5B8CFF", "LIBRA": "#B07CF0", "IENE": "#F0506E", "EUA": "#5BC0EB", "JUROS": "#E6EAF2"}


def news(hours=14):
    items, seen = [], set()
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    for url in NEWS_FEEDS:
        try:
            root = ET.fromstring(requests.get(url, headers=S.UA, timeout=30).content)
        except Exception as e:
            print(f"  ! feed {url[:50]}: {e}"); continue
        host = url.split("/")[2]
        for it in root.iter("item"):
            title, src = (it.findtext("title") or "").strip(), SOURCES.get(host, host)
            if "news.google" in host and " - " in title:
                title, src = title.rsplit(" - ", 1)
                if not re.search(TRUSTED, src.upper()):
                    continue
            raw = it.findtext("pubDate") or ""
            try:
                dt = parsedate_to_datetime(raw)
            except Exception:
                try:
                    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
            key = re.sub(r"\W+", "", title.lower())[:60]
            if re.search(OFF_TOPIC, title.upper()):
                continue
            if dt >= cut and title and key not in seen:
                seen.add(key)
                tag = next(((t, a) for t, rx, a in TOPICS if re.search(rx, title.upper())), None)
                items.append(dict(title=title, dt=dt, src=src.strip(), tag=tag[0] if tag else None, asset=tag[1] if tag else None))
    return sorted(items, key=lambda x: x["dt"], reverse=True)


def news_key(title):
    return re.sub(r"\W+", "", title.lower())[:60]


def pick_news(items, n=5, seen=()):
    """Most recent market headline per topic first, so the carousel covers different subjects.
    Stories already used in an earlier carousel (seen) are left out."""
    items = [i for i in items if news_key(i["title"]) not in seen]
    out, used = [], set()
    for it in items:
        if it["tag"] and it["tag"] not in used:
            out.append(it); used.add(it["tag"])
    out += [i for i in items if i["tag"] and i not in out]
    return out[:n]


def wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width) or [""]


def news_cover(heads, qs):
    now = datetime.now(TZ)
    fig = fig_base("Resumo do dia", f"{WEEKDAYS[now.weekday()]}, {S.stamp()} {S.TZ_LABEL}  ·  o que mexeu com os mercados")
    import matplotlib.patches as mp
    y = 0.80
    for i, h in enumerate(heads):
        lines = wrap(h["title"], 44)[:2]
        if len(wrap(h["title"], 44)) > 2:
            lines[-1] = lines[-1].rstrip(".,;: ") + "…"
        hgt = 0.05 + 0.032 * len(lines)
        fig.patches.append(mp.FancyBboxPatch((0.06, y - hgt), 0.88, hgt, transform=fig.transFigure,
                                             boxstyle="round,pad=0,rounding_size=0.012", fc=TILE, ec="none"))
        fig.text(0.085, y - 0.024, f"{i + 1:02d}", fontsize=17, color=C["gold"], fontweight="bold", va="top")
        fig.text(0.16, y - 0.022, h["tag"], fontsize=9.5, color=TAG_COL.get(h["tag"], C["muted"]), fontweight="bold", va="top")
        fig.text(0.16, y - 0.05, "\n".join(lines), fontsize=12.5, color=C["text"], va="top", linespacing=1.45)
        y -= hgt + 0.018
    fig.text(0.94, 0.115, "deslize para ver cada notícia  →", fontsize=12, color=C["gold"], fontweight="bold", ha="right", va="top")
    footer(fig)
    return S.save_clean(fig, "news_0.png")


def news_slide(i, n, h, qs, blurb=None):
    fig = fig_base("", "")
    col = TAG_COL.get(h["tag"], C["muted"])
    fig.text(0.06, 0.94, f"NOTÍCIA {i:02d}/{n:02d}", fontsize=11, color=C["muted"], fontweight="bold", va="top")
    fig.text(0.94, 0.945, f"  {h['tag']}  ", fontsize=13, color=C["bg"], fontweight="bold", ha="right", va="top",
             bbox=dict(boxstyle="round,pad=0.4,rounding_size=0.35", fc=col, ec="none"))
    lines = wrap(h["title"], 26)
    size = 30 if len(lines) <= 5 else 25
    lines = wrap(h["title"], 26 if size == 30 else 31)[:7]
    fig.text(0.06, 0.84, "\n".join(lines), fontsize=size, color=C["text"], fontweight="bold", va="top", linespacing=1.3)
    y = 0.84 - len(lines) * size * 1.3 / 72 / 8 - 0.04
    fig.add_artist(__import__("matplotlib").lines.Line2D([0.06, 0.16], [y, y], color=col, lw=4, transform=fig.transFigure))
    if blurb:
        fig.text(0.06, y - 0.03, "\n".join(wrap(blurb, 52)[:4]), fontsize=14, color=C["muted"], va="top", linespacing=1.5)
    if blurb:
        y -= 0.03 + 0.033 * len(wrap(blurb, 52)[:4])
    if h["asset"] and h["asset"] in qs:
        reaction(fig, qs[h["asset"]], top=min(y - 0.08, 0.62))
    loc = h["dt"].astimezone(TZ)
    footer(fig, f"Fonte: {h['src']} · {loc:%H:%M} {S.TZ_LABEL}")
    return S.save_clean(fig, f"news_{i}.png")


def reaction(fig, q, top):
    """Market panel under a headline: asset, price, day change and a 48h line chart filling the free space."""
    import matplotlib.patches as mp
    bottom = 0.11
    fig.text(0.06, top + 0.035, "COMO O MERCADO REAGIU", fontsize=10, color=C["muted"], fontweight="bold", va="top")
    fig.patches.append(mp.FancyBboxPatch((0.06, bottom), 0.88, top - bottom, transform=fig.transFigure,
                                         boxstyle="round,pad=0,rounding_size=0.012", fc=TILE, ec="none"))
    up = q["chg"] >= 0
    col = C["buy"] if up else C["sell"]
    fig.text(0.085, top - 0.03, q["label"].upper(), fontsize=11, color=C["muted"], fontweight="bold", va="top")
    fig.text(0.085, top - 0.065, fmt(q["price"], q["dec"]), fontsize=24, color=C["text"], fontweight="bold", va="top")
    fig.text(0.915, top - 0.03, f" {'▲' if up else '▼'} {pct(q['chg'])} hoje ", fontsize=12, color=C["bg"], fontweight="bold",
             ha="right", va="top", bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.3", fc=col, ec="none"))
    h = top - bottom - 0.16
    if len(q["spark"]) > 3:
        if h > 0.06:                                       # room under the price: full-width chart
            rect = [0.085, bottom + 0.035, 0.83, h]
        else:                                              # short panel: chart to the right of the price
            rect = [0.42, bottom + 0.035, 0.49, max(top - bottom - 0.10, 0.04)]
        ax = fig.add_axes(rect, zorder=3); ax.axis("off")
        s_ = q["spark"]
        ax.plot(range(len(s_)), s_, color=col, lw=2.2)
        ax.fill_between(range(len(s_)), s_, min(s_), color=col, alpha=0.14, lw=0)
        ax.set_xlim(0, len(s_) - 1)
        fig.text(0.915, bottom + 0.02, "últimas 48h", fontsize=9, color=C["muted"], ha="right", va="bottom")


NEWS_OPEN = ["🌙 Resumo do dia", "🗞 O dia nos mercados", "🌙 Fechamento do dia", "🗞 Resumo da noite"]
NEWS_CLOSE = ["Boa noite e até amanhã.", "Bom descanso e até amanhã às 9h.", "Boa noite a todos. Amanhã tem mais.",
              "Obrigado por acompanhar. Boa noite."]


def slot_news(ch, ctx):
    qs = quotes([k for k, *_ in ASSETS], ctx["basis"])
    seen = ctx["d"].setdefault("news_seen", {})
    heads = pick_news(news(), seen=seen)
    if not heads:
        p = market_card(qs, "Fechamento do dia", "close.png")
        return [(p, f"{pick(NEWS_OPEN)}\n\nNos mercados:\n{movers_text(qs, 4)}\n\n{pick(NEWS_CLOSE)}")]
    blurbs = {}
    raw = ai("Para cada manchete numerada, escreva UMA frase curta (até 140 caracteres) explicando por que ela importa para o "
             "investidor, sem acrescentar fatos que não estejam na manchete. Responda no formato 'N: frase', uma por linha.",
             "\n".join(f"{i + 1}: {h['title']}" for i, h in enumerate(heads)), 900)
    for line in (raw or "").splitlines():
        m = re.match(r"\s*(\d+)\s*[:.)-]\s*(.+)", line)
        if m:
            blurbs[int(m.group(1))] = m.group(2).strip()
    paths = [news_cover(heads, qs)] + [news_slide(i + 1, len(heads), h, qs, blurbs.get(i + 1)) for i, h in enumerate(heads)]
    paths.append(market_card(qs, "Fechamento do dia", "news_close.png"))
    cap = ai("Escreva o resumo das notícias do dia para o canal, em tom respeitoso e sóbrio: uma linha de abertura, 4 ou 5 tópicos "
             "curtos (um por linha, começando com •) com o que mais importou, e uma linha final desejando boa noite. "
             "Use só as manchetes e números fornecidos.",
             "Mercados no dia:\n" + facts_text(qs) + "\n\nManchetes:\n" + "\n".join(f"- {h['title']}" for h in heads), 950)
    if not cap:
        bullets = "\n".join(f"• {h['title']}" for h in heads)
        cap = f"{pick(NEWS_OPEN)}\n\n{bullets}\n\nNos mercados:\n{movers_text(qs, 3)}\n\n{pick(NEWS_CLOSE)}"
        while len(cap) > 1000 and bullets.count("\n"):
            bullets = bullets.rsplit("\n", 1)[0]
            cap = f"{pick(NEWS_OPEN)}\n\n{bullets}\n\nNos mercados:\n{movers_text(qs, 3)}\n\n{pick(NEWS_CLOSE)}"
    def posted():
        for h in heads:
            seen[news_key(h["title"])] = time.time()
        for k in [k for k, t in seen.items() if time.time() - t > 10 * 86400]:
            del seen[k]
    return [{"album": paths, "cap": cap, "after": posted}]


# ================================================================= runner
SLOTS = {9: ("morning", slot_morning), 11: ("overview", slot_overview), 13: ("signal", slot_signal),
         15: ("poll", slot_poll), 19: ("signal", slot_signal), 21: ("news", slot_news)}


def run(hour, live):
    if hour not in SLOTS:
        print(f"  {hour:02d}:00 {S.TZ_LABEL}: no automatic post (17:00 is manual)"); return
    kind, fn = SLOTS[hour]
    print(f"  {hour:02d}:00 {S.TZ_LABEL} · {kind}{'' if live else ' (dry-run)'}")
    d, st = load()
    basis = S.gold_basis(None)
    try:
        for ch in S.load_channels():
            if ch.get("slots") is not None and hour not in ch["slots"]:
                print(f"  {ch['chat']}: slot {hour} not enabled in channels.json"); continue
            S.use_branding(ch)
            cd = d.setdefault(str(ch["chat"]), {})
            ctx = dict(d=cd, live=live, basis=basis, hour=hour)
            try:
                if os.getenv(ch["token_env"]):
                    try:
                        pull_updates(ch, ctx)
                    except Exception as e:                   # a slow getUpdates must not cost the slot its post
                        print(f"  ! could not read bot updates: {e}")
                for it in fn(ch, ctx):
                    if isinstance(it, dict) and "poll" in it:
                        send_poll(ch, it["poll"], ctx); continue
                    if isinstance(it, dict) and "album" in it:
                        j = send_album(ch, it["album"], it["cap"], live, hour)
                        if j.get("ok") and live and it.get("after"):
                            it["after"]()
                        continue
                    path, cap, extra = (tuple(it) + ({},))[:3]
                    j = send_photo(ch, path, cap, live, hour)
                    if j.get("ok") and live:
                        if extra.get("after"):
                            extra["after"]()
                        if extra.get("signal"):
                            extra["signal"]["msg_id"] = j.get("result", {}).get("message_id")
                            cd.setdefault("signals", []).append(extra["signal"])
                            cd["signals"] = [x for x in cd["signals"] if time.time() - x["ts"] < 7 * 86400]
                    time.sleep(1)
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"  ✗ {ch['chat']} {kind} failed: {e}"); S.FAILURES.append(ch["chat"])
        if live and not S.FAILURES:                      # later cron attempts for this slot will skip it
            done = st.setdefault("_done", {})
            done[slots.done_key(hour)] = time.time()
            for k in [k for k, t in done.items() if time.time() - t > 8 * 86400]:
                del done[k]
    finally:
        if live:
            save(st)                                     # keep poll results, offsets and posted flags even after an error


HOOK_START = ("📩", "💬", "🔒")


def scrub(ids, live):
    """Remove the DM line from published posts (message ids). Reads the caption from the channel's public page."""
    for ch in S.load_channels():
        handle = str(ch["chat"]).lstrip("@")
        for mid in ids:
            html = requests.get(f"https://t.me/s/{handle}?before={mid + 1}", headers=S.UA, timeout=30).text
            from bs4 import BeautifulSoup
            el = BeautifulSoup(html, "html.parser").select_one(
                f'.tgme_widget_message[data-post="{handle}/{mid}"] .tgme_widget_message_text')
            if not el:
                print(f"  ! {handle}/{mid}: caption not found"); continue
            for br in el.find_all("br"):
                br.replace_with("\n")
            lines = el.get_text().split("\n")
            kept = [l for l in lines if not l.strip().startswith(HOOK_START)]
            cap = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
            if kept == lines:
                print(f"  {handle}/{mid}: no DM line, unchanged"); continue
            if not live:
                print(f"  [dry-run] {handle}/{mid} new caption:\n{cap}\n"); continue
            j = S.tg(os.getenv(ch["token_env"]), "editMessageCaption", data={"chat_id": ch["chat"], "message_id": mid, "caption": cap})
            print(f"  {handle}/{mid}: {'DM line removed' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
            if not j.get("ok"):
                S.FAILURES.append(ch["chat"])


def report():
    d, _ = load()
    out = []
    for chat, cd in d.items():
        polls = cd.get("polls", [])
        out.append(f"{chat}: {len(polls)} enquetes")
        for p in polls[-30:]:
            out.append(f"  {p['date']}  votantes {p['total']:>4}  vencedor {A[p['winner']]['label'] if p['winner'] else '—':12s} "
                       + "  ".join(f"{A[k]['label']} {v}" for k, v in p["counts"].items()))
        if cd.get("pref"):
            out.append("  preferência acumulada: " + ", ".join(f"{A[k]['label']} {v:.2f}" for k, v in sorted(cd["pref"].items(), key=lambda x: -x[1])))
        sig = cd.get("signals", [])
        out.append(f"  sinais publicados (7 dias): {len(sig)}")
    text = "\n".join(out) or "sem dados ainda"
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write("```\n" + text + "\n```\n")


if __name__ == "__main__":
    a = sys.argv[1:]
    live = "--live" in a
    if a and a[0] == "run":
        run(int(a[1]) if len(a) > 1 and a[1].isdigit() else datetime.now(TZ).hour, live)
    elif a and a[0] == "report":
        report()
    elif a and a[0] == "scrub":                            # python daily.py scrub 19 20 [--live]
        scrub([int(x) for x in a[1:] if x.isdigit()], live)
    else:
        print(__doc__)
    if S.FAILURES:
        print(f"\n  ✗ FAILED for: {sorted(set(S.FAILURES))}")
        sys.exit(1)
