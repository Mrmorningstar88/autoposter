// AFX relay on Cloudflare Workers (cron every minute). Same job as `python daily.py relay --live` on GitHub Actions:
// every new signal of the source channel goes to each relay channel as a chart card (PT or ES, no signature).
// Bindings: KV namespace STATE; secrets TG_BOT_TOKEN (PT bot) and TELEGRAM_BOT_ES_TOKEN (ES bot).
// The repo is public: never log message texts, chat titles or ids.

const RELAYS = [
  { bot: "TG_BOT_TOKEN", lang: "pt", chat: -1003874360802, source: "afx" },
  { bot: "TELEGRAM_BOT_ES_TOKEN", lang: "es", chat: -1004318189383, source: "afx" },
];
const FRESH_MIN = 120;            // an older source post is never relayed
const FAR = 0.06;                 // entry further than 6% from the market = misread post, never relayed

// ------------------------------------------------------------------ text
const TXT = {
  pt: { GOLD: "OURO", BUY: "COMPRA", SELL: "VENDA", NOW: "AGORA", LIMIT: "LIMITE", chart: "Gráfico", ENTRY: "ENTRADA",
        zone: "ZONA DE", entry: "Entrada", tp1: "Quando o TP1 bater, mova o SL para a entrada.",
        nfa: "⚠️ Não é recomendação de investimento. Arrisque no máximo 1% por operação.",
        reason: (s, tf, w) => `Sinal ${s === "BUY" ? "de compra" : "de venda"} no ${tf}. ${w}`,
        mkt: "Entrada a mercado.", buyw: "Entrada num recuo até a zona marcada.", sellw: "Entrada num repique até a zona marcada.",
        months: ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"], silver: "PRATA" },
  es: { GOLD: "ORO", BUY: "COMPRA", SELL: "VENTA", NOW: "AHORA", LIMIT: "LÍMITE", chart: "Gráfico", ENTRY: "ENTRADA",
        zone: "ZONA DE", entry: "Entrada", tp1: "Cuando toque el TP1, mueve el SL a la entrada.",
        nfa: "⚠️ No es recomendación de inversión. Arriesga como máximo 1% por operación.",
        reason: (s, tf, w) => `Señal ${s === "BUY" ? "de compra" : "de venta"} en ${tf}. ${w}`,
        mkt: "Entrada a mercado.", buyw: "Entrada en un retroceso a la zona marcada.", sellw: "Entrada en un rebote a la zona marcada.",
        months: ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"], silver: "PLATA" },
};
const NAMES = { XAUUSD: "GOLD", EURUSD: "EUR/USD", GBPUSD: "GBP/USD", USDJPY: "USD/JPY", XAGUSD: "SILVER", BTCUSD: "BITCOIN",
  US30: "DOW JONES", NAS100: "NASDAQ", WTI: "WTI", BRENT: "BRENT", ETHUSD: "ETHEREUM", US500: "S&P 500", GER40: "DAX",
  UK100: "FTSE 100", JP225: "NIKKEI" };
// name -> [yahoo ticker, pip, decimals]
const MARKETS = { XAUUSD: ["GC=F", 0.1, 2], EURUSD: ["EURUSD=X", 0.0001, 5], GBPUSD: ["GBPUSD=X", 0.0001, 5],
  USDJPY: ["USDJPY=X", 0.01, 3], XAGUSD: ["SI=F", 0.01, 3], BTCUSD: ["BTC-USD", 1, 0], US30: ["YM=F", 1, 0],
  NAS100: ["NQ=F", 1, 0], WTI: ["CL=F", 0.01, 2], BRENT: ["BZ=F", 0.01, 2], ETHUSD: ["ETH-USD", 0.1, 1],
  US500: ["ES=F", 0.1, 1], GER40: ["^GDAXI", 1, 0], UK100: ["^FTSE", 1, 0], JP225: ["^N225", 1, 0] };

function pname(name, lang) {
  if (name === "XAUUSD") return TXT[lang].GOLD;
  if (name === "XAGUSD") return TXT[lang].silver;
  return NAMES[name] || (name.length === 6 ? `${name.slice(0, 3)}/${name.slice(3)}` : name);
}
const fmt = (x, dec) => x.toFixed(dec);

// ------------------------------------------------------------------ parser (port of daily.parse_signal)
// JS \b and \w are ASCII only; the source writes Russian, so build Unicode-aware versions.
const W = "[\\p{L}\\p{N}_]";
const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;
const U = (s, f = "u") => new RegExp(s.replaceAll("\\b", B).replaceAll("\\w", W), f);
const NW = "[^\\p{L}\\p{N}_\\n]";             // python [^\w\n]

const RU = [["ПОКУПК\\w*|ПОКУПАЕМ|ЛОНГ|LONG", "BUY"], ["ПРОДАЖ\\w*|ПРОДАЕМ|ПРОДАЁМ|ШОРТ|SHORT", "SELL"],
  ["СТОП[\\s-]*ЛОСС|СТОП|СЛ\\b", "SL"], ["ТЕЙК[\\s-]*ПРОФИТ|ТЕЙК|ТП", "TP"],
  ["ВХОД|ENTRY\\s*(PRICE|ZONE)?|ENTRADA|ЦЕНА", "ENTRY"], ["STOP\\s*LOSS", "SL"], ["TAKE\\s*PROFIT|TARGET", "TP"]];
const INSTR = [["XAUUSD", "\\b(XAU\\s*/?\\s*USD|GOLD|ORO|ЗОЛОТ\\w*)\\b"], ["EURUSD", "\\bEUR\\s*/?\\s*USD\\b"],
  ["GBPUSD", "\\bGBP\\s*/?\\s*USD\\b"], ["USDJPY", "\\bUSD\\s*/?\\s*JPY\\b"], ["BTCUSD", "\\b(BTC|BITCOIN|БИТКОИН)\\w*"],
  ["US30", "\\b(US30|DOW)\\b"], ["NAS100", "\\b(NAS100|NASDAQ|US100)\\b"], ["XAGUSD", "\\b(XAG\\s*/?\\s*USD|SILVER|СЕРЕБР\\w*)\\b"],
  ["BRENT", "\\b(BRENT|UKOIL|XBR\\s*/?\\s*USD)\\b"], ["WTI", "\\b(USOIL|WTI|XTI\\s*/?\\s*USD|CRUDE|OIL|НЕФТ\\w*)\\b"],
  ["ETHUSD", "\\b(ETH|ETHEREUM|ЭФИР\\w*)\\b"], ["US500", "\\b(US500|SPX500|SP500|S&P\\s*500)\\b"],
  ["GER40", "\\b(GER40|GER30|DE40|DAX)\\b"], ["UK100", "\\b(UK100|FTSE)\\b"], ["JP225", "\\b(JP225|JPN225|NIKKEI)\\b"]];
const FX = "USD|EUR|GBP|JPY|CHF|AUD|NZD|CAD";
const NUM = "(\\d+(?:\\.\\d+)?)";

function instrumentOf(t) {
  for (const [name, rx] of INSTR) if (U(rx).test(t)) return name;
  const m = t.match(U(`\\b(${FX})\\s*/?\\s*(${FX})\\b`));
  if (!m || m[1] === m[2]) return null;
  const name = m[1] + m[2];
  if (!MARKETS[name]) MARKETS[name] = [`${name}=X`, name.includes("JPY") ? 0.01 : 0.0001, name.includes("JPY") ? 3 : 5];
  return name;
}

export function parseSignal(text) {
  let t = text.toUpperCase().replace(/(?<=\d),(?=\d)/g, ".").replaceAll("📍", " ENTRY ");
  for (const [rx, rep] of RU) t = t.replace(U(rx, "gu"), rep);
  const name = instrumentOf(t);
  const side = t.match(U("\\b(BUY|SELL)\\b"));
  if (!name || !side) return null;
  const sl = t.match(U(`\\bSL\\b${NW}{0,6}${NUM}`));
  const tps = [...t.matchAll(U(`\\bTP(?:\\s?\\d(?![\\d.]))?${NW}{0,6}${NUM}`, "gu"))].map((m) => parseFloat(m[1]));
  const rng = `${NUM}(?:\\s*[-–/]\\s*${NUM})?`;
  let m = t.match(U(`\\bENTRY\\b${NW}{0,6}${rng}`));
  if (!m) {                                   // numbers between the side and the first SL/TP, instrument removed
    const after = t.slice(side.index + side[0].length);
    const stop = after.match(U("\\b(SL|TP)\\b"));
    let seg = stop ? after.slice(0, stop.index) : after.slice(0, 60);
    seg = seg.replace(U(`\\b(${FX})\\s*/?\\s*(${FX})\\b|\\b(US30|NAS100|US100|XAU\\s*/?\\s*USD|XAG\\s*/?\\s*USD|BTC\\w*)\\b`, "gu"), " ");
    m = seg.match(new RegExp(rng));
  }
  if (!(sl && tps.length && m)) return null;
  const ent = [m[1], m[2]].filter(Boolean).map(parseFloat);
  const entry = ent.reduce((a, b) => a + b, 0) / ent.length, s = parseFloat(sl[1]), sd = side[1];
  const tp1 = tps[0], tp2 = tps.length > 1 ? tps[tps.length - 1] : tp1 + (tp1 - entry);
  const ok = sd === "BUY" ? s < entry && entry < tp1 && tp1 <= tp2 : s > entry && entry > tp1 && tp1 >= tp2;
  if (!ok) return null;
  return { name, side: sd, entry, sl: s, tp1, tp2, at_market: !t.includes("LIMIT") };
}

// ------------------------------------------------------------------ market data
const tfFor = (name) => (["XAUUSD", "XAGUSD", "BTCUSD", "US30", "NAS100"].includes(name) ? "15m" : "1h");

async function candles(name, tf) {
  const tk = MARKETS[name][0];
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(tk)}?interval=${tf === "15m" ? "15m" : "60m"}&range=${tf === "15m" ? "5d" : "1mo"}`;
  const r = await fetch(url, { cf: { cacheTtl: 30 } });
  if (!r.ok) throw new Error(`yahoo ${r.status}`);
  const j = await r.json();
  const res = j.chart.result[0], q = res.indicators.quote[0];
  const out = [];
  res.timestamp.forEach((ts, i) => {
    const o = q.open[i], h = q.high[i], l = q.low[i], c = q.close[i];
    if ([o, h, l, c].every((v) => typeof v === "number")) out.push({ ts, o, h, l, c });
  });
  if (out.length < 10) throw new Error("few candles");
  return out.slice(-70);
}

async function spot(metal) {                 // gold/silver futures are shifted to spot so the card matches the signal
  try {
    const r = await fetch(`https://api.gold-api.com/price/${metal}`);
    const j = await r.json();
    return typeof j.price === "number" ? j.price : null;
  } catch { return null; }
}

// ------------------------------------------------------------------ card
const C = { bg: "#0E1525", grid: "#1A2438", text: "#F2F5FA", muted: "#8C98AE", buy: "#22C38E", sell: "#F0506E", gold: "#F2B33D" };

function stamp(lang) {
  const d = new Date(Date.now() - 3 * 3600e3);       // Brazil time (UTC-3)
  return `${String(d.getUTCDate()).padStart(2, "0")} ${TXT[lang].months[d.getUTCMonth()]}, ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} BRT`;
}

function chartConfig(name, tf, bars, p, lang, last) {
  const L = TXT[lang], up = p.side === "BUY", sc = up ? C.buy : C.sell, n = bars.length;
  const dec = name === "XAUUSD" ? 1 : MARKETS[name][2];
  let lo = Math.min(...bars.map((b) => b.l), p.sl, p.tp2), hi = Math.max(...bars.map((b) => b.h), p.sl, p.tp2);
  const pad = (hi - lo) * 0.07;
  const atr = bars.slice(-14).reduce((a, b) => a + (b.h - b.l), 0) / Math.min(14, n);
  const right = Math.round(n * 0.6), x0 = n + 1, x1 = n + Math.round(right * 0.5);
  const line = (y, col, txt, w = 2) => ({ type: "line", xMin: x0, xMax: x1, yMin: y, yMax: y, borderColor: col, borderWidth: w,
    label: { enabled: true, display: true, content: txt, position: "end", xAdjust: 70, backgroundColor: col, color: C.bg,
      font: { size: 15, weight: "bold" }, padding: 6, borderRadius: 4 } });
  const ann = {
    zone: { type: "box", xMin: -1, xMax: n + right, yMin: p.entry - atr * 0.25, yMax: p.entry + atr * 0.25,
      backgroundColor: up ? "rgba(34,195,142,0.16)" : "rgba(240,80,110,0.16)", borderWidth: 0,
      label: { enabled: true, display: true, content: `${L.zone} ${L[up ? "BUY" : "SELL"]}`, position: "start", color: sc, font: { size: 13, weight: "bold" } } },
    reward: { type: "box", xMin: x0, xMax: x1, yMin: Math.min(p.entry, p.tp2), yMax: Math.max(p.entry, p.tp2), backgroundColor: "rgba(34,195,142,0.22)", borderWidth: 0 },
    risk: { type: "box", xMin: x0, xMax: x1, yMin: Math.min(p.entry, p.sl), yMax: Math.max(p.entry, p.sl), backgroundColor: "rgba(240,80,110,0.28)", borderWidth: 0 },
    now: { type: "line", xMin: -1, xMax: n + right, yMin: last, yMax: last, borderColor: C.gold, borderWidth: 1, borderDash: [6, 4],
      label: { enabled: true, display: true, content: `${L.NOW} ${fmt(last, dec)}`, position: "start", backgroundColor: C.gold, color: C.bg, font: { size: 12, weight: "bold" } } },
    tp2: line(p.tp2, C.buy, `TP2  ${fmt(p.tp2, dec)}`), tp1: line(p.tp1, C.buy, `TP1  ${fmt(p.tp1, dec)}`),
    entry: line(p.entry, C.text, `${L.ENTRY}  ${fmt(p.entry, dec)}`, 3), sl: line(p.sl, C.sell, `SL  ${fmt(p.sl, dec)}`),
  };
  const head = `${pname(name, lang)}   ${L[p.side]} ${p.at_market ? L.NOW : L.LIMIT}`;
  return {
    version: "3", backgroundColor: C.bg, width: 800, height: 800, format: "png",
    chart: {
      type: "candlestick",
      data: { datasets: [{ data: bars.map((b, i) => ({ x: i, o: b.o, h: b.h, l: b.l, c: b.c })),
        color: { up: C.buy, down: C.sell, unchanged: C.muted }, borderColor: { up: C.buy, down: C.sell, unchanged: C.muted } }] },
      options: {
        layout: { padding: { left: 10, right: 20, top: 10, bottom: 10 } },
        plugins: {
          legend: { display: false },
          title: { display: true, text: head, color: up ? C.buy : C.sell, align: "start", font: { size: 30, weight: "bold" } },
          subtitle: { display: true, text: `${L.chart} ${tf.toUpperCase()}  ·  ${stamp(lang)}  ·  ${p.reason}`, color: C.muted, align: "start", font: { size: 13 }, padding: { bottom: 14 } },
          annotation: { annotations: ann },
        },
        scales: {
          x: { type: "linear", min: -1, max: n + right, ticks: { display: false }, grid: { display: false } },
          y: { position: "left", min: lo - pad, max: hi + pad, grid: { color: C.grid }, ticks: { color: C.muted, font: { size: 12 } } },
        },
      },
    },
  };
}

function caption(name, tf, p, lang) {
  const L = TXT[lang], [, pip, d] = MARKETS[name], dec = name === "XAUUSD" ? 1 : d;
  const risk = Math.abs(p.entry - p.sl) || 1e-9;
  const rr = (t) => (Math.abs(t - p.entry) / risk).toFixed(1);
  const pips = Math.round(risk / pip).toLocaleString("de-DE");
  return [`${p.side === "BUY" ? "🟢" : "🔴"} ${pname(name, lang)} ${L[p.side]} ${p.at_market ? L.NOW : L.LIMIT} · ${tf.toUpperCase()}`, "",
    `📍 ${L.entry}: ${fmt(p.entry, dec)}`, `🛑 SL: ${fmt(p.sl, dec)}  (${pips} pips)`,
    `🎯 TP1: ${fmt(p.tp1, dec)}  (1:${rr(p.tp1)})`, `🏁 TP2: ${fmt(p.tp2, dec)}  (1:${rr(p.tp2)})`, "",
    p.reason, L.tp1, "", L.nfa].join("\n");
}

function textSignal(s, lang) {                 // no market data: the same levels as text
  const L = TXT[lang], dec = s.entry < 20 ? 5 : s.entry < 200 ? 3 : 2, risk = Math.abs(s.entry - s.sl) || 1e-9;
  const rr = (t) => (Math.abs(t - s.entry) / risk).toFixed(1);
  return [`${s.side === "BUY" ? "🟢" : "🔴"} ${pname(s.name, lang)} ${L[s.side]} ${s.at_market ? L.NOW : L.LIMIT}`, "",
    `📍 ${L.entry}: ${fmt(s.entry, dec)}`, `🛑 SL: ${fmt(s.sl, dec)}`, `🎯 TP1: ${fmt(s.tp1, dec)}  (1:${rr(s.tp1)})`,
    `🏁 TP2: ${fmt(s.tp2, dec)}  (1:${rr(s.tp2)})`, "", L.tp1, "", L.nfa].join("\n");
}

// ------------------------------------------------------------------ telegram
async function tg(token, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${token}/${method}`, body instanceof FormData
    ? { method: "POST", body } : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) });
  return r.json();
}

// Build and send one signal. Returns a short log line (no texts or ids).
async function post(token, rc, s) {
  const lang = rc.lang, tf = tfFor(s.name);
  let bars = null, last = null;
  try {
    bars = await candles(s.name, tf);
    if (s.name === "XAUUSD" || s.name === "XAGUSD") {
      const px = await spot(s.name === "XAUUSD" ? "XAU" : "XAG");
      const fut = bars[bars.length - 1].c;
      if (px && Math.abs(px / fut - 1) < 0.03) bars = bars.map((b) => ({ ts: b.ts, o: b.o - fut + px, h: b.h - fut + px, l: b.l - fut + px, c: b.c - fut + px }));
    }
    last = bars[bars.length - 1].c;
  } catch (e) { bars = null; }
  if (last && Math.abs(s.entry / last - 1) > FAR) return { done: true, log: `${s.name} far from market: skipped` };
  if (bars) {
    const p = { ...s, at_market: s.at_market && Math.abs(s.entry - last) / last < 0.002 };
    const L = TXT[lang];
    p.reason = L.reason(p.side, tf.toUpperCase(), p.at_market ? L.mkt : p.side === "BUY" ? L.buyw : L.sellw);
    try {
      const img = await fetch("https://quickchart.io/chart", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(chartConfig(s.name, tf, bars, p, lang, last)) });
      if (img.ok) {
        const fd = new FormData();
        fd.append("chat_id", String(rc.chat));
        fd.append("caption", caption(s.name, tf, p, lang));
        fd.append("photo", new Blob([await img.arrayBuffer()], { type: "image/png" }), "signal.png");
        const j = await tg(token, "sendPhoto", fd);
        if (j.ok) return { done: true, log: `${s.name} ${s.side} card ok` };
      }
    } catch (e) { /* fall through to text */ }
  }
  const j = await tg(token, "sendMessage", { chat_id: rc.chat, text: textSignal(s, lang) });
  return { done: !!j.ok, log: `${s.name} ${s.side} text ${j.ok ? "ok" : "FAILED " + (j.description || "")}` };
}

// ------------------------------------------------------------------ one run
async function runBot(env, rc, log) {
  const token = env[rc.bot];
  if (!token) { log.push(`${rc.lang}: no token`); return; }
  const key = `state:${rc.bot}`;
  const st = (await env.STATE.get(key, "json")) || { offset: 0, src: [], relayed: {} };
  let dirty = false;
  const now = Date.now() / 1000;
  for (let page = 0; page < 10; page++) {
    const j = await tg(token, "getUpdates", { offset: st.offset, limit: 100, timeout: 0, allowed_updates: ["channel_post", "my_chat_member"] });
    if (!j.ok) { log.push(`${rc.lang}: getUpdates ${j.description}`); break; }
    for (const u of j.result) {
      st.offset = u.update_id + 1; dirty = true;
      const p = u.channel_post;
      if (!p || String(p.chat.id) === String(rc.chat)) continue;
      if (!String(p.chat.title || "").toLowerCase().includes(rc.source)) continue;
      const sig = parseSignal(p.text || p.caption || "");
      if (sig) st.src.push({ ...sig, ts: p.date, id: p.message_id });
      log.push(`${rc.lang}: source post ${sig ? "signal " + sig.name + " " + sig.side : "no signal"}`);
    }
    if (j.result.length < 100) break;
  }
  st.src = st.src.filter((s) => now - s.ts < 3 * 86400);
  for (const s of st.src.sort((a, b) => a.ts - b.ts)) {
    const k = String(s.id);
    if (st.relayed[k]) continue;
    if (now - s.ts > FRESH_MIN * 60) { st.relayed[k] = now; dirty = true; continue; }
    const r = await post(token, rc, s);
    log.push(`${rc.lang}: ${r.log}`);
    if (r.done) { st.relayed[k] = now; dirty = true; }
  }
  for (const k of Object.keys(st.relayed)) if (now - st.relayed[k] > 4 * 86400) { delete st.relayed[k]; dirty = true; }
  if (dirty) await env.STATE.put(key, JSON.stringify(st));     // KV free tier: 1000 writes/day, so write only on change
}

async function runAll(env) {
  const log = [];
  for (const rc of RELAYS) {
    try { await runBot(env, rc, log); } catch (e) { log.push(`${rc.lang}: error ${e.message}`); }
  }
  if (log.length) {
    console.log(log.join("\n"));
    await env.STATE.put("last_log", JSON.stringify({ t: new Date().toISOString(), log }), { expirationTtl: 7 * 86400 });
  }
  return log;
}

export default {
  async scheduled(event, env, ctx) { ctx.waitUntil(runAll(env)); },
  async fetch(req, env) {
    const u = new URL(req.url);
    if (u.pathname === "/health") {
      const last = await env.STATE.get("last_log");
      return new Response(last || "{}", { headers: { "content-type": "application/json" } });
    }
    return new Response("ok");
  },
};
