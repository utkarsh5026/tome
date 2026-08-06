# CLAUDE.md — tome

A local web reader for a repo's markdown. One Python file, no dependencies, no
network, no build step. See `README.md` for the user-facing story.

## ⚠️ The three constraints that define this project

Everything else is negotiable. These are not:

1. **Standard library only.** `dependencies = []` in `pyproject.toml` is a
   promise, and CI enforces it by running the tests with no install step. If a
   change wants a library, the answer is to write the 40 lines or drop the
   feature — that is the whole reason `tome` installs in a second and runs on
   any machine with Python.
2. **One file.** `tome.py` is the program: parser, highlighter, server, and page.
   Someone can vendor it into a repo by copying it. Don't split it into a package.
3. **Python 3.9+.** No `match`, no `tomllib` (hence JSON config), no `X | Y` at
   runtime — annotations are fine, the file has `from __future__ import annotations`.
   CI runs 3.9 and 3.13 on Linux, macOS, and Windows.

Loopback-only is a fourth: the server binds `127.0.0.1` and there is no flag to
change that. It serves file contents out of someone's working tree.

## Layout

`tome.py` reads top to bottom in banner-comment sections. Work inside the right one:

| section | what lives there |
|---|---|
| **Configuration** | `Config`, `find_root`, `load_config`, `configure` — everything `.tome.json` can set |
| **Discovery** | `build_tree` and friends: walking, grouping, labelling, kind detection |
| **Syntax highlighting** | `LangSpec`, the keyword tables, `highlight` |
| **Markdown → HTML** | the `Markdown` class — one instance per rendered doc |
| **Documents** | `safe_path`, `_is_secret`, `render_doc`, `search` |
| **Server** | `Handler`, CLI parsing, `main`, `cli` |
| **The page** | `page_html`, `FAVICON`, and `PAGE` — the whole inline HTML/CSS/JS |

`test_tome.py` is stdlib `unittest`, ~40 tests, most of which build a throwaway
repo on disk with `make_repo({...})` and point tome at it. Add tests there; they
run in under a second, so there is no excuse.

## Commands

```bash
python3 -m unittest          # the suite (no install, no deps)
python3 -m unittest -v       # …with names
uvx ruff check .             # lint — CI runs exactly this
uv tool install . --force    # install the `tome` CLI from the working tree
python3 tome.py --root ~/somewhere --port 7979   # run without installing
```

**Never run `ruff format`.** The file is hand-formatted: the language tables,
the `MIME`/`EDITORS` dicts, and the CSS/JS inside `PAGE` are laid out to be read
in columns, and the formatter destroys that. CI lints only, deliberately.

## Conventions

- **Adding a `.tome.json` key** touches four places, in this order: a field on
  `Config`, a parse line in `load_config` (defensive — a broken config warns and
  is ignored, never raises), a test in `TestConfig`, and the table in `README.md`.
  If the page needs it, add it to the `settings` dict in `page_html` too.
- **The page is a template with three slots** — `{{TITLE}}`, `{{BRAND}}`,
  `{{FAVICON}}`, and `{{SETTINGS}}`. Server-side values reach the JS through the
  `TOME` object in `{{SETTINGS}}` (JSON, with `<` escaped so a path can't close
  the script tag). Don't add a fifth slot when a `TOME` field will do.
- **localStorage is namespaced by helper**: `K("x")` for preferences that should
  follow the user across repos (theme, panel layout), `RK("x")` for state that
  belongs to one repo (last doc, expanded sections). Pick deliberately.
- **The markdown parser is not CommonMark** and must not grow toward it. It
  handles what repo docs actually contain. The rule when it meets something
  unknown: degrade to a paragraph, never mangle the page. `blocks()` must always
  make progress — `_paragraph` is the branch of last resort and always consumes
  at least one line.
- **The highlighter is a tokenizer, not a parser.** It will get edge cases wrong;
  that is accepted. Adding a language means adding a `LangSpec` and an alias, not
  special-casing `highlight`.
- **Never widen `safe_path`.** It resolves inside `ROOT`, refuses `skip_dirs`,
  and refuses secrets via `_is_secret`. New secret patterns go in `_is_secret`
  with a test in `TestSafePath`.
- Errors that reach a user are plain sentences on stderr with a next action
  (`hint:` / `warning:`), never tracebacks.

## The website

<https://utkarsh5026.github.io/tome/> is **README.md rendered by tome itself** —
`site/build.py` imports the module, runs the README through the same `Markdown`
class the server uses, and lifts the stylesheet straight out of `PAGE`. Two
consequences worth protecting:

- **There is no second copy of the content.** Edit `README.md` and the site
  changes. Never add prose to `build.py` that belongs in the README.
- **The page is a live demo.** If the renderer breaks a table or the highlighter
  mangles Rust, it breaks in public. That is the point — don't work around a
  rendering bug in the build script, fix the renderer.

```bash
python3 site/build.py --serve   # build + preview on :8000
```

The `site` CI job builds it on every push and PR; the `deploy` job publishes to
Pages, but only from `main` and only after `test`, `lint`, `install`, and `site`
are all green. Pages is configured with `build_type: workflow` — there is no
Jekyll and no `gh-pages` branch, and `site/_site/` is generated, never committed.

Two coupling points to keep in mind when editing: `build.py` strips the README's
leading H1 and tagline because the hero restates them (it warns if the README
stops opening that way), and it rewrites `/raw?p=` image URLs and `#/` doc links,
which only exist inside the running app, into plain relative and GitHub paths.

## Releasing to PyPI

The package is published as `tome-docs` (the `tome` name on PyPI was already
taken). `.github/workflows/publish.yml` builds and publishes on any tag matching
`v*.*.*`, using PyPI trusted publishing (OIDC) — there is no API token in this
repo's secrets, and there should never be one.

To cut a release: bump `__version__` in `tome.py` (the single source hatchling
reads via `[tool.hatch.version]`), commit, then tag and push:

```bash
git tag v0.1.1
git push origin v0.1.1
```

The workflow refuses to publish if the tag doesn't match `__version__`, so a
forgotten version bump fails CI instead of shipping the wrong version.

One-time setup on PyPI's side, required before the first tag is pushed: since
`tome-docs` doesn't exist as a PyPI project yet, register a **pending
publisher** at <https://pypi.org/manage/account/publishing/> — project name
`tome-docs`, repo `utkarsh5026/tome`, workflow `publish.yml`, environment
`pypi`. PyPI creates the project automatically on the first successful publish
from that pending publisher. Until this is registered, the workflow's `publish`
job will fail with an authentication error — the `build` job works regardless,
since it doesn't touch PyPI.

## Regenerating the screenshots

`assets/*.png` are real captures, not mockups. Headless Chrome can't press keys,
so states like the ⌘K palette come from a **scratch copy** of `tome.py` with a
small bootstrap appended — the pixels are genuine, only the trigger is synthetic.
The harness must never land in the shipped file.

```bash
# 1. stamp the harness into a scratch copy, serve a doc-rich repo with it
python3 tools/shots/inject.py /tmp/tome_shot.py
cd ~/projects/backend-gauntlet && python3 /tmp/tome_shot.py --port 7970 &

# 2. capture (Windows Chrome from WSL; --screenshot needs a Windows path)
tools/shots/capture.sh reading '/#/projects/02-rate-limiter/docs/00-token-bucket.md' 1600,1150
tools/shots/capture.sh search  '/?shot=search&q=backpressure#/projects/10-api-gateway/SPEC.md'
tools/shots/capture.sh themes  '/?shot=themes#/projects/10-api-gateway/SPEC.md'
tools/shots/capture.sh source  '/#/projects/10-api-gateway/src/error.rs'
```

`?shot=search|themes` drives the UI; `?y=N` scrolls. Two traps: the page's
`scroll-behavior: smooth` freezes a capture mid-animation (the harness forces
`auto`), and scrolling far under a virtual-time budget can produce a blank
frame — prefer picking a document whose *top* shows what you want over scrolling
to it.

## Style

- Match the surrounding code. Comments explain *why*, and the existing ones set
  the register: dry, specific, no restating the code.
- Docstrings on anything non-obvious; one line where one line does.
- Keep `main`/`cli` thin — argument parsing in `build_parser`, work in functions
  that are callable from tests without a server.
