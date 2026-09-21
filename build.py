#!/usr/bin/env python3
"""Regenerate the site from src/*.md.

    pandoc required.   python3 build.py
                       python3 build.py --cards     # also redraw og/*.png

Adding a post: drop the markdown in src/, add an entry to POSTS, run this.
The h1 and the italic line under it become the title and the standfirst;
`blurb` is what search engines and link previews show.

A post with `of` (the slug it translates) and a `lang` is a translation: it
gets a page, hreflang links and a sitemap entry, but no place in the index or
the feed.

Link-preview cards (og/*.png, 1200x630) are drawn on the first build that
needs one, and again with --cards. That needs Pillow and macOS fonts, so the
PNGs are committed and an ordinary build only points at them.
"""
import html
import json
import math
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "src"
SITE = "https://blog.sandrpod.com"

POSTS = [
    {
        "src": "e2b-compatible-sandbox-api.md",
        "slug": "e2b-compatible-sandbox-api",
        "date": "2026-08-06",
        "rfc822": "Thu, 06 Aug 2026 09:00:00 +0000",
        "blurb": "One VM, one domain, four containers — then the unmodified E2B "
                 "SDK talking to infrastructure you own, checked feature by feature.",
    },
    {
        "src": "deepagents-sandbox-backends.md",
        "slug": "deepagents-sandbox-backends",
        "date": "2026-08-06",
        "rfc822": "Thu, 06 Aug 2026 09:30:00 +0000",
        "blurb": "langchain-e2b and langchain-sandrpod, same twelve assertions down "
                 "each path. Both pass. Testing both is what found two defects, both "
                 "of them mine, and a third thing that only looked like one.",
    },
    {
        "src": "e2b-compatible-sandbox-api.zh.md",
        "slug": "zh/e2b-compatible-sandbox-api",
        "of": "e2b-compatible-sandbox-api",
        "lang": "zh-Hans",
        "date": "2026-09-21",
        "blurb": "一台机器、一个域名、四个容器——然后让未经修改的官方 E2B SDK "
                 "连上你自己的基础设施，并逐个功能验证。",
    },
]
POSTS = [{"lang": "en", **p} for p in POSTS]

NATIVE = {"en": "English", "zh-Hans": "中文版"}
GITHUB = "https://github.com/sandrpod/sandrpod"

# Everything a reader sees that isn't the article itself.
UI = {
    "en": {
        "locale": "en_US", "skip": "Skip to content", "writing": "Writing",
        "theme": "Toggle colour theme", "dark": "Dark", "light": "Light",
        "source": "Source",
        "tag": "SandrPod — self-hosted execution infrastructure for AI agents",
        "tagline": "Self-hosted execution infrastructure for AI agents",
        "foot": f'<div class="article-foot">SandrPod is open source, Apache-2.0 — '
                f'<a href="{GITHUB}">github.com/sandrpod/sandrpod</a>. '
                f'<a href="/">More writing</a>.</div>',
    },
    "zh-Hans": {
        "locale": "zh_CN", "skip": "跳到正文", "writing": "文章",
        "theme": "切换配色", "dark": "深色", "light": "浅色",
        "source": "源码",
        "tag": "SandrPod — 自托管的 AI 智能体执行基础设施",
        "tagline": "自托管的 AI 智能体执行基础设施",
        "foot": f'<div class="article-foot">SandrPod 是开源项目，Apache-2.0 — '
                f'<a href="{GITHUB}">github.com/sandrpod/sandrpod</a>。'
                f'<a href="/">更多文章</a>（英文）。</div>',
    },
}

# Must run before first paint, or dark-mode readers get a white flash.
THEME_BOOT = (
    '<script>(function(){try{var t=localStorage.getItem("theme");'
    'if(t)document.documentElement.setAttribute("data-theme",t)}catch(e){}})()</script>'
)

THEME_TOGGLE = """<script>
(function () {
  var btn = document.querySelector('.theme-toggle');
  if (!btn) return;
  var root = document.documentElement;
  function current() {
    return root.getAttribute('data-theme') ||
      (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function label() { btn.textContent = current() === 'dark' ? btn.dataset.light : btn.dataset.dark; }
  label();
  btn.addEventListener('click', function () {
    var next = current() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) {}
    label();
  });
})();
</script>"""


def shell(*, title, desc, canonical, body, card, lang="en", og_type="article",
          alts=(), ld=None):
    ui = UI[lang]
    t, d = html.escape(title), html.escape(desc)
    img = f"{SITE}/{card}"
    hreflangs = "".join(
        f'<link rel="alternate" hreflang="{h}" href="{u}">\n' for h, u in alts)
    jsonld = ""
    if ld:  # "</" would end the script element early
        jsonld = ('<script type="application/ld+json">'
                  + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")
                  + "</script>\n")
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<link rel="canonical" href="{canonical}">
{hreflangs}<meta property="og:type" content="{og_type}">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="SandrPod">
<meta property="og:locale" content="{ui['locale']}">
<meta property="og:image" content="{img}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{t}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{d}">
<meta name="twitter:image" content="{img}">
<meta name="twitter:image:alt" content="{t}">
<link rel="alternate" type="application/rss+xml" title="SandrPod" href="{SITE}/feed.xml">
<link rel="stylesheet" href="/style.css">
{jsonld}{THEME_BOOT}
</head>
<body>
<a class="skip" href="#main">{ui['skip']}</a>
<header class="masthead">
  <a class="wordmark" href="/">SandrPod</a>
  <nav aria-label="Main">
    <a href="/">{ui['writing']}</a>
    <a href="{GITHUB}">GitHub</a>
    <button class="theme-toggle" type="button" aria-label="{ui['theme']}" data-dark="{ui['dark']}" data-light="{ui['light']}">{ui['dark']}</button>
  </nav>
</header>
<main id="main">
{body}
</main>
<footer>
  <span>{ui['tag']}</span>
  <a href="{GITHUB}">{ui['source']}</a>
  <a href="{SITE}/feed.xml">RSS</a>
  <span>Apache-2.0</span>
</footer>
{THEME_TOGGLE}
<script data-goatcounter="https://sandrpod.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>
</body>
</html>
"""


def split_head(frag):
    """Title from the h1; standfirst from the italic paragraph right after it."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", frag, re.S)
    title = html.unescape(" ".join(re.sub(r"<[^>]+>", "", m.group(1)).split())) if m else ""
    s = re.search(r"</h1>\s*<p><em>(.*?)</em></p>", frag, re.S)
    sub = html.unescape(" ".join(re.sub(r"<[^>]+>", "", s.group(1)).split())) if s else ""
    return title, sub


def lastmod(post):
    """When the source last changed: today if it has uncommitted edits, else its last commit."""
    def git(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    f = f"src/{post['src']}"
    if git("status", "--porcelain", "--", f):
        return date.today().isoformat()
    return git("log", "-1", "--format=%cs", "--", f) or post["date"]


# ── link-preview cards ─────────────────────────────────────────────────────

def oklch(L, C, h):
    """CSS oklch() -> sRGB, so the cards are drawn in the stylesheet's own colours."""
    a, b = C * math.cos(math.radians(h)), C * math.sin(math.radians(h))
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    gamma = lambda x: 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return tuple(round(255 * max(0.0, min(1.0, gamma(x)))) for x in lin)


# style.css :root, light theme
PAPER, INK, SOFT = oklch(.982, .004, 85), oklch(.21, .012, 60), oklch(.46, .012, 60)
FAINT, RULE, ACCENT = oklch(.64, .010, 60), oklch(.88, .008, 70), oklch(.52, .155, 42)

FONTS = "/System/Library/Fonts"
CJK = r"[　-鿿＀-￯]"
NO_LINE_START = "，。、；：！？）》」』】”’…"


def wrap(runs, width, clause=""):
    """runs: [(text, font, colour, atomic)] -> lines of tokens. A run ends with the space
    that separates it from the next; an atomic run is never split. CJK breaks anywhere
    except before closing punctuation, and after `clause` once the line is a third full."""
    toks = []
    for text, font, colour, atomic in runs:
        for t in ([text] if atomic else
                  re.findall(CJK + r"|(?:(?!" + CJK + r")\S)+\s*|\s+", text)):
            if toks and t in NO_LINE_START and toks[-1][1] is font:
                toks[-1] = (toks[-1][0] + t, font, colour)
            else:
                toks.append((t, font, colour))
    lines, cur, used = [], [], 0.0
    for t, font, colour in toks:
        if cur and used + font.getlength(t.rstrip()) > width:
            lines.append(cur)
            cur, used = [], 0.0
        cur.append((t, font, colour))
        used += font.getlength(t)
        if clause and t.endswith(clause) and used > width / 3:
            lines.append(cur)
            cur, used = [], 0.0
    return lines + ([cur] if cur else [])


def draw_card(path, runs, lang):
    """runs: [(text, emphasised)] — the title, with the emphasised part in italic accent."""
    from PIL import Image, ImageDraw, ImageFont
    W, H, M = 1200, 630, 84
    zh = lang.startswith("zh")
    face = lambda f, i, size: ImageFont.truetype(f"{FONTS}/{f}", size, index=i)
    roman = ("Supplemental/Songti.ttc", 6) if zh else ("Supplemental/Iowan Old Style.ttc", 0)
    italic = roman if zh else ("Supplemental/Iowan Old Style.ttc", 2)

    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    x, wm = M, face("HelveticaNeue.ttc", 1, 26)          # tracked-out wordmark, as on the site
    for ch in "SANDRPOD":
        d.text((x, 86), ch, font=wm, fill=INK, anchor="ls")
        x += wm.getlength(ch) + 26 * 0.16
    d.text((W - M, 86), "blog.sandrpod.com", font=face("HelveticaNeue.ttc", 0, 24),
           fill=FAINT, anchor="rs")
    d.line([(M, 118), (W - M, 118)], fill=RULE, width=2)
    d.rectangle([M, 172, M + 84, 178], fill=ACCENT)

    for size in range(76, 43, -2):                        # largest title that fits
        fonts = {False: face(*roman, size), True: face(*italic, size)}
        lines = wrap([(t, fonts[e], ACCENT if e else INK, e) for t, e in runs], W - 2 * M,
                     "，" if zh else "")
        if len(lines) <= 4 and len(lines) * size * 1.14 <= 300:
            break
    y = 206 + fonts[False].getmetrics()[0]
    for line in lines:
        x = M
        for t, font, colour in line:
            d.text((x, y), t, font=font, fill=colour, anchor="ls")
            x += font.getlength(t)
        y += round(size * 1.14)

    tag = face("Hiragino Sans GB.ttc", 0, 28) if zh else face("HelveticaNeue.ttc", 0, 28)
    d.text((M, H - 60), UI[lang]["tagline"], font=tag, fill=SOFT, anchor="ls")

    path.parent.mkdir(exist_ok=True)
    img.save(path, optimize=True)
    print(f"  {path.relative_to(ROOT)}  (drawn)")


def card(rel, runs, lang):
    if not (ROOT / rel).exists() or "--cards" in sys.argv:
        draw_card(ROOT / rel, runs, lang)


# ── build ──────────────────────────────────────────────────────────────────

if not SRC.is_dir():
    sys.exit(f"missing {SRC}")

by_slug = {p["slug"]: p for p in POSTS}
url = lambda p: f"{SITE}/{p['slug']}/"


def family(p):
    """The post and its translations, original first."""
    root = by_slug[p["of"]] if "of" in p else p
    return [root] + [q for q in POSTS if q.get("of") == root["slug"]]


built = []
for post in POSTS:
    md = SRC / post["src"]
    if not md.is_file():
        sys.exit(f"missing {md}")
    lang, canonical, modified = post["lang"], url(post), lastmod(post)
    # zh: a newline between two CJK characters must not turn into a space
    reader = "gfm+east_asian_line_breaks" if lang.startswith("zh") else "gfm"
    frag = subprocess.run(
        ["pandoc", str(md), "-f", reader, "-t", "html5", "--syntax-highlighting=none"],
        capture_output=True, text=True, check=True,
    ).stdout
    # short inline code must not wrap, or `--flag` splits between its dashes; long stays wrappable
    frag = re.sub(r"(?<!<pre>)<code>([^<\n]{1,28})</code>", r'<code class="nb">\1</code>', frag)
    title, sub = split_head(frag)

    fam = family(post)
    switch = "".join(
        f'<p class="lang-switch"><a href="{url(q)}" lang="{q["lang"]}" '
        f'hreflang="{q["lang"]}">{NATIVE[q["lang"]]}</a></p>\n'
        for q in fam if q is not post)
    alts = ([(q["lang"], url(q)) for q in fam] + [("x-default", url(fam[0]))]
            if len(fam) > 1 else [])

    cardfile = "og/" + post["slug"].replace("/", "-") + ".png"
    card(cardfile, [(title, False)], lang)

    publisher = {"@type": "Organization", "name": "SandrPod", "url": SITE + "/"}
    ld = {
        "@context": "https://schema.org", "@type": "BlogPosting",
        "headline": title, "description": post["blurb"], "inLanguage": lang,
        "image": [f"{SITE}/{cardfile}"],
        "datePublished": post["date"], "dateModified": modified,
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "author": {**publisher, "url": GITHUB}, "publisher": publisher,
    }

    out = ROOT / post["slug"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(shell(
        title=f"{title} — SandrPod", desc=post["blurb"], canonical=canonical,
        lang=lang, card=cardfile, alts=alts, ld=ld,
        body=f"<article>\n{switch}{frag}\n{UI[lang]['foot']}\n</article>"))
    built.append({**post, "title": title, "sub": sub, "canonical": canonical,
                  "lastmod": modified})
    print(f"  {post['slug']}/index.html")

english = [p for p in built if p["lang"] == "en"]
entries = "\n".join(
    f'<a class="entry" href="/{p["slug"]}/">\n'
    f'  <div class="meta"><span class="no">{n:02d}</span>{p["date"]}</div>\n'
    f"  <div>\n"
    f'    <h2>{html.escape(p["title"])}<span class="arrow">→</span></h2>\n'
    f'    <p>{html.escape(p["sub"])}</p>\n'
    f"  </div>\n</a>"
    for n, p in enumerate(english, 1)
)

HOME_DESC = ("Open-source, Apache-2.0. Run AI-agent code on infrastructure you own: "
             "any cloud, plain Docker, or a bare machine. E2B-SDK compatible.")
card("og/site.png", [("Run your agents’ code on ", False), ("your own ", True),
                     ("infrastructure.", False)], "en")
(ROOT / "index.html").write_text(shell(
    title="SandrPod — self-hosted execution infrastructure for AI agents",
    desc=HOME_DESC, canonical=SITE + "/", og_type="website", card="og/site.png",
    ld={"@context": "https://schema.org", "@type": "WebSite", "name": "SandrPod",
        "url": SITE + "/", "description": HOME_DESC, "inLanguage": "en"},
    body=f"""<section class="hero">
  <h1>Run your agents’ code on <em>your own</em> infrastructure.</h1>
  <p>SandrPod turns a server you control — any cloud, plain Docker, or a bare
  machine — into on-demand sandboxes. Speak its native API, or point the
  unmodified E2B SDK at it. Nothing dials in; workers dial out.</p>
  <p class="cta">
    <a href="{GITHUB}">github.com/sandrpod/sandrpod</a>
    <span>·</span>
    <a href="{GITHUB}/blob/main/docs/PRODUCTION_DEPLOYMENT.md">Deployment guide</a>
  </p>
</section>

<h2 class="writing-head">Writing</h2>
{entries}"""))
print("  index.html")

items = "\n".join(
    f"""    <item>
      <title>{html.escape(p['title'])}</title>
      <link>{p['canonical']}</link>
      <guid isPermaLink="true">{p['canonical']}</guid>
      <pubDate>{p['rfc822']}</pubDate>
      <description>{html.escape(p['blurb'])}</description>
    </item>"""
    for p in english
)
(ROOT / "feed.xml").write_text(f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>SandrPod</title>
    <link>{SITE}/</link>
    <atom:link href="{SITE}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>Self-hosted execution infrastructure for AI agents.</description>
    <language>en</language>
{items}
  </channel>
</rss>
""")

pages = [(SITE + "/", max(p["lastmod"] for p in built))] + [
    (p["canonical"], p["lastmod"]) for p in built]
urls = "\n".join(f"  <url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in pages)
(ROOT / "sitemap.xml").write_text(
    '<?xml version="1.0" encoding="utf-8"?>\n'
    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}\n</urlset>\n')
(ROOT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n")
print("  feed.xml  sitemap.xml  robots.txt")
