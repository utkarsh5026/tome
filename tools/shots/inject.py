#!/usr/bin/env python3
"""Stamp a screenshot-only bootstrap into a scratch copy of tome.

Headless Chrome's `--screenshot` cannot press keys, so this drives the *real*
UI into the state a keypress would produce. It lives here and never in the
shipped `tome.py` — the pixels in `assets/` are genuine, only the trigger is
synthetic.

    python3 tools/shots/inject.py /tmp/tome_shot.py
    cd ~/some/doc-rich/repo && python3 /tmp/tome_shot.py --port 7970

Then drive it with query params: `?shot=search&q=…`, `?shot=themes`, `?y=N`.
See CLAUDE.md for the capture command.
"""

import pathlib
import shutil
import sys

SRC = pathlib.Path(__file__).resolve().parents[2] / "tome.py"

HARNESS = """<script>
/* SCREENSHOT HARNESS — not part of tome. */
(async () => {
  const p = new URLSearchParams(location.search);
  const shot = p.get("shot");
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  /* Smooth scrolling + a virtual-time budget = a capture frozen mid-animation. */
  document.documentElement.style.scrollBehavior = "auto";
  await wait(900);
  if (shot === "search") {
    palette(true);
    const box = document.querySelector("#q");
    box.value = p.get("q") || "backpressure";
    paletteRender(box.value);
    await wait(1200);
  } else if (shot === "themes") {
    themePop(true);
    await wait(500);
  }
  if (p.get("y")) {
    window.scrollTo({ top: parseInt(p.get("y"), 10), behavior: "instant" });
    await wait(1400);
  }
  document.title = "READY";
})();
</script>
</body>
</html>
\"\"\""""


def main() -> int:
    dst = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "tome_shot.py")
    shutil.copy(SRC, dst)
    text = dst.read_text(encoding="utf-8")
    marker = '</body>\n</html>\n"""'
    if text.count(marker) != 1:
        print("error: page tail not found — did PAGE change?", file=sys.stderr)
        return 1
    dst.write_text(text.replace(marker, HARNESS), encoding="utf-8")
    print(f"harness stamped into {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
