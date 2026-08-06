# Linking, Source Following & Search

The other two docs in this set exercise the markdown parser and the
highlighter. This one is for the parts of `tome` that aren't visible in a
single rendered page: navigation between documents, following a link out to
source, and search.

## Following links into source

Markdown links that point at a non-`.md` file open that file in the same
reader, syntax-highlighted by the same [tokenizer](02-code-highlighting.md)
used for fenced code:

- [`tome.py`](../tome.py) — the whole server, parser, and page template.
- [`test_tome.py`](../test_tome.py) — `make_repo({...})`-style tests.
- [`pyproject.toml`](../pyproject.toml) — the `dependencies = []` promise.
- [`.github/workflows/publish.yml`](../.github/workflows/publish.yml) — the
  trusted-publishing release workflow.

Each of those should render as a `.src`-styled link (not `.doc`) and, once
clicked, show the real file contents in the reader pane.

## Jumping between docs

- [Back to the markdown tour](01-markdown-tour.md)
- [Over to the code highlighting tour](02-code-highlighting.md)
- [Straight to a heading in this repo's README](../README.md#install)

## Search (⌘K / Ctrl-K)

Opening the search palette and typing should surface this file two ways:

1. **By title** — typing `linking` or `search` matches this doc's heading.
2. **By content** — typing something that only appears in a paragraph, like
   `trusted-publishing`, should still find this page via `/api/search`.

A few phrases planted here specifically to make search worth trying:

- `token bucket` (also appears in the Rust and Python samples in the code tour)
- `loopback-only`, the constraint that keeps `tome`'s server bound to
  `127.0.0.1` and off the network entirely
- `zero-dependency`

## What "working" looks like

If you're using this doc set to sanity-check a change to `tome.py`, a quick
pass looks like:

- [x] the sidebar shows a `docs` section with three entries, in numeric order
- [x] headings from each doc populate the right-hand table of contents
- [x] the mermaid diagram in the markdown tour renders once the browser
      fetches it
- [x] clicking `tome.py` or `test_tome.py` above opens real source, highlighted
- [ ] search finds this doc by both title and body text
- [ ] the intentionally-broken link in the markdown tour renders without
      crashing the page
