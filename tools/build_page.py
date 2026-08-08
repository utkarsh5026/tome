#!/usr/bin/env python3
"""Inline `web/app.css` and `web/app.js` into the PAGE constant in `tome.py`.

`tome.py` stays the one shipped file — it is still the thing you curl into
`~/.local/bin`, vendor into a repo, or hand to PyInstaller. But the ~830 lines
of frontend that live inside it are edited as real files, where an editor knows
they are CSS and JavaScript and a linter can be pointed at them.

So `tome.py` is generated in exactly one small respect, and the `page` job in
CI runs `--check` to make sure nobody edits the copy inside `tome.py` and
watches it get silently overwritten later.

    python3 tools/build_page.py           # regenerate tome.py from web/
    python3 tools/build_page.py --check   # exit 1 if tome.py is out of date

The head bootstrap stays inline and is deliberately not extracted: it carries
the `{{SETTINGS}}` slot, and a Python template placeholder inside a .js file
would break the very tooling this exists to enable.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOME = ROOT / "tome.py"
CSS = ROOT / "web" / "app.css"
JS = ROOT / "web" / "app.js"

CSS_INDENT = "  "  # the stylesheet sits two spaces in; the script does not


def _slice(page: str, open_tag: str, close_tag: str, *, last: bool) -> tuple[int, int]:
    """Bounds of the text between `open_tag` and `close_tag`, exclusive."""
    start = page.rfind(open_tag) if last else page.find(open_tag)
    if start < 0:
        raise SystemExit(f"error: no {open_tag} in PAGE — has the page been restructured?")
    start += len(open_tag)
    end = page.find(close_tag, start)
    if end < 0:
        raise SystemExit(f"error: {open_tag} is never closed in PAGE")
    return start, end


def _indent(text: str, pad: str) -> str:
    """Re-apply `pad`, leaving blank lines genuinely blank rather than padded."""
    return "\n".join(pad + ln if ln.strip() else ln for ln in text.split("\n"))


def render() -> str:
    """`tome.py` as it should be, given the current contents of `web/`."""
    src = TOME.read_text(encoding="utf-8")
    head = src.index('PAGE = r"""') + len('PAGE = r"""')
    tail = src.index('"""', head)
    page = src[head:tail]

    if page.count("<script>") != 2:
        raise SystemExit(
            "error: expected exactly two <script> tags in PAGE (the head bootstrap and the app)\n"
            "hint: a third would make 'the last one' the wrong anchor — update tools/build_page.py"
        )

    css = _indent(CSS.read_text(encoding="utf-8").rstrip("\n"), CSS_INDENT)
    js = JS.read_text(encoding="utf-8").rstrip("\n")

    # Replace the script first: it sits after the style, so its offsets survive.
    for body, (open_tag, close_tag, last) in (
        (js, ("<script>", "</script>", True)),
        (css, ("<style>", "</style>", False)),
    ):
        a, b = _slice(page, open_tag, close_tag, last=last)
        page = f"{page[:a]}\n{body}\n{page[b:]}"

    return src[:head] + page + src[tail:]


def main(argv: list[str]) -> int:
    check = "--check" in argv
    want = render()
    if TOME.read_text(encoding="utf-8") == want:
        if not check:
            print("tome.py already matches web/ — nothing to do")
        return 0
    if check:
        print(
            "error: tome.py does not match web/app.css and web/app.js",
            file=sys.stderr,
        )
        print("hint: run `make page` and commit the result", file=sys.stderr)
        return 1
    TOME.write_text(want, encoding="utf-8")
    print(f"inlined web/app.css + web/app.js into {TOME.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
