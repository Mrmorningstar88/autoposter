"""
Daily content plan for the channel (Brazil time, Mon–Fri, plus a weekend economy post). Built on the chart engine in scrape.py.

  09:00 morning   good-morning card with the main markets
  11:00 overview  focus asset (yesterday's poll winner, else the biggest mover) + related markets
  13:00 signal    fresh signal from the source channel, else the result of an earlier signal, else an own setup
  15:00 poll      "which asset should I review?" card + Telegram poll; results go to the owner and steer 11:00
  17:00           posted by hand
  19:00 signal    same as 13:00
  21:00 news      market-close card + short news roundup in Portuguese
  Sat/Sun 11:00   econ    US and world economy news carousel (the other weekend posts are manual)

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
from zoneinfo import ZoneInfo

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


def _plain(o):
    """numpy numbers/bools from the chart engine -> plain JSON values (memory must always save)."""
    return o.item() if hasattr(o, "item") else str(o)


def save(st):
    tmp = S.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, default=_plain)
    os.replace(tmp, S.STATE_FILE)                         # never leave a half-written memory file


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
            stale = today < datetime.now(TZ).date()          # market not open yet today: the change is the last session's
            ago = "ontem" if today == datetime.now(TZ).date() - timedelta(days=1) else WEEKDAYS[today.weekday()].lower()
            out[k] = dict(a, price=last, chg=(last / ref - 1) * 100, spark=[float(x) for x in c.iloc[-48:]], stale=stale, ago=ago)
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
         "Português do Brasil, tom respeitoso, confiante e direto, frases curtas. "
         "Nunca prometa lucro nem use exageros; não invente números, preços ou notícias além dos dados fornecidos. "
         "Responda só com o texto do post, sem aspas, sem markdown (sem ** nem #).")


def day_note():
    """Today's weekday and time for the AI, so greetings and sign-offs fit (no 'boa semana' on a Thursday, no
    'bom fim de semana' on Friday morning)."""
    now = datetime.now(TZ)
    wd = now.weekday()
    fit = ("Pode desejar uma boa semana." if wd == 0 else
           "Não deseje 'boa semana' nem 'bom fim de semana' ainda: o dia de mercado continua (pode desejar uma boa sexta-feira)."
           if wd == 4 and now.hour < 18 else
           "Não deseje 'boa semana'; se for o caso, deseje um bom fim de semana." if wd >= 4 else
           "Não deseje 'boa semana' nem 'bom fim de semana'.")
    return (f"Hoje é {WEEKDAYS[wd].lower()}{'' if wd >= 5 else '-feira'}, {now:%d/%m}; agora são {now:%H}h "
            f"(não escreva a hora no post). {fit}")


def ai(task, facts, max_chars, emoji=True):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        r = anthropic.Anthropic().beta.messages.create(
            model=AI_MODEL, max_tokens=4000, system=f"{VOICE} {day_note()} {EMOJI_USE if emoji else 'Não use emojis.'}",
            betas=["server-side-fallback-2026-07-01"],
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


# ---------------------------------------------------------------- formatting: asset icons, bold labels, animated emoji
# emoji.json maps ordinary emoji to animated (custom) ones. Telegram shows the animated emoji only when the bot may use them
# (in channels: a bot with a Fragment username); otherwise it shows the ordinary emoji, so the posts read the same either way.
def _load_emoji():
    """emoji.json, keeping only well-formed parts: a bad edit switches the extras off, it never stops the posts."""
    try:
        e = json.load(open(os.path.join(S.HERE, "emoji.json")))
    except (OSError, ValueError) as x:
        print(f"  ! emoji.json not used: {x}")
        return {}
    if not isinstance(e, dict):
        return {}
    for part in ("assets", "bullet", "symbols", "general"):
        if not isinstance(e.get(part), dict):
            e[part] = {}
    e["assets"] = {k: v for k, v in e["assets"].items() if k in A and isinstance(v, dict)
                   and isinstance(v.get("id"), str) and isinstance(v.get("alt"), str) and v["alt"]}
    for part in ("symbols", "general"):
        e[part] = {a: i for a, i in e[part].items() if isinstance(i, str) and i and a and a[0] > "\u2000"}
    if not (isinstance(e["bullet"].get("id"), str) and isinstance(e["bullet"].get("alt"), str) and e["bullet"]["alt"]):
        e["bullet"] = {}
    return e


EMO = _load_emoji()
ICON_DEFAULT = {"XAUUSD": "🥇", "XAGUSD": "🥈", "EURUSD": "🇪🇺", "GBPUSD": "🇬🇧", "USDJPY": "🇯🇵", "USDBRL": "💵",
                "BTCUSD": "🪙", "WTI": "🛢", "SPX": "🇺🇸", "IBOV": "🇧🇷"}
ICON = {k: (EMO.get("assets", {}).get(k) or {}).get("alt") or v for k, v in ICON_DEFAULT.items()}
BULLET = (EMO.get("bullet") or {}).get("alt") or "•"
UP, DOWN = "📈", "📉"
_ICON_LIST = ", ".join(f"{A[k]['label']} {ICON[k]}" for k in ICON)
TONE = ("☀️ (manhã), 🌙 (noite), 📊 (dados), 🎯 (alvo, objetivo), ⚠️ (risco, alerta), 🔔 (aviso), 🔥 (movimento forte), "
        "💡 (dica), 🚀 (alta forte), 🏦 (bancos centrais), ⚖️ (decisão, equilíbrio), 💼 (empresas), 🐂 (mercado comprador), "
        "🐻 (mercado vendedor), 📌 (ponto importante), 🗓 (agenda), ⌛ (paciência, tempo), 👀 (atenção), 💪 (disciplina), "
        "🙏 (agradecimento)")
EMOJI_USE = (f"Use emojis com significado, como num post profissional: comece cada linha que fala de um ativo com o ícone "
             f"dele ({_ICON_LIST}); use {UP} para alta e {DOWN} para queda; fora isso, no máximo 2 emojis de tom, "
             f"escolhidos desta lista e usados só no sentido indicado: {TONE}. Para listas, comece cada item com •.")
EMOJI_STATS = {"sent": 0, "kept": 0}                 # animated emoji sent in this run / kept by Telegram


def _variants(ch):
    base = ch.replace("\ufe0f", "")
    return sorted({ch, base, base + "\ufe0f"}, key=len, reverse=True)


def visible(text):
    """The caption as readers see it: list bullets become the bullet emoji (when one is configured)."""
    return re.sub(r"(?m)^•\s*", f"{BULLET} ", text) if BULLET != "•" else text


def utf16_cut(text, limit):
    """Telegram counts caption length in UTF-16 units; cut at a line break when possible."""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    while text and len(text.encode("utf-16-le")) // 2 > limit:
        text = text[:-1]
    return text.rsplit("\n", 1)[0].rstrip() if "\n" in text else text


def rich(text, bold=True):
    """Telegram HTML for a visible caption: bold title, bold labels and asset names, animated emoji. Same visible text."""
    import html as H
    named = {}                                       # "🥇 Ouro", "🟢 OURO": asset names right after an icon get bold
    for k in A:
        for l in {A[k]["label"], A[k]["label"].upper(), S.pname(k)}:
            named.setdefault(H.escape(l, quote=False), set()).update({ICON.get(k, ""), "🟢", "🔴", "🎯", "❌"})
    lab = "|".join(re.escape(l) for l in sorted(named, key=len, reverse=True))
    out, first = [], True
    for line in text.split("\n"):
        e = H.escape(line, quote=False)
        if bold and line.strip():
            if first:
                e = f"<b>{e}</b>"
            else:
                e = re.sub(rf"^([^\w\s<&]+?)(\s+)({lab})(?=[\s:,.)]|$)",
                           lambda m: f"{m[1]}{m[2]}<b>{m[3]}</b>" if m[1].replace("\ufe0f", "") in {i.replace("\ufe0f", "") for i in named[m[3]]}
                           else m[0], e, count=1)
                e = re.sub(r"^((?:[^\w\s<&]+\s+)?[^\W\d][\w/&;. ]{0,26}?:)(?!\d)", r"<b>\1</b>", e, count=1) if "<b>" not in e else e
            first = False
        out.append(e)
    html = "\n".join(out)
    return animate(html) if EMO.get("enabled") else html


def _emoji_maps():
    """alt emoji -> id: symbols (fixed meaning) over general (faces, hands); assets: alt -> [(id, asset)] (only on lines
    that name the asset, e.g. the Bitcoin logo)."""
    sym = dict(EMO.get("general") or {})
    sym.update(EMO.get("symbols") or {})
    assets = {}
    for k, v in (EMO.get("assets") or {}).items():
        assets.setdefault(v["alt"], []).append((v["id"], k))
    return sym, assets


def animate(html):
    """Wrap each mapped ordinary emoji in <tg-emoji>, keeping the ordinary one inside exactly as written (what readers see
    when the animated one cannot be shown)."""
    sym, assets = _emoji_maps()
    bullet = EMO.get("bullet") or {}
    alts = set(sym) | set(assets)
    if not alts:
        return html
    canon = {x: a for a in alts for x in _variants(a)}
    pat = re.compile("(?<!\u200d)(?:" + "|".join(re.escape(x) for x in sorted(canon, key=len, reverse=True))
                     + ")(?![\ufe0f\U0001F3FB-\U0001F3FF\u200d])|[\U0001F1E6-\U0001F1FF]{2}")   # flags always in pairs
    tag = lambda eid, alt: f'<tg-emoji emoji-id="{eid}">{alt}</tg-emoji>'

    def line_sub(line):
        def one(m):
            if m.group(0) not in canon:
                return m.group(0)
            alt = canon[m.group(0)]
            named = [i for i, k in assets.get(alt, []) if re.search(re.escape(A[k]["label"]), line, re.I)]
            if len(named) == 1:
                return tag(named[0], m.group(0))
            return tag(sym[alt], m.group(0)) if alt in sym else m.group(0)
        b = re.match(r"(<b>)?" + re.escape(bullet["alt"]) + " ", line) if bullet.get("id") else None
        if b:
            return (b[1] or "") + tag(bullet["id"], bullet["alt"]) + pat.sub(one, line[b.end() - 1:])
        return pat.sub(one, line)
    return "\n".join(line_sub(l) for l in html.split("\n"))


def tg_formatted(ch, method, build, files=None):
    """Send build(True) (HTML formatting); if Telegram rejects the formatting, send build(False), the same text plain.
    Counts the animated emoji sent and the ones Telegram kept (it keeps them only when the bot may use them)."""
    token = os.getenv(ch["token_env"])
    data = build(True)
    j = S.tg(token, method, data=data, **({"files": files} if files is not None else {}))
    if not j.get("ok") and j.get("error_code") == 400:
        print(f"  ! formatting rejected ({j.get('description')}), sending plain text")
        for f in (files or {}).values():
            f.seek(0)
        return S.tg(token, method, data=build(False), **({"files": files} if files is not None else {}))
    n = sum(str(v).count("<tg-emoji") for v in data.values())
    if j.get("ok") and n:
        r = j["result"][0] if isinstance(j["result"], list) and j["result"] else j["result"]
        r = r if isinstance(r, dict) else {}
        poll = r.get("poll") or {}
        ents = (r.get("caption_entities") or r.get("entities") or poll.get("question_entities") or []) + \
            [x for o in poll.get("options", []) for x in o.get("text_entities") or []]
        EMOJI_STATS["sent"] += n
        EMOJI_STATS["kept"] += sum(1 for x in ents if x.get("type") == "custom_emoji")
    return j


# ---------------------------------------------------------------- telegram
_RECENT = {}
SENT_HISTORY = []                                     # this channel's last captions, from memory (longer than the public page)


def remember_sent(cd, cap):
    """Keep the caption for later avoid lists (Rule 1), the last 40 posts."""
    cd["sent_texts"] = (cd.get("sent_texts", []) + [re.sub(r"\s+", " ", cap).strip()[:1000]])[-40:]


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


def fresh(ch, keyword=None, n=3):
    """Rule 1 for AI copy: the latest posts (optionally only those containing keyword), for the AI to avoid. Uses the
    channel's public page plus the captions kept in memory, so it reaches back further than the page's last ~20 posts."""
    try:
        key = lambda t: re.sub(r"\W+", "", t)[:60]
        page = recent_texts(ch)                                   # newest posts, in order
        on_page = {key(t) for t in page}
        pool = [t for t in SENT_HISTORY if key(t) not in on_page] + page   # older captions from memory first
        recent = [t[:400] for t in pool if not keyword or keyword in t][-n:]
    except Exception:
        recent = []
    return ("\nNão repita frases, aberturas nem fechamentos destes posts recentes do canal; escreva com outras palavras:\n"
            + "\n---\n".join(recent)) if recent else ""


def endings(ch, keyword, n=4):
    """The closing sentences of the latest posts containing keyword, so the AI closes with a new idea (Rule 1)."""
    try:
        key = lambda t: re.sub(r"\W+", "", t)[:60]
        page = recent_texts(ch)
        on_page = {key(t) for t in page}
        pool = [t for t in SENT_HISTORY if key(t) not in on_page] + page
        out = []
        for t in [t for t in pool if keyword in t][-n:]:
            parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", t) if len(p.strip()) > 12]
            if parts:
                out.append(" ".join(parts[-2:])[-220:])
    except Exception:
        out = []
    return ("\nEstes foram os fechamentos das últimas análises; termine com uma ideia e palavras diferentes de todas "
            "elas (nada de variações):\n" + "\n".join(f"- {e}" for e in out)) if out else ""


def already_in_channel(ch, cap):
    """True when the caption's opening (first 3 non-empty lines) already appears in a recent post."""
    head = [re.sub(r"\s+", " ", l).strip() for l in cap.splitlines() if l.strip()][:3]
    if len(" ".join(head)) < 25:
        return False
    return any(all(l in t for l in head) for t in recent_texts(ch))


def safe_rich(text, bold=True):
    """rich(), or None when formatting fails for any reason: the post then goes out as plain text."""
    try:
        return rich(text, bold)
    except Exception as e:
        print(f"  ! formatting failed, plain text instead: {e}")
        return None


def caption_data(cap, fmt):
    h = safe_rich(cap) if fmt else None
    return {"caption": h, "parse_mode": "HTML"} if h is not None else {"caption": cap}


def send_photo(ch, path, cap, live, hour):
    cap = utf16_cut(visible(cap + (f"\n\n{ch['footer']}" if ch.get("footer") else "")), 1024)
    if already_in_channel(ch, cap):
        print(f"  · {os.path.basename(path)}: same content is already in the channel, not posted again")
        return {"ok": False, "duplicate": True}
    if not live:
        print(f"  [dry-run] → {ch['chat']} {os.path.basename(path)}\n{cap}\n  html: {safe_rich(cap)}\n")
        return {"ok": True, "result": {"message_id": 0}}
    silent = hour in (ch.get("quiet_hours") or [])
    with open(path, "rb") as f:
        j = tg_formatted(ch, "sendPhoto", lambda fmt: {"chat_id": ch["chat"], "disable_notification": silent, **caption_data(cap, fmt)},
                         files={"photo": f})
    print(f"  → {ch['chat']} {os.path.basename(path)}: {'ok' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
    if not j.get("ok"):
        S.FAILURES.append(ch["chat"])
    return j


def send_album(ch, paths, cap, live, hour):
    cap = utf16_cut(visible(cap + (f"\n\n{ch['footer']}" if ch.get("footer") else "")), 1024)
    if already_in_channel(ch, cap):
        print("  · album: same content is already in the channel, not posted again")
        return {"ok": False, "duplicate": True}
    if not live:
        print(f"  [dry-run] album → {ch['chat']} {[os.path.basename(p) for p in paths]}\n{cap}\n  html: {safe_rich(cap)}\n")
        return {"ok": True}
    files = {f"f{i}": open(p, "rb") for i, p in enumerate(paths[:10])}
    silent = hour in (ch.get("quiet_hours") or [])
    media = lambda fmt: json.dumps([dict(type="photo", media=f"attach://f{i}", **(caption_data(cap, fmt) if i == 0 else {}))
                                    for i in range(len(files))])
    try:
        j = tg_formatted(ch, "sendMediaGroup", lambda fmt: {"chat_id": ch["chat"], "media": media(fmt), "disable_notification": silent},
                         files=files)
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


def notify_owner(ch, text, ctx, to_issue=True):
    print("  [report]\n" + text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("```\n" + text + "\n```\n")
    if not ctx["live"]:
        return
    if to_issue:                                         # poll reports also go to the "Relatório de enquetes" issue
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
    fig.text(x + w - 0.018, y + h - 0.02, f" {'▲' if up else '▼'} {pct(q['chg'])}{' ' + q.get('ago', 'ontem') if q.get('stale') else ''} ", fontsize=10, color=C["bg"], fontweight="bold",
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


def move(q):
    """'📈 +0.12%' / '📉 -0.40%', with 'ontem' when the market has not opened yet today."""
    return f"{UP if q['chg'] >= 0 else DOWN} {pct(q['chg'])}{f" ({q.get('ago', 'ontem')})" if q.get('stale') else ''}"


def movers_text(qs, n=4):
    ks = sorted(qs, key=lambda k: -abs(qs[k]["chg"]))[:n]
    return "\n".join(f"{ICON.get(k, '•')} {qs[k]['label']} {fmt(qs[k]['price'], qs[k]['dec'])} · {move(qs[k])}" for k in ks)


def facts_text(qs):
    return "\n".join(f"{ICON.get(k, '')} {q['label']}: {fmt(q['price'], q['dec'])} ("
                     + (f"{pct(q['chg'])} no pregão de {q.get('ago', 'ontem')}; mercado ainda não abriu hoje" if q.get("stale") else f"{pct(q['chg'])} no dia")
                     + ")" for k, q in qs.items())


# ================================================================= 09:00 morning
MORNING = ["☀️ Bom dia, traders!\n\nDestaques desta manhã:\n{m}\n\nPlano definido, risco controlado. Bons trades! 💪",
           "☀️ Bom dia!\n\nAssim os mercados começam o dia:\n{m}\n\nPaciência para esperar a entrada certa vale mais que pressa. 🎯",
           "🌅 Bom dia, pessoal!\n\nO que se moveu desde ontem:\n{m}\n\nOpere o plano, não a emoção. Bom dia de trades!",
           "☕ Bom dia, traders!\n\nPanorama da manhã:\n{m}\n\nStop sempre definido antes da entrada. Bora pro dia! 💪",
           "☀️ Bom dia!\n\nOs números de abertura:\n{m}\n\nMenos operações, mais qualidade. Excelente dia a todos."]
MORNING_THEMES = ["paciência para esperar o setup certo", "aceitar perdas pequenas como parte do jogo",
                  "não operar por impulso ou por tédio", "seguir o plano de trading definido antes do pregão",
                  "conferir o calendário econômico antes de operar", "registrar cada operação num diário de trading",
                  "proteger o capital acima de tudo", "controlar as emoções depois de um gain ou de um loss",
                  "qualidade acima de quantidade de operações", "respeitar o tamanho da posição"]


# Rule 1: tips already posted per theme (the theme comes back every 10 days), so a theme never gets the same tip again.
# Seeded with what the channel posted before this memory existed; each morning adds its own.
MORNING_TIPS_SEED = {
    "seguir o plano de trading definido antes do pregão": [
        "Defina seu stop antes de entrar, respeite o tamanho da posição e não opere por impulso. Disciplina primeiro, resultado depois.",
        "O plano foi feito com a cabeça fria. Não deixe o calor do pregão reescrever o que você já decidiu."],
    "paciência para esperar o setup certo": [
        "Movimentos moderados até aqui. Bom momento para observar antes de agir.",
        "O melhor trade da semana pode ser aquele que você não abriu. Deixe o preço chegar até o seu nível, não corra atrás dele."],
    "controlar as emoções depois de um gain ou de um loss": [
        "O mercado não lembra do seu último trade. Depois de um gain, nada de euforia. Depois de um loss, nada de vingança."],
    "conferir o calendário econômico antes de operar": [
        "Antes do primeiro clique, abra o calendário econômico. Um dado fora do radar pode virar o gráfico em segundos."],
    "aceitar perdas pequenas como parte do jogo": [
        "Um stop curto é o preço da entrada, não um sinal de fracasso. Quem paga pouco para errar continua na mesa para acertar.",
        "Stop pequeno é custo de operar, não sinal de fracasso. Aceite a perda curta e siga o plano."],
    "não operar por impulso ou por tédio": [
        "O mercado não precisa da sua presença o tempo todo. Tédio não é sinal. Impulso não é estratégia. Se não há setup, não há operação."],
    "respeitar o tamanho da posição": [
        "O tamanho da posição decide se um erro vira lição ou vira prejuízo sério. Escolha o lote antes da entrada, nunca depois."],
    "proteger o capital acima de tudo": [
        "Capital preservado é o que garante a próxima oportunidade. Lucro pode esperar, a sua conta não pode sumir."],
}


def closing_sentences(text, n=3, limit=300):
    """The last sentences of a caption (where the morning tip sits), for the per-theme memory."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)) if len(p.strip()) > 12]
    return " ".join(parts[-n:])[-limit:]


def slot_morning(ch, ctx):
    d = ctx["d"]
    qs = quotes([k for k, *_ in ASSETS], ctx["basis"])
    p = market_card(qs, "Bom dia, traders", "morning.png")
    theme = pick(MORNING_THEMES)
    used = d.get("morning_tips", {}).get(theme) or MORNING_TIPS_SEED.get(theme, [])
    before = ("\nFrases já publicadas no canal sobre esse tema; traga outra ideia, outra imagem e outras palavras, nada "
              "parecido com elas:\n" + "\n".join(f"- {t}" for t in used[-3:])) if used else ""
    cap = ai("Escreva um post curto de bom dia para o canal: saudação, 2-3 destaques do mercado nesta manhã com os números, "
             f"e uma frase curta e original sobre {theme} (sem escrever rótulos como 'tema do dia')."
             + before + fresh(ch, "Bom dia"),
             facts_text(qs), 700)
    if cap and ctx["live"]:
        d.setdefault("morning_tips", {})[theme] = (list(used) + [closing_sentences(cap)])[-3:]
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
             *[f"  {ICON.get(k, '')} {A[k]['label']}: {v} ({v / total:.0%})" if total else f"  {A[k]['label']}: 0"
               for k, v in sorted(counts.items(), key=lambda x: -x[1])],
             f"Últimas enquetes (votantes): {trend}",
             "Preferência acumulada: " + ", ".join(f"{A[k]['label']} {v:.2f}" for k, v in top),
             adjust]
    safe_report(ch, ctx, "\n".join(lines))
    return rec


def safe_report(ch, ctx, text, to_issue=True):
    try:
        notify_owner(ch, text, ctx, to_issue)
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


POLL_Q = ["🎯 Qual mercado merece a análise de amanhã às 11h?",            # used in turn, one per poll (rule 1)
          "👀 De olho em qual ativo? O mais votado ganha análise amanhã às 11h.",
          "📊 Vote no ativo que eu devo analisar amanhã às 11h.",
          "💡 Qual ativo você quer entender melhor? Análise do mais votado amanhã às 11h.",
          "🔍 Me diga: qual ativo eu destrincho amanhã às 11h?",
          "📈 Qual desses ativos você está acompanhando? O vencedor ganha análise amanhã às 11h.",
          "🎯 Escolha o ativo da análise de amanhã, às 11h.",
          "👀 Enquete do dia: qual gráfico você quer ver amanhã às 11h?",
          "📊 Qual ativo você quer que eu analise amanhã às 11h?",
          "🗳 Você escolhe: qual ativo ganha análise completa amanhã às 11h?",
          "📈 Amanhã às 11h eu analiso o mais votado. Qual vai ser?",
          "🔍 Qual gráfico você quer ver destrinchado amanhã às 11h?"]


def slot_poll(ch, ctx):
    d = ctx["d"]
    close_poll(ch, ctx, late=True)
    i = d.get("poll_q_i", 0) % len(POLL_Q)                  # next question in turn: no repeat for 12 polls
    q = POLL_Q[i]
    if datetime.now(TZ).weekday() == 4:                      # Friday: the next 11:00 run is on Monday
        q = q.replace("de amanhã", "de segunda-feira").replace("amanhã", "na segunda-feira").replace("Amanhã", "Na segunda-feira")
    return [{"poll": dict(question=q, keys=poll_options(d), q_next=i + 1)}]


def poll_text(question, opts, fmt):
    """Poll question and options, with animated emoji when fmt (polls take no bold); plain if formatting fails."""
    q, o = (safe_rich(question, bold=False), [safe_rich(x, bold=False) for x in opts]) if fmt else (None, [None])
    if q is None or None in o:
        return {"question": question, "options": json.dumps([{"text": x} for x in opts])}
    return {"question": q, "question_parse_mode": "HTML",
            "options": json.dumps([{"text": x, "text_parse_mode": "HTML"} for x in o])}


def send_poll(ch, spec, ctx):
    if not ctx["live"]:
        print(f"  [dry-run] poll → {ch['chat']}: {spec['question']} {[ICON.get(k, '') + ' ' + A[k]['label'] for k in spec['keys']]}\n")
        return
    opts = [f"{ICON.get(k, '')} {A[k]['label']}".strip() for k in spec["keys"]]
    j = tg_formatted(ch, "sendPoll", lambda fmt: {          # polls allow animated emoji but no bold
        "chat_id": ch["chat"], "is_anonymous": True, "disable_notification": ctx["hour"] in (ch.get("quiet_hours") or []),
        **poll_text(spec["question"], opts, fmt)})
    print(f"  → {ch['chat']} poll: {'ok' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
    ctx["sent_poll"] = j
    if j.get("ok"):
        if "q_next" in spec:
            ctx["d"]["poll_q_i"] = spec["q_next"]
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
    n = voted["counts"][focus] if voted else 0
    intro = f"🗳 Vocês escolheram: {A[focus]['label'].upper()} ({n} {'voto' if n == 1 else 'votos'}). Aqui está a análise.\n\n" if voted else ""
    cap = ai(f"Escreva uma visão geral curta de {A[focus]['label']} para hoje: tendência, níveis principais (suporte/resistência) "
             f"e como os ativos relacionados se moveram. Termine com o que observar no dia. Não abra com a fórmula "
             f"'<ativo> ... nesta <dia da semana>' (já usada várias vezes); comece por outro ângulo."
             + (fresh(ch, "Resist", n=4) or fresh(ch)) + endings(ch, "Resist"), facts, 800 - len(intro))
    cap = intro + (cap or template_overview(focus, facts))
    return [(p, cap, {"after": posted})]


def template_overview(focus, f):
    return (f"{ICON.get(focus, '🔍')} {A[focus]['label'].upper()} · visão do dia\n\n{f['trend_line']}\n"
            f"Resistência: {f['res']} · Suporte: {f['sup']}\n\n📊 Relacionados:\n{f['rel']}\n\n⚠️ Não é recomendação de investimento.")


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
                 rel="\n".join(f"{ICON.get(k, '•')} {qs[k]['label']} · {move(qs[k])}" for k in rel))
    facts_str = (f"Ativo: {ICON.get(focus, '')} {a['label']}, preço {fmt(last, dec)}, {pct(q['chg'])} hoje. {tl} "
                 f"Resistências: {facts['res']}. Suportes: {facts['sup']}. Relacionados: "
                 + ", ".join(f"{ICON.get(k, '')} {qs[k]['label']} {pct(qs[k]['chg'])}" for k in rel))
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


FX = "USD|EUR|GBP|JPY|CHF|AUD|NZD|CAD"


def instrument_of(t):
    """Known instrument, or any major FX pair written like USD/CHF or EURJPY (added to the chart markets on the fly)."""
    name = S.instrument(t)
    if name:
        return name
    m = re.search(rf"\b({FX})\s*/?\s*({FX})\b", t)
    if not m or m.group(1) == m.group(2):
        return None
    name = m.group(1) + m.group(2)
    if name not in [n for n, *_ in S.TA_MARKETS + S.EXTRA_MARKETS]:
        jpy = "JPY" in name
        S.EXTRA_MARKETS.append((name, f"{name}=X", 0.01 if jpy else 0.0001, 3 if jpy else 5))
        S.NAMES[name] = f"{name[:3]}/{name[3:]}"
    return name


def parse_signal(text):
    """Forex signal text (EN/RU/PT) -> dict(name, side, entry, sl, tp1, tp2, at_market) or None.
    Handles AFX's format: 'SELL XAU/USD · 📍 4182 - 4184 · Stop loss (SL): 4202 · Take profit 1: 4179 …'."""
    t = re.sub(r"(?<=\d),(?=\d)", ".", text.upper()).replace("📍", " ENTRY ")
    for rx, rep in RU:
        t = re.sub(rx, rep, t)
    name = instrument_of(t)
    side = re.search(r"\b(BUY|SELL)\b", t)
    if not name or not side:
        return None
    sl = re.search(r"\bSL\b[^\w\n]{0,6}" + NUM, t)
    tps = [float(x) for x in re.findall(r"\bTP(?:\s?\d(?![\d.]))?[^\w\n]{0,6}" + NUM, t)]
    rng = NUM + r"(?:\s*[-–/]\s*" + NUM + ")?"
    m = re.search(r"\bENTRY\b[^\w\n]{0,6}" + rng, t)
    if not m:                                             # numbers between the side and the first SL/TP, instrument removed
        stop = re.search(r"\b(SL|TP)\b", t[side.end():])
        seg = t[side.end(): side.end() + stop.start()] if stop else t[side.end(): side.end() + 60]
        seg = re.sub(rf"\b({FX})\s*/?\s*({FX})\b|\b(US30|NAS100|US100|XAU\s*/?\s*USD|XAG\s*/?\s*USD|BTC\w*)\b", " ", seg)
        m = re.search(rng, seg)
    if not (sl and tps and m):
        return None
    ent = [float(x) for x in m.groups() if x]
    entry, s_, sd = sum(ent) / len(ent), float(sl.group(1)), side.group(1)
    tp1 = tps[0]
    tp2 = tps[-1] if len(tps) > 1 else tp1 + (tp1 - entry)    # card shows the first and the final target
    ok = (s_ < entry < tp1 <= tp2) if sd == "BUY" else (s_ > entry > tp1 >= tp2)
    if not ok:
        return None
    return dict(name=name, side=sd, entry=entry, sl=s_, tp1=tp1, tp2=tp2, at_market="LIMIT" not in t)


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
            if not sig and (post.get("text") or post.get("caption")):
                d.setdefault("src_raw", []).append(dict(id=post["message_id"], ts=post["date"], text=post.get("text") or post.get("caption")))
            if not sig:                                   # private run log only: lets the parser be fixed for new formats
                raw = (post.get("text") or post.get("caption") or "[no text: " + ",".join(k for k in post if k in ("photo", "video", "document", "poll")) + "]")
                print("    raw: " + raw[:400].replace("\n", " ⏎ "))
            if sig:
                d.setdefault("src", []).append(dict(sig, ts=post["date"], id=post["message_id"], posted=False))
        if len(j["result"]) < 100 or not ctx["live"]:   # a dry run never pages: a higher offset deletes updates
            break
    for r in list(d.get("src_raw", [])):                   # re-read posts an older parser did not understand
        sig = parse_signal(r["text"])
        if sig:
            print(f"  · source post {r['id']} now recognised: {sig['name']} {sig['side']}")
            d.setdefault("src", []).append(dict(sig, ts=r["ts"], id=r["id"], posted=False))
            d["src_raw"].remove(r)
    d["src_raw"] = [r for r in d.get("src_raw", []) if time.time() - r["ts"] < 86400][-30:]
    d["src"] = [s for s in d.get("src", []) if time.time() - s["ts"] < 3 * 86400]


def tf_for(name):
    return "15m" if name in ("XAUUSD", "XAGUSD", "BTCUSD", "US30", "NAS100") else "1h"


NY = ZoneInfo("America/New_York")


def market_open(now=None):
    """Forex and gold trade from Sunday 17:00 to Friday 17:00 New York time. A new entry posted while they are closed
    (the Friday 19:00 slot) could not be taken before Monday's gap, so then no new setup is posted."""
    ny = (now or datetime.now(TZ)).astimezone(NY)
    return not (ny.weekday() == 5 or (ny.weekday() == 4 and ny.hour >= 17) or (ny.weekday() == 6 and ny.hour < 17))


def slot_signal(ch, ctx):
    d = ctx["d"]
    now = time.time()
    is_open = market_open()
    if not is_open:
        print("  market closed for the weekend: no new entries, only a result or a panorama")
    # 1. a fresh signal from the source channel, posted inside Brazilian daytime
    for s in sorted(d.get("src", []) if is_open else [], key=lambda s: -s["ts"]):
        brt = datetime.fromtimestamp(s["ts"], TZ)
        if s["posted"] or now - s["ts"] > FRESH_MIN * 60 or not 7 <= brt.hour < 23:
            continue
        if ch["markets"] is not None and s["name"] not in ch["markets"] and s["name"] not in [m for m, *_ in S.EXTRA_MARKETS]:
            continue
        item = signal_post(ch, ctx, s, source=True)
        if item and seen_setup(d, s["name"], item[2]["signal"], ctx):
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
    # 3. own setup from the chart engine, never on a market that still has an open setup from the last day
    pref = d.get("pref", {})
    busy = {x["name"] for x in d.get("signals", []) if not x.get("followed") and now - x["ts"] < 86400}
    if busy:
        print(f"  · open setups, no new one on: {', '.join(sorted(busy))}")
    for name in sorted([m for m in (ch["markets"] or ["XAUUSD", "EURUSD", "GBPUSD"]) if m not in busy] if is_open else [],
                       key=lambda m: -pref.get(m, 0)):
        for tf in ("15m", "1h") if name == "XAUUSD" else ("1h",):
            p, c, res = S.render_entry_chart(name, tf, 70, ctx["basis"] if name == "XAUUSD" else 0.0)
            if res["plan"] and not seen_setup(d, name, res["plan"], ctx):
                return [finish_signal(ch, ctx, name, tf, res["plan"], p, c, "own")]
    # 4. never leave the slot empty: a market panorama of an asset not covered yet today
    print("  no fresh signal, no result to show and no new setup: posting a market panorama")
    return [panorama_item(ch, ctx)]


OLD_GOLD_BASIS = 28.0                                     # the fixed gold offset used before it was measured


def sig_basis(sig, ctx):
    """The gold futures-spot offset a stored signal's levels belong to. Our own setups were drawn on futures minus the
    offset of that day; source-channel levels are real spot, so they take today's offset. 0 for other markets."""
    if sig["name"] != "XAUUSD":
        return 0.0
    return sig.get("basis", OLD_GOLD_BASIS) if sig.get("origin") == "own" else ctx["basis"]


def seen_setup(d, name, plan, ctx, hours=24):
    """Rule 1: same instrument and side with an entry within 0.15% of a signal posted in the last day is a repeat.
    Gold entries are compared on today's offset, so a change in the offset never hides a repeat."""
    now_basis = ctx["basis"] if name == "XAUUSD" else 0.0
    for x in d.get("signals", []):
        if (x["name"] == name and x["side"] == plan["side"] and time.time() - x["ts"] < hours * 3600
                and abs(x["entry"] + sig_basis(x, ctx) - now_basis - plan["entry"]) / plan["entry"] < 0.0015):
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
             f"nas próximas horas. Comece dizendo que não há entrada limpa neste momento." + fresh(ch), facts, 750)
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
               tp1=plan["tp1"], tp2=plan["tp2"], at_market=bool(plan["at_market"]), ts=time.time(), origin=origin, followed=False,
               basis=ctx["basis"] if name == "XAUUSD" else 0.0)
    return (p, cap, {"signal": rec})


def outcome(sig, basis):
    """Walk the candles since the post: (status, candles). status: open | TP1 | TP2 | SL | BE | expired.
    1-minute candles when they reach back to the post (Yahoo keeps ~5 days), else 15-minute ones. Each candle's range
    includes the previous close, so a price crossed between two quotes or jumped over in a gap still counts. Inside one
    candle the order of the moves is unknown, so on the candle where a pending entry fills only the stop counts (a target
    touched in it may have come before the fill)."""
    tk = next(t for n, t, *_ in S.TA_MARKETS + S.EXTRA_MARKETS if n == sig["name"])
    start = datetime.fromtimestamp(int(sig["ts"]), TZ)
    df = None
    try:
        m = S.load_tf(tk, "1m")
        if len(m) and m.index[0] <= start:
            df = m
    except Exception as e:
        print(f"  · no 1-minute data for {sig['name']} ({e}): judging on 15-minute candles")
    df = (S.load_tf(tk, "15m") if df is None else df) - basis
    df = df[df.index >= start]
    up = sig["side"] == "BUY"
    live, tp1, prev = sig["at_market"], False, None
    for hi, lo, close in zip(df["High"], df["Low"], df["Close"]):
        if prev is not None:                   # the move from the last close counts too: Yahoo's 1-minute forex
            hi, lo = max(hi, prev), min(lo, prev)  # candles are single quotes, and a gap jumps over the price
        prev = close
        if not live:
            live = lo <= sig["entry"] <= hi
            if live and ((lo <= sig["sl"]) if up else (hi >= sig["sl"])):
                return "SL", df
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
        basis = sig_basis(sig, ctx)
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
        try:
            p, cap = result_card(ch, sig, st_, basis)
        except Exception as e:                             # a broken result card must not cost the slot its post
            print(f"  ! result card failed for {sig['key']}: {e}"); continue
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
    start = datetime.fromtimestamp(int(sig["ts"]), TZ)    # whole seconds: candle times have no sub-second unit
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
    icon = "🎯" if status != "SL" else "❌"
    cap = f"{icon} {ICON.get(name, '')} {S.pname(name)} · {label}\n\n{S.side_word(sig['side'])} em {fmt(sig['entry'], dec)} → {ptxt}\n{msg}"
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


def news(hours=14, feeds=None, topics=None):
    items, seen = [], set()
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    topics = topics or TOPICS
    for url in feeds or NEWS_FEEDS:
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
                tag = next(((t, a) for t, rx, a in topics if re.search(rx, title.upper())), None)
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


def news_cover(heads, qs, title="Resumo do dia", sub="o que mexeu com os mercados"):
    now = datetime.now(TZ)
    fig = fig_base(title, f"{WEEKDAYS[now.weekday()]}, {S.stamp()} {S.TZ_LABEL}  ·  {sub}")
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
    when = "hoje" if not q.get("stale") else (q.get("ago", "ontem") if q.get("ago", "ontem") == "ontem" else f"na {q['ago']}")
    fig.text(0.915, top - 0.03, f" {'▲' if up else '▼'} {pct(q['chg'])} {when} ", fontsize=12, color=C["bg"], fontweight="bold",
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
NEWS_CLOSE_FRI = ["Bom fim de semana a todos. Sábado às 11h tem o resumo da economia.",
                  "Ótimo fim de semana. Amanhã às 11h, a economia dos EUA e do mundo.",
                  "Boa noite e bom fim de semana. Sábado e domingo às 11h tem resumo da economia."]
NEWS_CLOSE = ["Boa noite e até amanhã.", "Bom descanso e até amanhã às 9h.", "Boa noite a todos. Amanhã tem mais.",
              "Obrigado por acompanhar. Boa noite."]


def news_close():
    """Closing line; on Friday it wishes a good weekend and points to the weekend economy post."""
    return pick(NEWS_CLOSE_FRI if datetime.now(TZ).weekday() == 4 else NEWS_CLOSE)


def slot_news(ch, ctx):
    qs = quotes([k for k, *_ in ASSETS], ctx["basis"])
    seen = ctx["d"].setdefault("news_seen", {})
    heads = pick_news(news(), seen=seen)
    if not heads:
        p = market_card(qs, "Fechamento do dia", "close.png")
        return [(p, f"{pick(NEWS_OPEN)}\n\nNos mercados:\n{movers_text(qs, 4)}\n\n{news_close()}")]
    blurbs = {}
    raw = ai("Para cada manchete numerada, escreva UMA frase curta (até 140 caracteres) explicando por que ela importa para o "
             "investidor, sem acrescentar fatos que não estejam na manchete. Responda no formato 'N: frase', uma por linha.",
             "\n".join(f"{i + 1}: {h['title']}" for i, h in enumerate(heads)), 900, emoji=False)
    for line in (raw or "").splitlines():
        m = re.match(r"\W*(\d+)\s*[:.)-]\s*(.+)", line)
        if m:                                              # drawn on the slide: the slide font has no emoji
            blurbs[int(m.group(1))] = re.sub(r"\s+", " ", re.sub(r"[\U0001F000-\U0001FAFF\u2190-\u2BFF\ufe0f\u200d]", "", m.group(2))).strip()
    paths = [news_cover(heads, qs)] + [news_slide(i + 1, len(heads), h, qs, blurbs.get(i + 1)) for i, h in enumerate(heads)]
    paths.append(market_card(qs, "Fechamento do dia", "news_close.png"))
    cap = ai("Escreva o resumo das notícias do dia para o canal, em tom respeitoso e sóbrio: uma linha de abertura, 4 ou 5 tópicos "
             "curtos (um por linha, começando com •) com o que mais importou, e uma linha final desejando boa noite"
             + (" e bom fim de semana, avisando que sábado e domingo às 11h tem o resumo da economia dos EUA e do mundo"
                if datetime.now(TZ).weekday() == 4 else "") + ". "
             "Use só as manchetes e números fornecidos." + fresh(ch),
             "Mercados no dia:\n" + facts_text(qs) + "\n\nManchetes:\n" + "\n".join(f"- {h['title']}" for h in heads), 950)
    if not cap:
        bullets = "\n".join(f"• {h['title']}" for h in heads)
        cap = f"{pick(NEWS_OPEN)}\n\n{bullets}\n\nNos mercados:\n{movers_text(qs, 3)}\n\n{news_close()}"
        while len(cap) > 1000 and bullets.count("\n"):
            bullets = bullets.rsplit("\n", 1)[0]
            cap = f"{pick(NEWS_OPEN)}\n\n{bullets}\n\nNos mercados:\n{movers_text(qs, 3)}\n\n{news_close()}"
    def posted():
        for h in heads:
            seen[news_key(h["title"])] = time.time()
        for k in [k for k, t in seen.items() if time.time() - t > 10 * 86400]:
            del seen[k]
    return [{"album": paths, "cap": cap, "after": posted}]


# ================================================================= weekend 11:00: US and world economy (carousel)
ECON_FEEDS = ["https://news.google.com/rss/search?q=economia+(EUA+OR+%22Estados+Unidos%22+OR+Fed+OR+%22Wall+Street%22+OR+Treasury)"
              "+when:2d&hl=pt-BR&gl=BR&ceid=BR:pt-419",
              "https://news.google.com/rss/search?q=(%22economia+global%22+OR+%22economia+mundial%22+OR+China+OR+BCE+OR+FMI+OR+tarifas"
              "+OR+petr%C3%B3leo)+economia+when:2d&hl=pt-BR&gl=BR&ceid=BR:pt-419",
              "https://br.investing.com/rss/news_1.rss"]
ECON_TOPICS = [  # US and world only: Brazil-only stories (Copom, Selic, Ibovespa) are left out
    ("EUA", r"\bFED\b|WALL STREET|S&P|NASDAQ|DOW JONES|TREASUR|PAYROLL|EUA\b|ESTADOS UNIDOS|AMERICAN|POWELL|TRUMP|CASA BRANCA", "SPX"),
    ("CHINA", r"CHINA|CHIN[ÊE]S|PEQUIM|XI JINPING", None), ("EUROPA", r"EUROPA|EUROPE|\bBCE\b|ZONA DO EURO|\bEURO\b|ALEMANHA", "EURUSD"),
    ("JAPÃO", r"JAP[ÃA]O|\bBOJ\b|IENE", "USDJPY"), ("PETRÓLEO", r"PETR[ÓO]LEO|OPEP|BRENT", "WTI"),
    ("OURO", r"\bOURO\b", "XAUUSD"), ("CRIPTO", r"BITCOIN|CRIPTO", "BTCUSD"),
    ("MUNDO", r"\bFMI\b|BANCO MUNDIAL|ECONOMIA (GLOBAL|MUNDIAL)|TARIFA|COM[ÉE]RCIO (GLOBAL|MUNDIAL|EXTERIOR)|G20|G7\b", None)]
ECON_DOMESTIC = r"IBOVESPA|\bB3\b|R\$|COPOM|SELIC|IPCA|HADDAD|TESOURO DIRETO"   # Brazil-only market stories
TAG_COL.update({"CHINA": "#F0506E", "EUROPA": "#5B8CFF", "JAPÃO": "#F0506E", "MUNDO": "#B07CF0"})
ECON_OPEN = ["🌎 Economia dos EUA e do mundo", "🌎 O que movimenta a economia global", "🇺🇸 Economia: EUA e mundo"]
ECON_CLOSE = ["Bom fim de semana a todos.", "Ótimo fim de semana. Segunda às 9h voltamos com os mercados.",
              "Bom descanso e até segunda."]


def slot_econ(ch, ctx):
    """Weekend 11:00: the main US and world economy stories of the last two days. Stories already used in a
    carousel are skipped (rule 1); market panels show the last session's change (crypto trades at the weekend)."""
    qs = quotes([k for k, *_ in ASSETS], ctx["basis"])
    seen = ctx["d"].setdefault("news_seen", {})
    items = [h for h in news(48, ECON_FEEDS, ECON_TOPICS) if h["tag"] and not re.search(ECON_DOMESTIC, h["title"].upper())]
    heads = pick_news(items, seen=seen)
    if not heads:
        print("  no new US/world economy stories: nothing to post"); return []
    blurbs = {}
    raw = ai("Para cada manchete numerada, escreva UMA frase curta (até 140 caracteres) explicando o que ela significa para a "
             "economia e os mercados, sem acrescentar fatos que não estejam na manchete. Responda no formato 'N: frase', uma por linha.",
             "\n".join(f"{i + 1}: {h['title']}" for i, h in enumerate(heads)), 900, emoji=False)
    for line in (raw or "").splitlines():
        m = re.match(r"\W*(\d+)\s*[:.)-]\s*(.+)", line)
        if m:
            blurbs[int(m.group(1))] = re.sub(r"\s+", " ", re.sub(r"[\U0001F000-\U0001FAFF\u2190-\u2BFF\ufe0f\u200d]", "", m.group(2))).strip()
    paths = [news_cover(heads, qs, "Economia: EUA e mundo", "o que movimenta a economia global")]
    paths += [news_slide(i + 1, len(heads), h, qs, blurbs.get(i + 1)) for i, h in enumerate(heads)]
    cap = ai("Escreva o resumo de fim de semana da economia dos EUA e do mundo para o canal, em tom respeitoso e sóbrio: uma linha "
             "de abertura, 4 ou 5 tópicos curtos (um por linha, começando com •) com o que mais importa, e uma linha final desejando "
             "bom fim de semana. Use só as manchetes fornecidas." + fresh(ch),
             "Manchetes:\n" + "\n".join(f"- {h['title']}" for h in heads), 950)
    if not cap:
        bullets = "\n".join(f"• {h['title']}" for h in heads)
        cap = f"{pick(ECON_OPEN)}\n\n{bullets}\n\n{pick(ECON_CLOSE)}"
        while len(cap) > 1000 and bullets.count("\n"):
            bullets = bullets.rsplit("\n", 1)[0]
            cap = f"{pick(ECON_OPEN)}\n\n{bullets}\n\n{pick(ECON_CLOSE)}"
    def posted():
        for h in heads:
            seen[news_key(h["title"])] = time.time()
    return [{"album": paths, "cap": cap, "after": posted}]


# ================================================================= runner
SLOTS = {9: ("morning", slot_morning), 11: ("overview", slot_overview), 13: ("signal", slot_signal),
         15: ("poll", slot_poll), 19: ("signal", slot_signal), 21: ("news", slot_news)}
WEEKEND_SLOTS = {11: ("econ", slot_econ)}             # Sat/Sun; the rest of the weekend is posted by hand


def slots_today(now=None):
    return WEEKEND_SLOTS if slots.weekend(now or datetime.now(TZ)) else SLOTS


def run(hour, live):
    table = slots_today()
    if hour not in table:
        print(f"  {hour:02d}:00 {S.TZ_LABEL}: no automatic post today (posted by hand)"); return
    kind, fn = table[hour]
    print(f"  {hour:02d}:00 {S.TZ_LABEL} · {kind}{'' if live else ' (dry-run)'}")
    d, st = load()
    sample = S.spot_now()                                # spot gold now: later runs set it against the futures
    st["_gold_spot"] = [x for x in st.get("_gold_spot", []) if time.time() - x[0] < 5 * 86400][-40:] + ([list(sample)] if sample else [])
    basis = S.gold_basis(None, samples=st["_gold_spot"])
    if S.BASIS_SRC.startswith("spot"):
        st["_gold_basis"] = dict(b=basis, t=time.time())
    print(f"  gold futures-spot offset: {basis:.2f} ({S.BASIS_SRC})")
    try:
        for ch in S.load_channels():
            enabled = ch.get("weekend_slots" if table is WEEKEND_SLOTS else "slots")
            if enabled is not None and hour not in enabled:
                print(f"  {ch['chat']}: slot {hour} not enabled in channels.json"); continue
            S.use_branding(ch)
            cd = d.setdefault(str(ch["chat"]), {})
            SENT_HISTORY[:] = cd.get("sent_texts", [])
            ctx = dict(d=cd, live=live, basis=basis, hour=hour)
            sent, failed_before = [], len(S.FAILURES)
            EMOJI_STATS.update(sent=0, kept=0)
            try:
                if os.getenv(ch["token_env"]):
                    try:
                        pull_updates(ch, ctx)
                    except Exception as e:                   # a slow getUpdates must not cost the slot its post
                        print(f"  ! could not read bot updates: {e}")
                for it in fn(ch, ctx):
                    if isinstance(it, dict) and "poll" in it:
                        send_poll(ch, it["poll"], ctx)
                        j = ctx.get("sent_poll") or {}
                        if j.get("ok"):
                            sent.append(dict(what="Poll: " + it["poll"]["question"], id=first_id(j)))
                        continue
                    if isinstance(it, dict) and "album" in it:
                        j = send_album(ch, it["album"], it["cap"], live, hour)
                        if j.get("ok") and live:
                            remember_sent(cd, it["cap"])
                        if j.get("ok") and live and it.get("after"):
                            it["after"]()
                        if j.get("ok") and live:
                            sent.append(dict(what=f"News carousel ({len(it['album'])} slides)", id=first_id(j)))
                        continue
                    path, cap, extra = (tuple(it) + ({},))[:3]
                    j = send_photo(ch, path, cap, live, hour)
                    if j.get("ok") and live:
                        remember_sent(cd, cap)
                        sent.append(dict(what=next((l for l in cap.splitlines() if l.strip()), os.path.basename(path))[:90],
                                         id=first_id(j)))
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
            if live and EMOJI_STATS["sent"]:                # did Telegram keep the animated emoji? (owner's daily report)
                cd["emoji_status"] = dict(t=time.time(), kept=EMOJI_STATS["kept"] > 0)
                print(f"  animated emoji: Telegram kept {EMOJI_STATS['kept']} of {EMOJI_STATS['sent']}")
            if live:
                day = cd.setdefault("log", {}).setdefault(f"{datetime.now(TZ):%Y-%m-%d}", {})
                day[str(hour)] = dict(kind=kind, sent=sent, failed=len(S.FAILURES) > failed_before, t=time.time())
                for k in [k for k in cd["log"] if k < (datetime.now(TZ) - timedelta(days=10)).strftime("%Y-%m-%d")]:
                    del cd["log"][k]
                if table is SLOTS and hour == max(slots.SLOT_HOURS):   # after the last weekday slot: the owner's end-of-day report
                    safe_report(ch, ctx, day_report(ch, cd), to_issue=False)
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
            kept = [l for l in lines if not (l.strip().startswith(HOOK_START) and re.search(r"@\w{4,}", l))]
            cap = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
            if kept == lines:
                print(f"  {handle}/{mid}: no DM line, unchanged"); continue
            if not live:
                print(f"  [dry-run] {handle}/{mid} new caption:\n{cap}\n"); continue
            j = tg_formatted(ch, "editMessageCaption", lambda fmt: {"chat_id": ch["chat"], "message_id": mid, **caption_data(cap, fmt)})
            print(f"  {handle}/{mid}: {'DM line removed' if j.get('ok') else 'FAILED ' + str(j.get('description'))}")
            if not j.get("ok"):
                S.FAILURES.append(ch["chat"])


def first_id(j):
    """Message id from a Telegram answer (one message or an album); never raises: bookkeeping must not fail a post."""
    try:
        r = j.get("result")
        r = r[0] if isinstance(r, list) and r else r
        return r.get("message_id") if isinstance(r, dict) else None
    except Exception:
        return None


SLOT_NAMES = {9: "Good morning + prices", 11: "Asset analysis", 13: "Signal", 15: "Poll", 19: "Signal", 21: "News carousel"}


def day_report(ch, cd, date=None):
    """End-of-day summary for the owner (English): every slot, what went out with links, what failed or was missed."""
    date = date or f"{datetime.now(TZ):%Y-%m-%d}"
    day = cd.get("log", {}).get(date, {})
    handle = str(ch["chat"]).lstrip("@")
    link = lambda mid: f" https://t.me/{handle}/{mid}" if mid and str(ch["chat"]).startswith("@") else ""
    lines = [f"📋 Daily report · {ch['chat']} · {datetime.strptime(date, '%Y-%m-%d'):%a %d %b}"]
    missing = 0
    for h in (9, 11, 13, 15, 17, 19, 21):
        if h == 17:
            lines.append("17:00 ✍️ your manual post"); continue
        e = day.get(str(h))
        if not e:
            missing += 1
            lines.append(f"{h:02d}:00 ⚠️ {SLOT_NAMES[h]}: not posted (no run reached this slot)")
        elif e["failed"] and not e["sent"]:
            missing += 1
            lines.append(f"{h:02d}:00 ❌ {SLOT_NAMES[h]}: failed")
        elif not e["sent"]:
            lines.append(f"{h:02d}:00 ➖ {SLOT_NAMES[h]}: nothing new to post")
        else:
            for x in e["sent"]:
                lines.append(f"{h:02d}:00 ✅ {x['what']}{link(x.get('id'))}")
    polls = cd.get("polls", [])
    if polls:
        p = polls[-1]
        top = sorted(p["counts"].items(), key=lambda x: -x[1])
        lines.append(f"Last closed poll ({p['date']}): {p['total']} voters" + (f", winner {A[top[0][0]]['label']}" if p["winner"] else ""))
    src = [x for x in cd.get("src", []) if datetime.fromtimestamp(x["ts"], TZ).strftime("%Y-%m-%d") == date]
    seen = cd.get("source_seen")
    lines.append(f"AFX signals read today: {len(src)}, forwarded: {sum(1 for x in src if x.get('posted'))}"
                 + ("" if seen else " (no AFX post seen yet)"))
    es = cd.get("emoji_status")
    if es:
        lines.append("Animated emoji: showing ✨" if es["kept"] else
                     "Animated emoji: Telegram showed the regular emoji (channel posts need a Fragment username on the bot)")
    lines.append("All slots posted." if not missing else f"{missing} slot(s) missed or failed: see the alert issue on GitHub.")
    return "\n".join(lines)


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
