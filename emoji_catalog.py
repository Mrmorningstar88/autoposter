"""
List animated (custom) emoji packs so we can pick an emoji id per meaning (settings live in emoji.json).

  python emoji_catalog.py [extra_pack_name ...]

Packs come from the reference ids in emoji.json ("reference_ids"), the packs already named there, and the names given
on the command line. Writes charts/emoji_catalog.json and one contact sheet per pack (charts/emoji_<pack>.png, each
emoji numbered) — both are kept as run artifacts.
"""
import io, json, os, subprocess, sys, tempfile

import requests
from PIL import Image, ImageDraw, ImageFont

import scrape as S

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "charts")
TOKEN = os.getenv("TG_BOT_TOKEN")


def thumb(st):
    """A still image of one emoji: its thumbnail, the file itself for static (webp) emoji, or a video's first frame."""
    fids = ([st["thumbnail"]["file_id"]] if st.get("thumbnail") else []) + ([st["file_id"]] if not st.get("is_animated") else [])
    for fid in fids:
        j = S.tg(TOKEN, "getFile", data={"file_id": fid})
        if not j.get("ok"):
            continue
        data = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{j['result']['file_path']}", timeout=60).content
        if j["result"]["file_path"].endswith(".webm"):
            with tempfile.TemporaryDirectory() as t:
                open(f"{t}/v.webm", "wb").write(data)
                subprocess.run(["ffmpeg", "-loglevel", "error", "-c:v", "libvpx-vp9", "-i", f"{t}/v.webm", "-frames:v", "1", f"{t}/f.png"])
                data = open(f"{t}/f.png", "rb").read() if os.path.exists(f"{t}/f.png") else b""
        try:
            return Image.open(io.BytesIO(data)).convert("RGBA").resize((72, 72))
        except Exception:
            continue
    return None


def sheet(name, title, stickers):
    cols, cell = 10, 96
    rows = (len(stickers) + cols - 1) // cols
    img = Image.new("RGB", (cols * cell, rows * cell + 40), "white")
    dr = ImageDraw.Draw(img)
    from matplotlib import font_manager
    font = ImageFont.truetype(font_manager.findfont("DejaVu Sans"), 14)
    dr.text((8, 10), f"{name} — {title} ({len(stickers)})", fill="black", font=font)
    for i, st in enumerate(stickers):
        x, y = (i % cols) * cell, (i // cols) * cell + 40
        im = thumb(st)
        if im:
            img.paste(im, (x + 12, y + 2), im)
        dr.text((x + 4, y + 76), str(i), fill="red", font=font)
    p = os.path.join(OUT, f"emoji_{name}.png")
    img.save(p)
    return p


def main(extra):
    cfg = json.load(open(os.path.join(HERE, "emoji.json")))
    os.makedirs(OUT, exist_ok=True)
    names = list(dict.fromkeys(cfg.get("packs", []) + extra))
    ref = [i for i in cfg.get("reference_ids", []) if i]
    if ref:
        j = S.tg(TOKEN, "getCustomEmojiStickers", data={"custom_emoji_ids": json.dumps(ref)})
        for st in j.get("result", []):
            print(f"  reference {st['custom_emoji_id']} {st.get('emoji')} -> pack {st.get('set_name')}")
            if st.get("set_name"):
                names.append(st["set_name"])
        if not j.get("ok"):
            print(f"  ! getCustomEmojiStickers: {j.get('description')}")
    catalog = {}
    for name in dict.fromkeys(names):
        j = S.tg(TOKEN, "getStickerSet", data={"name": name})
        if not j.get("ok"):
            print(f"  ! {name}: {j.get('description')}")
            continue
        s = j["result"]
        sts = [x for x in s["stickers"] if x.get("custom_emoji_id")]
        if not sts:
            print(f"  · {name}: not an emoji pack ({s.get('sticker_type')})")
            continue
        catalog[name] = dict(title=s.get("title"), emoji=[dict(n=i, id=x["custom_emoji_id"], fallback=x.get("emoji"),
                                                              kind="video" if x.get("is_video") else "animated" if x.get("is_animated") else "static")
                                                         for i, x in enumerate(sts)])
        print(f"  ✓ {name} — {s.get('title')}: {len(sts)} emoji · " + " ".join(f"{i}{x.get('emoji')}" for i, x in enumerate(sts)))
        sheet(name, s.get("title", ""), sts[:200])
    json.dump(catalog, open(os.path.join(OUT, "emoji_catalog.json"), "w"), ensure_ascii=False, indent=1)
    print(f"  {len(catalog)} packs written to charts/")


if __name__ == "__main__":
    main(sys.argv[1:])
