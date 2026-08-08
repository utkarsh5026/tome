# CLAUDE.md — tome

A local web reader for a repo's markdown. One Python file, no dependencies, no
network. Nothing to build to *use* it — the only generated thing in the repo is
the `PAGE` constant, inlined from `web/` by `make page`. See `README.md` for the
user-facing story.

## ⚠️ The three constraints that define this project

Everything else is negotiable. These are not:

1. **Standard library only.** `dependencies = []` in `pyproject.toml` is a
   promise, and CI enforces it by running the tests with no install step. If a
   change wants a library, the answer is to write the 40 lines or drop the
   feature — that is the whole reason `tome` installs in a second and runs on
   any machine with Python.
2. **One file.** `tome.py` is the program: parser, highlighter, server, and page.
   Someone can vendor it into a repo by copying it, curl it into `~/.local/bin`,
   or hand it to PyInstaller. Don't split it into a package. The frontend is the
   one exception and it is not a real one — `web/app.css` and `web/app.js` are
   editing conveniences that get inlined straight back into `tome.py`, which
   remains the whole program.
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
| **Git metadata** | `git_info`, `_last_change`, `doc_git` — one `git log` for the whole repo, cached |
| **Syntax highlighting** | `LangSpec`, the keyword tables, `highlight` |
| **Markdown → HTML** | the `Markdown` class — one instance per rendered doc |
| **Formats** | `Format`, `FORMATS`, `DOC_SUFFIXES`, front matter, the org translator, the notebook renderer |
| **Documents** | `safe_path`, `_is_secret`, `render_doc`, `tree_payload`, `search`, `link_index`/`backlinks` |
| **Export** | `export`, `_bundle` — every doc baked into one HTML file, no server behind it |
| **Server** | `Handler`, CLI parsing, `main`, `cli` |
| **The page** | `page_html`, `FAVICON`, and `PAGE` — the inline HTML. **The CSS and the app JS inside `PAGE` are generated; edit `web/app.css` and `web/app.js`.** |

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
make page                    # after editing web/app.css or web/app.js
make binary                  # standalone executable for this platform
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
- **Adding a document format** means one `Format` entry in `FORMATS` and nothing
  else — discovery, sidebar titles, link classing, search, the backlink index,
  and `render_doc` all read from `DOC_SUFFIXES`. Never hard-code a suffix
  outside that table; the `.markdown` files that were discovered but rendered as
  source code for two releases are what that rule is for. A format's `title` and
  `meta` callables run for every doc on every tree build, so they read a bounded
  prefix or they cache (see `_ipynb_title`, which has to do the latter).
  `links` is handed whatever `text` returns, which is why the notebook needs no
  link reader of its own and org — whose `text` is org — does.
- **Front matter is not YAML and must not grow into it.** `_front_matter`
  reads `key: value`, `[a, b]`, and `- a` lists; everything else is skipped,
  and a block that never closes is a horizontal rule, not metadata. Adding a
  key means a reader (`meta_str`/`meta_list`/`meta_bool`/`meta_int`), a field on
  `Doc` if the sidebar needs it, `tree_payload`, and the README.
- **`tree_payload` and `page_html` are shared with the exporter.** A field the
  server sends that the bundle doesn't (or the reverse) is a bug the tests catch
  — the whole point is that the page cannot tell which one it is talking to. New
  per-doc data goes in `render_doc`, which both go through.
- **Anything cached about the repo gets cleared in `configure`.** `_GIT`,
  `_INDEX`, and `_NB_TITLES` are caches about *this* root, and `configure` is
  the only moment it can become a different one.
- **A new format does not get its own parser.** Org is translated into markdown
  and fed to `Markdown`; a notebook's cells are handed to it directly. That is
  what keeps the rule above honest — a second parser is how the first one starts
  growing toward CommonMark, and it would drift from it besides.
- **The highlighter is a tokenizer, not a parser.** It will get edge cases wrong;
  that is accepted. Adding a language means adding a `LangSpec` and an alias, not
  special-casing `highlight`.
- **Never widen `safe_path`.** It resolves inside `ROOT`, refuses `skip_dirs`,
  and refuses secrets via `_is_secret`. New secret patterns go in `_is_secret`
  with a test in `TestSafePath`.
- Errors that reach a user are plain sentences on stderr with a next action
  (`hint:` / `warning:`), never tracebacks.

## The frontend lives in `web/`

`PAGE` is ~890 lines, and 830 of them are CSS and JavaScript. Inside a Python
string an editor cannot highlight them, a formatter cannot touch them, and no
linter will ever look at them. So they are edited as real files and inlined:

```bash
$EDITOR web/app.css      # 383 lines, two-space indent re-applied on inline
$EDITOR web/app.js       # 445 lines
make page                # rewrites the PAGE constant in tome.py
```

**Commit `tome.py` along with whatever you changed in `web/`.** `tome.py` is
still the whole program — the thing people curl, vendor, and freeze — so a
change that only exists in `web/` has not shipped. The `page` job in `ci.yml`
and a `--check` in `publish.yml` both fail on drift, the second so a stale UI
can't reach PyPI and all five binaries.

The catch worth knowing: editing the CSS or JS *inside* `tome.py` appears to
work, and then the next `make page` silently reverts it. That is what the CI
check is for.

Two things stay inline in `PAGE` on purpose:

- **The head bootstrap** (`<script>` in `<head>`) — it carries the
  `{{SETTINGS}}` slot and sets the theme before first paint. A Python
  placeholder inside a `.js` file would break the tooling this exists for.
- **The HTML itself** — it is the template, slots and all.

`tools/build_page.py` anchors on the single `<style>` and the *last* of exactly
two `<script>` tags, and refuses to run if it finds a third rather than guessing.

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

## Releasing

One tag ships two things. `.github/workflows/publish.yml` fires on any tag
matching `v*.*.*` and runs both `publish` (the wheel to PyPI) and
`binaries` → `release` (standalone executables to the GitHub Release).

The package is published as `tome-docs` (the `tome` name on PyPI was already
taken), using PyPI trusted publishing (OIDC) — there is no API token in this
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

### The binaries

PyInstaller, one job per target OS, because it cannot cross-compile. `make
binary` builds the same thing locally for whatever machine you're on; keep its
flags in step with the workflow, which is the one that actually ships.

Four things about that matrix are load-bearing, and all four are cheap to break:

- **`fail-fast: false`.** The default cancels every sibling the moment one
  fails, throwing away four good binaries because one runner was flaky.
- **The excludes stay timid.** `http.server` imports `email` to parse headers,
  so excluding it produces a binary that starts fine, prints `--version` fine,
  and dies on the first request. That exact mistake is why the smoke test
  serves a page instead of just checking `--version`.
- **The smoke test reads the port out of tome's own banner.** `_bind()` walks
  forward when a port is busy, so asking for 7979 and curling 7979 can test a
  different process entirely.
- **`ubuntu-22.04`, not `24.04`.** The build host sets the glibc floor.

`release` deliberately depends on `binaries` alone, not on `publish` — until the
pending publisher above is registered, `publish` fails, and the binaries have no
reason to go down with it.

The binaries are unsigned. macOS Gatekeeper and Windows SmartScreen both warn on
first run, and the README tells users how to get past it. Fixing that properly
means an Apple Developer account ($99/yr) for notarization and a code-signing
certificate for Windows; both are additive later and neither blocks a release.

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
