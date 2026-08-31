#!/usr/bin/env python3
"""Regenerate the site from src/*.md.

    pandoc required.   python3 build.py

Adding a post: drop the markdown in src/, add an entry to POSTS, run this.
The h1 and the italic line under it become the title and the standfirst;
`blurb` is what search engines and link previews show.
"""
import html
import re
import subprocess
import sys
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
]

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
  function label() { btn.textContent = current() === 'dark' ? 'Light' : 'Dark'; }
  label();
  btn.addEventListener('click', function () {
    var next = current() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) {}
    label();
  });
})();
</script>"""


def shell(*, title, desc, canonical, body, og_type="article"):
    t, d = html.escape(title), html.escape(desc)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="{og_type}">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="SandrPod">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{d}">
<link rel="alternate" type="application/rss+xml" title="SandrPod" href="{SITE}/feed.xml">
<link rel="stylesheet" href="/style.css">
{THEME_BOOT}
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="masthead">
  <a class="wordmark" href="/">SandrPod</a>
  <nav aria-label="Main">
    <a href="/">Writing</a>
    <a href="https://github.com/sandrpod/sandrpod">GitHub</a>
    <button class="theme-toggle" type="button" aria-label="Toggle colour theme">Dark</button>
  </nav>
</header>
<main id="main">
{body}
</main>
<footer>
  <span>SandrPod — self-hosted execution infrastructure for AI agents</span>
  <a href="https://github.com/sandrpod/sandrpod">Source</a>
  <a href="{SITE}/feed.xml">RSS</a>
  <span>Apache-2.0</span>
</footer>
{THEME_TOGGLE}
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


if not SRC.is_dir():
    sys.exit(f"missing {SRC}")

built = []
for i, post in enumerate(POSTS, 1):
    md = SRC / post["src"]
    if not md.is_file():
        sys.exit(f"missing {md}")
    frag = subprocess.run(
        ["pandoc", str(md), "-f", "gfm", "-t", "html5", "--syntax-highlighting=none"],
        capture_output=True, text=True, check=True,
    ).stdout
    title, sub = split_head(frag)
    canonical = f"{SITE}/{post['slug']}/"
    foot = (
        '<div class="article-foot">SandrPod is open source, Apache-2.0 — '
        '<a href="https://github.com/sandrpod/sandrpod">github.com/sandrpod/sandrpod</a>. '
        '<a href="/">More writing</a>.</div>'
    )
    out = ROOT / post["slug"]
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(
        shell(title=f"{title} — SandrPod", desc=post["blurb"],
              canonical=canonical, body=f"<article>\n{frag}\n{foot}\n</article>")
    )
    built.append({**post, "title": title, "sub": sub, "n": i, "canonical": canonical})
    print(f"  {post['slug']}/index.html")

entries = "\n".join(
    f'<a class="entry" href="/{p["slug"]}/">\n'
    f'  <div class="meta"><span class="no">{p["n"]:02d}</span>{p["date"]}</div>\n'
    f"  <div>\n"
    f'    <h2>{html.escape(p["title"])}<span class="arrow">→</span></h2>\n'
    f'    <p>{html.escape(p["sub"])}</p>\n'
    f"  </div>\n</a>"
    for p in built
)

(ROOT / "index.html").write_text(shell(
    title="SandrPod — self-hosted execution infrastructure for AI agents",
    desc="Open-source, Apache-2.0. Run AI-agent code on infrastructure you own: "
         "any cloud, plain Docker, or a bare machine. E2B-SDK compatible.",
    canonical=SITE + "/", og_type="website",
    body=f"""<section class="hero">
  <h1>Run your agents’ code on <em>your own</em> infrastructure.</h1>
  <p>SandrPod turns a server you control — any cloud, plain Docker, or a bare
  machine — into on-demand sandboxes. Speak its native API, or point the
  unmodified E2B SDK at it. Nothing dials in; workers dial out.</p>
  <p class="cta">
    <a href="https://github.com/sandrpod/sandrpod">github.com/sandrpod/sandrpod</a>
    <span>·</span>
    <a href="https://github.com/sandrpod/sandrpod/blob/main/docs/PRODUCTION_DEPLOYMENT.md">Deployment guide</a>
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
    for p in built
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

urls = "\n".join(f"  <url><loc>{u}</loc></url>"
                 for u in [SITE + "/"] + [p["canonical"] for p in built])
(ROOT / "sitemap.xml").write_text(
    '<?xml version="1.0" encoding="utf-8"?>\n'
    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}\n</urlset>\n')
(ROOT / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n")
print("  feed.xml  sitemap.xml  robots.txt")
