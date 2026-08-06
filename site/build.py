#!/usr/bin/env python3
"""Build the GitHub Pages site — by rendering README.md with tome itself.

The landing page for a markdown reader ought to be rendered by that reader,
and this one is: it imports `tome`, runs `README.md` through the same
`Markdown` class the server uses, and reuses the same stylesheet. Two things
fall out of that, both of which matter more than the novelty:

  * the site cannot drift from the README — there is no second copy of the
    content to keep in sync, so updating the README updates the site;
  * the page is a live demonstration. If the renderer breaks a table or the
    highlighter mangles Rust, it breaks here in public.

    python3 site/build.py            # → site/_site/
    python3 site/build.py --serve    # …and preview it on :8000

Stdlib only, like everything else here.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import shutil
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tome  # noqa: E402  (must follow the sys.path insert)

OUT = ROOT / "site" / "_site"
REPO_URL = "https://github.com/utkarsh5026/tome"


def css() -> str:
    """tome's own stylesheet, lifted out of its page template.

    Extracted rather than copied so the site restyles itself whenever the
    reader does — there is no second stylesheet to forget about.
    """
    start = tome.PAGE.index("<style>") + len("<style>")
    return tome.PAGE[start : tome.PAGE.index("</style>")]


def render_readme() -> tuple[str, list[dict]]:
    """README.md → (html, toc), with in-app link forms rewritten for a static host."""
    md = tome.Markdown("README.md")
    html = md.render((ROOT / "README.md").read_text(encoding="utf-8"))

    # tome serves images through its own endpoint and routes doc links through
    # a hash router. Neither exists on a static host, so point them at files.
    html = html.replace('src="/raw?p=', 'src="')
    html = re.sub(r'href="#/([^"]+)"', rf'href="{REPO_URL}/blob/main/\1"', html)
    # The hero restates the README's H1 and its one-line tagline, so drop them
    # here instead of printing each twice.
    stripped = re.sub(r"^<h1 .*?</h1>\s*(?:<p>.*?</p>\s*)?", "", html, count=1, flags=re.DOTALL)
    if stripped == html:
        print("warning: README no longer opens with an H1 + tagline", file=sys.stderr)
    return stripped, md.toc


def toc_html(toc: list[dict]) -> str:
    items = "".join(
        f'<a class="{"l3" if t["level"] == 3 else ""}" href="#{t["id"]}">'
        f"{tome._esc(t['text'])}</a>"
        for t in toc
    )
    return f'<div class="th">on this page</div>{items}'


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tome — read any repo's markdown in a browser tab</title>
<meta name="description" content="{description}">
<meta property="og:title" content="tome">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<meta property="og:image" content="{repo}/raw/main/assets/reading.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{favicon}">
<style>{css}
  /* ── page-specific: the hero, and a few tweaks for a one-page document ──
     The hero lives *inside* <article>, so it inherits the reading column's
     width and left edge for free — nothing to keep in sync with .wrap. */
  .hero{{padding:26px 0 40px}}
  .hero h1{{font-size:46px;line-height:1.1;margin:0 0 14px;letter-spacing:-1px;
    font-weight:680;color:var(--strong)}}
  .hero .tag{{font-size:19px;color:var(--sub);max-width:60ch;margin:0 0 26px;line-height:1.55}}
  .hero .tag b{{color:var(--text);font-weight:600}}
  .cta{{display:flex;gap:11px;flex-wrap:wrap;align-items:center}}
  .cta a,.cta button{{border:1px solid var(--line2);border-radius:10px;padding:9px 16px;
    font:13.5px var(--sans);text-decoration:none;color:var(--text);background:var(--panel);
    cursor:pointer}}
  .cta a:hover,.cta button:hover{{border-color:var(--blue);color:var(--blue)}}
  .cta .prime{{background:var(--blue);border-color:var(--blue);color:var(--bg);font-weight:600}}
  .cta .prime:hover{{color:var(--bg);opacity:.9}}
  .cta code{{font-family:var(--mono);font-size:12.5px;color:var(--green);background:none;
    border:0;padding:0}}
  .pill{{display:inline-flex;gap:7px;align-items:center;font:11.5px var(--mono);
    color:var(--dim);border:1px solid var(--line);border-radius:999px;padding:4px 11px;
    margin-bottom:20px}}
  .pill i{{width:6px;height:6px;border-radius:50%;background:var(--green);display:block}}
  footer{{border-top:1px solid var(--line);margin-top:70px;padding:26px 30px 60px;
    color:var(--dim);font-size:12.5px;text-align:center}}
  footer a{{color:var(--sub)}}
  /* A landing page wants wide screenshots but still-readable prose, so the
     column runs full width and only the running text is measure-capped. */
  .wrap{{padding-top:14px}}
  article{{max-width:none}}
  article p,article ul,article ol,article blockquote{{max-width:76ch}}
  article img{{border:1px solid var(--line);margin:14px 0 22px}}
  article table{{font-size:13px}}
  @media(max-width:820px){{ .hero{{padding:40px 20px 0}} .hero h1{{font-size:34px}} }}
</style>
</head>
<body>
<div class="app nonav">
  <main>
    <div class="bar">
      <div class="crumb"><b>tome</b> <span style="color:var(--dim)">· a markdown reader</span></div>
      <span class="sp"></span>
      <button id="copy">copy install</button>
      <a class="btn" href="{repo}">GitHub</a>
    </div>

    <div class="wrap">
      <article>
        <div class="hero">
          <span class="pill"><i></i>v{version} · stdlib only · MIT</span>
          <h1>Read any repo's markdown in a browser tab.</h1>
          <p class="tag">One Python file. <b>Zero dependencies</b>, no build step, no
             network. Point it at a repo and it finds every doc, groups them the way
             the repo is laid out, and live-reloads as you edit.</p>
          <div class="cta">
            <a class="prime" href="#install">Install</a>
            <button id="copy2"><code>uv tool install git+{repo}</code></button>
          </div>
        </div>
        {body}
      </article>
      <div class="toc">{toc}</div>
    </div>

    <footer>
      Built by rendering <a href="{repo}/blob/main/README.md">README.md</a> with tome itself —
      the page you're reading is the renderer's own output.<br>
      MIT © Utkarsh Priyadarshi · <a href="{repo}">source</a>
    </footer>
  </main>
</div>

<script>
/* Copy buttons: the ones tome emits on every code block, plus the two in the
   header. tome wires these in its app JS; this page has to do it itself. */
const INSTALL = "uv tool install git+{repo}";
function flash(el, text) {{
  const was = el.textContent;
  el.textContent = text;
  setTimeout(() => {{ el.textContent = was; }}, 1100);
}}
for (const id of ["copy", "copy2"]) {{
  document.getElementById(id).onclick = (e) => {{
    navigator.clipboard.writeText(INSTALL);
    flash(e.currentTarget, "copied ✓");
  }};
}}
document.querySelectorAll(".cb .copy").forEach(btn => {{
  btn.onclick = () => {{
    navigator.clipboard.writeText(btn.parentElement.querySelector("code").innerText);
    flash(btn, "copied");
  }};
}});
</script>
</body>
</html>
"""


def build() -> Path:
    tome.configure(tome.load_config(ROOT))
    body, toc = render_readme()

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copytree(ROOT / "assets", OUT / "assets")
    # Tell GitHub Pages not to run the output through Jekyll, which would eat
    # any path starting with an underscore.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    (OUT / "index.html").write_text(
        PAGE.format(
            css=css(),
            body=body,
            toc=toc_html(toc),
            repo=REPO_URL,
            version=tome.__version__,
            favicon=tome.FAVICON.format(icon="📖"),
            description="Read any repo's markdown in a browser tab. "
            "One Python file, zero dependencies.",
        ),
        encoding="utf-8",
    )
    return OUT


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build the GitHub Pages site.")
    ap.add_argument("--serve", action="store_true", help="preview on :8000 after building")
    args = ap.parse_args(argv)

    out = build()
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"built {out} ({size / 1024:.0f} KB)")

    if args.serve:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
        with socketserver.TCPServer(("127.0.0.1", 8000), handler) as srv:
            print("preview → http://127.0.0.1:8000/  (ctrl-C to stop)", flush=True)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
