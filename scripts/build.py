#!/usr/bin/env python3
"""Render the small stats card (languages, stars, longest streak) as SVG.

    GITHUB_TOKEN=... python3 scripts/build.py --user ShinoKana --out dist

The card's aspect ratio matches the Moe-Counter image so the two sit side by
side at the same height. Fonts are pre-subset into assets/fonts (see
--subset) and embedded as data URIs, because SVGs shown through <img> cannot
load external fonts.
"""
import argparse
import base64
import json
import os
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"
FONT = "KleeOne-SemiBold"

LABELS = {"langs": "Languages", "stars": "Stars", "streak": "Longest streak", "days": " days"}

# Colours are taken from the header picture (ink bottle, lavender books,
# star charms, ribbon, paper).
THEMES = {
    "light": {
        "bg": "#ffffff",
        "border": "#e3e6ef",
        "text": "#34405e",
        "muted": "#7a84a0",
        "track": "#eef0f6",
        "palette": ["#446997", "#7a72a6", "#b9788a", "#5f86b3", "#c9b49a"],
    },
    "dark": {
        "bg": "#151a24",
        "border": "#272e3f",
        "text": "#dfe3f3",
        "muted": "#8d95b0",
        "track": "#222838",
        "palette": ["#7f9fd0", "#a99fd8", "#e3a3b3", "#9bc3e6", "#e5d3bd"],
    },
}


# ---------------------------------------------------------------- font


def subset_font(src_dir):
    """Write an ASCII subset of the full font in src_dir to assets/fonts."""
    from fontTools import subset

    opts = subset.Options()
    opts.flavor = "woff"
    opts.name_IDs = ["*"]
    font = subset.load_font(str(Path(src_dir) / f"{FONT}.ttf"), opts)
    sub = subset.Subsetter(opts)
    sub.populate(text="".join(chr(c) for c in range(0x20, 0x7F)))
    sub.subset(font)
    out = FONT_DIR / f"{FONT}.woff"
    out.parent.mkdir(parents=True, exist_ok=True)
    subset.save_font(font, str(out), opts)
    print(f"{out.name}: {out.stat().st_size // 1024} KB")


class Font:
    def __init__(self):
        path = FONT_DIR / f"{FONT}.woff"
        data = base64.b64encode(path.read_bytes()).decode()
        self.css = f"@font-face{{font-family:'klee';src:url(data:font/woff;base64,{data}) format('woff');}}"
        font = TTFont(str(path))
        self.cmap, self.hmtx, self.upm = font.getBestCmap(), font["hmtx"], font["head"].unitsPerEm

    def width(self, text, size):
        total = 0
        for ch in text:
            glyph = self.cmap.get(ord(ch))
            total += self.hmtx[glyph][0] if glyph else self.upm * 0.6
        return total * size / self.upm


# ---------------------------------------------------------------- data

QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC, first: 100) {
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
    contributionsCollection {
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
  }
}
"""


def fetch(user, token):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": {"login": user}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body["data"]["user"]


def summarize(u):
    repos = u["repositories"]["nodes"]
    sizes = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            sizes[name] = sizes.get(name, 0) + edge["size"]
    total = sum(sizes.values()) or 1
    langs = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]

    days = sorted(
        (d for w in u["contributionsCollection"]["contributionCalendar"]["weeks"] for d in w["contributionDays"]),
        key=lambda d: d["date"],
    )
    longest = run = 0
    for d in days:
        run = run + 1 if d["contributionCount"] > 0 else 0
        longest = max(longest, run)

    return {
        "stars": sum(r["stargazerCount"] for r in repos),
        "streak": longest,
        "langs": [(name, size / total * 100) for name, size in langs],
    }


# ---------------------------------------------------------------- svg


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card(font, theme, s):
    t = THEMES[theme]
    pal = t["palette"]
    W, H = 760, 220
    # Transparent strip on the left so the card lines up with the snake grid
    # (inset ~2%) without hspace, which would overflow on phones.
    PAD = 39
    L, R = 36, W - 36

    # stats, right-aligned on the first line
    items = [(LABELS["stars"], f"{s['stars']:,}"), (LABELS["streak"], f"{s['streak']}{LABELS['days']}")]
    stats, x = [], R
    for label, value in reversed(items):
        stats.append(f'<text class="num" x="{x}" y="66" text-anchor="end">{value}</text>')
        x -= font.width(value, 36) + 12
        stats.append(f'<text class="lbl" x="{x:.0f}" y="66" text-anchor="end">{label}</text>')
        x -= font.width(label, 24) + 44

    # one stacked bar for the languages
    segs, x = [], L
    for i, (_, pct) in enumerate(s["langs"]):
        w = (R - L) * pct / 100
        segs.append(f'<rect x="{x:.1f}" y="92" width="{w + 1:.1f}" height="16" fill="{pal[i % len(pal)]}"/>')
        x += w

    # legend, wrapping onto a second line when needed
    legend, x, y = [], L, 154
    for i, (name, pct) in enumerate(s["langs"]):
        pct_s = f"{pct:.1f}%"
        w = 20 + font.width(name, 24) + 10 + font.width(pct_s, 22)
        if x + w > R:
            x, y = L, y + 40
        legend.append(
            f'<circle cx="{x + 7}" cy="{y - 8}" r="7" fill="{pal[i % len(pal)]}"/>'
            f'<text class="name" x="{x + 22}" y="{y}">{esc(name)}</text>'
            f'<text class="lbl pct" x="{x + 22 + font.width(name, 24) + 10:.0f}" y="{y}">{pct_s}</text>'
        )
        x += w + 40

    # number colours drift through the palette, like the header text
    loop = ";".join(pal + pal[:1])
    shifted = ";".join(pal[2:] + pal[:3])
    css = f"""{font.css}
text{{font-family:'klee','Hiragino Mincho ProN',serif}}
.lbl{{font-size:24px;fill:{t['muted']}}}
.pct{{font-size:22px}}
.name{{font-size:24px;fill:{t['text']}}}
.num{{font-size:36px;fill:url(#ink)}}
"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-PAD} 0 {W + PAD} {H}" width="{W + PAD}" height="{H}" role="img" aria-label="languages, stars and longest streak">
<defs>
<style>{css}</style>
<linearGradient id="ink" x1="0" y1="0" x2="1" y2="0">
  <stop offset="0" stop-color="{pal[0]}"><animate attributeName="stop-color" values="{loop}" dur="12s" repeatCount="indefinite"/></stop>
  <stop offset="1" stop-color="{pal[2]}"><animate attributeName="stop-color" values="{shifted}" dur="12s" repeatCount="indefinite"/></stop>
</linearGradient>
<clipPath id="bar"><rect x="{L}" y="92" width="0" height="16" rx="8">
  <animate attributeName="width" from="0" to="{R - L}" dur="1.4s" fill="freeze" calcMode="spline" keySplines=".2 .8 .2 1" keyTimes="0;1"/>
</rect></clipPath>
</defs>
<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="20" fill="{t['bg']}" stroke="{t['border']}" stroke-width="2"/>
<text class="lbl" x="{L}" y="66">{LABELS['langs']}</text>
{''.join(stats)}
<rect x="{L}" y="92" width="{R - L}" height="16" rx="8" fill="{t['track']}"/>
<g clip-path="url(#bar)">{''.join(segs)}</g>
{''.join(legend)}
</svg>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "ShinoKana"))
    ap.add_argument("--out", default="dist")
    ap.add_argument("--data", help="read the GraphQL user object from this JSON file instead of the API")
    ap.add_argument("--subset", metavar="FONT_DIR", help="regenerate assets/fonts from the full TTF and exit")
    args = ap.parse_args()

    if args.subset:
        subset_font(args.subset)
        return

    if args.data:
        u = json.loads(Path(args.data).read_text())
    else:
        u = fetch(args.user, os.environ["GITHUB_TOKEN"])
    stats = summarize(u)
    font = Font()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        suffix = "" if theme == "light" else "-dark"
        (out / f"stats{suffix}.svg").write_text(card(font, theme, stats))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
