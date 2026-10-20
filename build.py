#!/usr/bin/env python3
"""Render the article markdown into a single self-contained index.html."""

import html
import re
import sys
from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

SRC = Path(__file__).parent / "binlog-router-bento.md"
DST = Path(__file__).parent / "index.html"

LEDE = (
    "Jak z go-mysql i Bento złożyć router CDC, który czyta binlog MySQL "
    "i rozsyła zmiany na trzy różne sinki, każdy z własnym filtrem."
)


def slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text).lower()
    repl = str.maketrans("ąćęłńóśźż", "acelnoszz")
    text = text.translate(repl)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def inline(text: str) -> str:
    """Escape, then apply code spans, bold, italics and links."""
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def render_code(lang: str, code: str) -> str:
    try:
        lexer = get_lexer_by_name(lang or "text")
    except ClassNotFound:
        lexer = get_lexer_by_name("text")
    return highlight(code, lexer, HtmlFormatter(cssclass="highlight"))


def convert(md: str) -> tuple[str, str, list[tuple[int, str, str]]]:
    lines = md.split("\n")
    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    title = ""
    para: list[str] = []
    items: list[list[str]] = []
    i = 0

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def flush_list() -> None:
        if items:
            body = "".join(f"<li>{inline(' '.join(it))}</li>" for it in items)
            out.append(f"<ul>{body}</ul>")
            items.clear()

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            flush_para()
            flush_list()
            out.append(render_code(lang, "\n".join(buf)))
            continue

        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            flush_para()
            flush_list()
            level, text = len(heading.group(1)), inline(heading.group(2))
            if level == 1:
                title = text
            else:
                slug = slugify(text)
                toc.append((level, slug, text))
                out.append(f'<h{level} id="{slug}">{text}</h{level}>')
            i += 1
            continue

        if re.match(r"^-\s+", line):
            flush_para()
            items.append([re.sub(r"^-\s+", "", line)])
            i += 1
            continue

        if items and line.startswith("  ") and line.strip():
            items[-1].append(line.strip())
            i += 1
            continue

        if not line.strip():
            flush_para()
            flush_list()
            i += 1
            continue

        flush_list()
        para.append(line.strip())
        i += 1

    flush_para()
    flush_list()
    return title, "\n".join(out), toc


def main() -> int:
    title, body, toc = convert(SRC.read_text(encoding="utf-8"))

    nav = "".join(
        f'<a class="lvl{lvl}" href="#{slug}">{text}</a>' for lvl, slug, text in toc
    )
    light = HtmlFormatter(style="xcode").get_style_defs(".highlight")
    dark_auto = HtmlFormatter(style="github-dark").get_style_defs(
        ':root:not([data-theme="light"]) .highlight'
    )
    dark_forced = HtmlFormatter(style="github-dark").get_style_defs(
        ':root[data-theme="dark"] .highlight'
    )
    plain = re.sub(r"<[^>]+>", "", title)

    DST.write_text(
        TEMPLATE.format(
            title=title,
            plain_title=html.escape(plain),
            lede=html.escape(LEDE),
            nav=nav,
            body=body,
            light=light,
            dark_auto=dark_auto,
            dark_forced=dark_forced,
        ),
        encoding="utf-8",
    )
    print(f"wrote {DST} ({DST.stat().st_size} bytes, {len(toc)} headings)")
    return 0


TEMPLATE = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{plain_title}</title>
<meta name="description" content="{lede}">
<meta property="og:title" content="{plain_title}">
<meta property="og:description" content="{lede}">
<meta property="og:type" content="article">
<style>
:root {{
  --bg: #fbfaf7;
  --surface: #ffffff;
  --fg: #1c1b19;
  --muted: #65635d;
  --rule: #e4e1d9;
  --accent: #9a3412;
  --accent-soft: #fdf0e7;
  --code-bg: #f5f3ee;
  --shadow: 0 1px 2px rgba(28,27,25,.06), 0 8px 24px rgba(28,27,25,.05);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #14151a;
    --surface: #1a1c22;
    --fg: #e6e4df;
    --muted: #9a978f;
    --rule: #2b2e36;
    --accent: #f0a06a;
    --accent-soft: #241a14;
    --code-bg: #15171c;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.3);
  }}
}}
:root[data-theme="dark"] {{
  --bg: #14151a;
  --surface: #1a1c22;
  --fg: #e6e4df;
  --muted: #9a978f;
  --rule: #2b2e36;
  --accent: #f0a06a;
  --accent-soft: #241a14;
  --code-bg: #15171c;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.3);
}}

* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; scroll-padding-top: 2rem; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 17px/1.68 ui-serif, Charter, "Iowan Old Style", Georgia, serif;
  -webkit-text-size-adjust: 100%;
}}

.wrap {{
  max-width: min(100% - 2.5rem, 46rem);
  margin: 0 auto;
  padding: 3.5rem 0 6rem;
}}

header.masthead {{ border-bottom: 1px solid var(--rule); padding-bottom: 2rem; margin-bottom: 2.5rem; }}
.kicker {{
  font: 600 12px/1 ui-sans-serif, system-ui, sans-serif;
  letter-spacing: .14em; text-transform: uppercase; color: var(--accent);
  margin-bottom: 1.1rem;
}}
h1 {{ font-size: clamp(2rem, 1.3rem + 2.6vw, 2.9rem); line-height: 1.14; margin: 0 0 1rem; letter-spacing: -.02em; }}
.lede {{ color: var(--muted); font-size: 1.12rem; line-height: 1.6; margin: 0; max-width: 38rem; }}

nav.toc {{
  background: var(--surface); border: 1px solid var(--rule); border-radius: 10px;
  padding: 1.1rem 1.3rem; margin: 0 0 3rem; box-shadow: var(--shadow);
}}
nav.toc strong {{
  display: block; font: 600 11px/1 ui-sans-serif, system-ui, sans-serif;
  letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: .85rem;
}}
nav.toc a {{
  display: block; padding: .22rem 0; color: var(--fg); text-decoration: none;
  font: 15px/1.45 ui-sans-serif, system-ui, sans-serif;
  border-left: 2px solid transparent; padding-left: .7rem; transition: border-color .15s, color .15s;
}}
nav.toc a:hover {{ color: var(--accent); border-left-color: var(--accent); }}
nav.toc a.lvl3 {{ padding-left: 1.7rem; font-size: 14px; color: var(--muted); }}

h2 {{
  font-size: 1.6rem; line-height: 1.22; letter-spacing: -.015em;
  margin: 3.4rem 0 1rem; padding-top: 1.4rem; border-top: 1px solid var(--rule);
}}
h3 {{ font-size: 1.18rem; line-height: 1.3; margin: 2.3rem 0 .8rem; }}
h2:first-of-type {{ border-top: none; padding-top: 0; }}

p {{ margin: 0 0 1.15rem; }}
ul {{ margin: 0 0 1.35rem; padding-left: 1.25rem; }}
li {{ margin-bottom: .55rem; }}
strong {{ font-weight: 650; }}
a {{ color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 2px; }}

code {{
  font: 0.88em/1.5 ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
  background: var(--accent-soft); padding: .12em .38em; border-radius: 4px;
  overflow-wrap: break-word;
}}

.highlight {{
  background: var(--code-bg) !important;
  border: 1px solid var(--rule); border-radius: 10px;
  padding: 1rem 1.15rem; margin: 0 0 1.6rem;
  overflow-x: auto; box-shadow: var(--shadow);
}}
.highlight pre {{ margin: 0; background: none !important; }}
.highlight pre, .highlight code {{
  font: 13.5px/1.62 ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
  background: none; padding: 0; border-radius: 0;
}}

footer {{
  margin-top: 4rem; padding-top: 1.5rem; border-top: 1px solid var(--rule);
  color: var(--muted); font: 14px/1.6 ui-sans-serif, system-ui, sans-serif;
}}

#theme {{
  position: fixed; top: 1rem; right: 1rem; z-index: 10;
  width: 2.3rem; height: 2.3rem; border-radius: 50%;
  border: 1px solid var(--rule); background: var(--surface); color: var(--fg);
  cursor: pointer; font-size: 1rem; line-height: 1; box-shadow: var(--shadow);
}}

@media (max-width: 640px) {{
  body {{ font-size: 16px; }}
  .wrap {{ padding-top: 2.5rem; }}
  .highlight {{ border-radius: 8px; padding: .85rem .9rem; }}
}}

{light}
@media (prefers-color-scheme: dark) {{
{dark_auto}
}}
{dark_forced}
</style>
</head>
<body>
<button id="theme" type="button" aria-label="Przełącz motyw">◑</button>
<div class="wrap">
<header class="masthead">
  <div class="kicker">Change Data Capture · Go</div>
  <h1>{title}</h1>
  <p class="lede">{lede}</p>
</header>
<nav class="toc"><strong>W tym tekście</strong>{nav}</nav>
<main>
{body}
</main>
<footer>
  Kod w artykule: <a href="https://github.com/go-mysql-org/go-mysql">go-mysql-org/go-mysql</a>
  i <a href="https://github.com/warpstreamlabs/bento">warpstreamlabs/bento</a>.
</footer>
</div>
<script>
(function () {{
  var root = document.documentElement;
  var saved = localStorage.getItem("theme");
  if (saved) root.setAttribute("data-theme", saved);
  document.getElementById("theme").addEventListener("click", function () {{
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") &&
       window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  }});
}})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
