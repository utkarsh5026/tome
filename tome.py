#!/usr/bin/env python3
"""tome — read any repo's markdown in a browser tab.

A zero-dependency local web reader for every markdown file in a repository, so
you can keep the docs open in one browser tab and code in the other window
instead of juggling editor splits.

  * DISCOVERS  ← every document under the repo — markdown, org-mode, Jupyter
                 notebooks — grouped the way the repo is laid out (monorepo
                 packages become their own sections).
  * RENDERS    ← markdown → HTML in-process (no pip deps, no CDN, no network),
                 including GFM tables, task lists, fenced code with syntax
                 highlighting, and relative links between docs. The other
                 formats reach that same renderer rather than growing a second.
  * FOLLOWS    ← links to source files (`[router.rs](../src/router.rs)`) open
                 the real file, syntax-highlighted, in the same reader.
  * RELOADS    ← polls mtimes; a doc you edit re-renders in place, scroll kept.
  * SEARCHES   ← ⌘K / ctrl-K jumps to a doc or greps inside all of them.

Usage:
    tome                     # serve the repo you are standing in
    tome --open              # …and launch a browser tab
    tome --port 9000         # pick a port (auto-advances if one is taken)
    tome docs/adr            # open straight to a path
    tome 10                  # or to a numbered package (`projects/10-*`)
    tome --root ~/work/api   # serve a repo you are not standing in

Everything is optional: with no config at all it finds the repo root, groups
what it finds, and serves. Drop a `.tome.json` at the root to override the
title, grouping, or reading order — see `tome --init-config`.

Stdlib only, single file. Binds to loopback only — it serves file contents
from the repo, so it is not for exposing on a network.
"""

from __future__ import annotations

import argparse
import contextlib
import html as html_mod
import json
import os
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import parse_qs, urlparse

__version__ = "0.1.1"

CONFIG_NAME = ".tome.json"

# Files that mark the top of a project. Checked in order of how strongly they
# mean "this is the root", so a package inside a monorepo doesn't win over the
# repository that contains it.
ROOT_MARKERS = (".git", ".hg", ".jj", CONFIG_NAME)
WEAK_MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile")

# Directories that never hold docs worth reading (and would swamp the tree).
DEFAULT_SKIP_DIRS = {
    "node_modules", "target", ".git", ".hg", ".jj", "dist", "build", ".sqlx",
    "__pycache__", ".venv", "venv", "env", ".vite", ".next", ".nuxt", ".svelte-kit",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".gradle", "vendor",
    ".terraform", "site-packages", ".cache", "coverage", ".idea", "Pods",
    ".ipynb_checkpoints",
}

# Top-level directories whose *children* are each worth their own sidebar
# section — the monorepo shape. A repo without any of these just groups by
# top-level directory instead.
DEFAULT_GROUP_DIRS = (
    "projects", "packages", "apps", "crates", "services", "libs", "modules",
    "plugins", "examples", "cmd", "workspaces",
)

# Docs with these stems are pinned to the top of their section, in this order.
# Everything else sorts after them: `docs/` files first, then the rest.
DEFAULT_PINNED = (
    "README", "SPEC", "DESIGN", "ARCHITECTURE", "CONCEPTS", "RESEARCH",
    "ROADMAP", "CHANGELOG", "CONTRIBUTING",
)

# Filled in by `configure()` before the server starts.
ROOT = Path.cwd()
CFG: Config
KIND_ORDER: dict[str, int] = {}

# Images are served as bytes; every other file a doc links to that isn't itself
# a document (.rs, .toml, .json, …) is followed and syntax-highlighted in the
# reader. What counts as a document is `DOC_SUFFIXES`, down with the formats.
RAW_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
MIME = {
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon",
}

STATUS_RE = re.compile(r"<!--\s*status:(.*?)-->", re.DOTALL)


def say(msg: str, *, err: bool = False) -> None:
    """`print`, for a stream whose encoding may not cover what we're printing.

    Windows hands back the legacy code page whenever stdout is redirected, and
    there an emoji is a `UnicodeEncodeError` rather than a cosmetic problem —
    `tome > log.txt` used to die on its own startup banner, traceback and all.
    So drop whatever the stream can't represent and print the words.
    """
    stream = sys.stderr if err else sys.stdout
    try:
        print(msg, file=stream, flush=True)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        plain = msg.encode(enc, "ignore").decode(enc)
        # Dropping a leading emoji leaves the line indented by its ghost.
        print("\n".join(" ".join(ln.split()) for ln in plain.splitlines()), file=stream, flush=True)


# --------------------------------------------------------------------------- #
# Configuration — all optional. The defaults are chosen so that the common
# case (`cd somewhere && tome`) needs no config file at all; `.tome.json` only
# exists for repos that want to override a specific decision.
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    root: Path
    title: str = ""
    skip_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_SKIP_DIRS))
    group_dirs: tuple[str, ...] = DEFAULT_GROUP_DIRS
    pinned: tuple[str, ...] = DEFAULT_PINNED
    theme: str = ""
    home: str = ""  # doc to open when there is no hash and no saved position
    editor: str = "vscode"
    icon: str = "📖"  # emoji favicon, so tabs from different repos are distinct

    @property
    def name(self) -> str:
        return self.title or self.root.name

    @property
    def editor_url(self) -> str:
        """URL template for the "edit" button. `{path}` is the absolute path."""
        return EDITORS.get(self.editor.lower(), self.editor if "{path}" in self.editor else "")


# Anything not listed here can still be used by giving a `{path}` template
# directly, e.g. "editor": "myeditor://open?f={path}".
EDITORS = {
    "vscode": "vscode://file/{path}",
    "code": "vscode://file/{path}",
    "vscodium": "vscodium://file/{path}",
    "cursor": "cursor://file/{path}",
    "windsurf": "windsurf://file/{path}",
    "zed": "zed://file/{path}",
    "idea": "idea://open?file={path}",
    "pycharm": "pycharm://open?file={path}",
    "sublime": "subl://open?url=file://{path}",
    "none": "",
}


def find_root(start: Path) -> Path:
    """The top of the repo containing `start`.

    Strong markers (`.git`, an explicit `.tome.json`) win outright. Weak ones
    (`package.json`, `Cargo.toml`) only apply if nothing stronger exists above
    them — otherwise standing in one package of a monorepo would serve just
    that package instead of the whole repo.
    """
    start = start.resolve()
    chain = [start, *start.parents]
    for d in chain:
        if any((d / m).exists() for m in ROOT_MARKERS):
            return d
    for d in chain:
        if any((d / m).exists() for m in WEAK_MARKERS):
            return d
    return start


def load_config(root: Path) -> Config:
    """Read `.tome.json` if present. A broken config warns and is ignored."""
    cfg = Config(root=root)
    path = root / CONFIG_NAME
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        say(f"warning: ignoring {CONFIG_NAME} — {e}", err=True)
        return cfg
    if not isinstance(raw, dict):
        say(f"warning: ignoring {CONFIG_NAME} — expected a JSON object", err=True)
        return cfg

    cfg.title = str(raw.get("title", "") or "")
    cfg.theme = str(raw.get("theme", "") or "")
    cfg.home = str(raw.get("home", "") or "")
    cfg.editor = str(raw.get("editor", "") or cfg.editor)
    cfg.icon = str(raw.get("icon", "") or cfg.icon)
    if isinstance(raw.get("pinned"), list):
        cfg.pinned = tuple(str(x).upper() for x in raw["pinned"])
    if isinstance(raw.get("groupDirs"), list):
        cfg.group_dirs = tuple(str(x) for x in raw["groupDirs"])
    # `skip` adds to the defaults; `skipOnly` replaces them outright.
    if isinstance(raw.get("skip"), list):
        cfg.skip_dirs |= {str(x) for x in raw["skip"]}
    if isinstance(raw.get("skipOnly"), list):
        cfg.skip_dirs = {str(x) for x in raw["skipOnly"]}
    return cfg


def configure(cfg: Config) -> None:
    """Publish the resolved config to the module globals the rest of it reads."""
    global ROOT, CFG, KIND_ORDER
    # ROOT must be fully resolved, because `safe_path` compares it against
    # resolved paths. Windows 8.3 short names (`RUNNER~1`) and macOS's
    # /tmp → /private/tmp symlink both make an unresolved root fail that
    # comparison for every file in the repo.
    cfg.root = cfg.root.resolve()
    ROOT = cfg.root
    CFG = cfg
    KIND_ORDER = {kind: i for i, kind in enumerate(cfg.pinned)}
    KIND_ORDER["doc"] = len(cfg.pinned)
    KIND_ORDER["other"] = len(cfg.pinned) + 1


SAMPLE_CONFIG = """{
  "title": "my-repo",
  "icon": "\ud83d\udcd6",
  "pinned": ["README", "SPEC", "CONCEPTS", "RESEARCH"],
  "groupDirs": ["projects", "packages", "crates"],
  "skip": ["fixtures", "snapshots"],
  "theme": "mocha",
  "home": "README.md",
  "editor": "vscode"
}
"""


# --------------------------------------------------------------------------- #
# Discovery — walk the repo once, group by package, keep it cheap enough to
# redo on every /api/tree call so new docs appear without a restart.
# --------------------------------------------------------------------------- #


@dataclass
class Doc:
    rel: str  # repo-relative posix path
    title: str
    label: str  # what the sidebar shows
    kind: str  # a pinned stem (README, SPEC, …) | doc | other
    group: str  # group id it belongs to


@dataclass
class Group:
    gid: str
    title: str
    num: str = ""  # leading NN- of a numbered package, for the sidebar
    state: str = ""
    prefix: str = ""  # path prefix the group covers, "" for the repo root
    docs: list[Doc] = field(default_factory=list)


def _walk_docs() -> list[Path]:
    """Every document under ROOT, in whatever formats `FORMATS` registers."""
    found: list[Path] = []
    stack = [ROOT]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name not in CFG.skip_dirs:
                    stack.append(e)
            elif e.suffix.lower() in DOC_SUFFIXES:
                found.append(e)
    return found


def _find_stem(d: Path, stem: str, suffixes: tuple[str, ...] = ()) -> Path | None:
    """`d/stem` in whichever format it exists as — markdown first, unless the
    caller narrows it to a format whose spelling it actually understands."""
    for suffix in suffixes or DOC_SUFFIXES:
        p = d / f"{stem}{suffix}"
        if p.is_file():
            return p
    return None


def _first_heading(path: Path) -> str:
    """The doc's own title, falling back to a prettified filename.

    Each format supplies its own reader for this, because it runs for every doc
    on every tree build: read a bounded prefix, or cache what you had to parse.
    """
    fmt = DOC_SUFFIXES.get(path.suffix.lower())
    if fmt is not None:
        try:
            title = fmt.title(path)
        except OSError:
            title = ""
        if title:
            return title
    return path.stem.replace("-", " ").replace("_", " ")


def _plain(text: str) -> str:
    """Strip inline markdown so headings read cleanly in menus and the TOC."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    return text.strip()


def _slugify(text: str) -> str:
    """A heading's anchor id. Shared so an org `[[*Section]]` link and the
    heading it points at agree on the spelling."""
    base = re.sub(r"[^\w\s-]", "", _plain(text).lower()).strip()
    return re.sub(r"[\s_]+", "-", base) or "section"


def _kind_of(path: Path) -> str:
    stem = path.stem.upper()
    if stem in CFG.pinned:
        return stem
    try:
        parts = path.relative_to(ROOT).parts
    except ValueError:
        parts = path.parts
    if "docs" in parts or "doc" in parts:
        return "doc"
    return "other"


def _prettify(slug: str) -> tuple[str, str]:
    """`10-api-gateway` → ("10", "api gateway"); `common_utils` → ("", "common utils")."""
    m = re.match(r"^(\d+)[-_.](.*)$", slug)
    num, rest = (m.group(1), m.group(2)) if m else ("", slug)
    return num, rest.replace("-", " ").replace("_", " ").strip() or slug


def _group_state(gdir: Path) -> str:
    """A render-invisible `<!-- status: state: … -->` block drives the sidebar dot.

    Entirely optional: a repo that doesn't use the convention just gets no dot.
    Markdown only — an HTML comment is the one spelling that stays invisible,
    and this reads the file on every tree build, which a notebook can't afford.
    """
    for name in ("SPEC", "STATUS", "README"):
        path = _find_stem(gdir, name, MARKDOWN.suffixes)
        if path is None:
            continue
        m = STATUS_RE.search(path.read_text(encoding="utf-8", errors="replace")[:4000])
        if not m:
            continue
        for line in m.group(1).splitlines():
            if line.strip().startswith("state:"):
                return line.split(":", 1)[1].split("#")[0].strip()
    return ""


def _doc_label(path: Path, kind: str, title: str) -> str:
    if kind in CFG.pinned:
        return kind
    if kind == "doc":
        # `04-how-snapshots-work.md` → "04 · how snapshots work"
        num, rest = _prettify(path.stem)
        return f"{num} · {rest}" if num else rest
    return title or path.name


def build_tree() -> list[Group]:
    """Group every document the way the repo is actually laid out.

    Three shapes, in order: files at the repo root are one "repo" section; a
    directory listed in `group_dirs` contributes one section *per child* (the
    monorepo case — `projects/10-api-gateway/` becomes its own section); any
    other top-level directory is a single section of its own.
    """
    groups: dict[str, Group] = {}

    def group_for(rel: str) -> Group:
        parts = rel.split("/")
        if len(parts) == 1:
            gid, title, num, prefix = "root", "repo", "", ""
        elif parts[0] in CFG.group_dirs and len(parts) > 2:
            gid = f"{parts[0]}/{parts[1]}"
            num, title = _prettify(parts[1])
            prefix = gid + "/"
        else:
            gid = parts[0]
            num, title = "", gid.lstrip(".").replace("-", " ").replace("_", " ")
            prefix = gid + "/"
        if gid not in groups:
            g = Group(gid=gid, title=title, num=num, prefix=prefix)
            if prefix:
                g.state = _group_state(ROOT / prefix)
            groups[gid] = g
        return groups[gid]

    for path in _walk_docs():
        rel = path.relative_to(ROOT).as_posix()
        kind = _kind_of(path)
        title = _first_heading(path)
        g = group_for(rel)
        g.docs.append(
            Doc(rel=rel, title=title, label=_doc_label(path, kind, title), kind=kind, group=g.gid)
        )

    for g in groups.values():
        g.docs.sort(key=lambda d: (KIND_ORDER.get(d.kind, 99), d.rel))

    def gkey(g: Group) -> tuple:
        if g.gid == "root":
            return (0, "")
        if g.num:
            return (1, g.num)
        return (2, g.title)

    return sorted(groups.values(), key=gkey)


# --------------------------------------------------------------------------- #
# Syntax highlighting — a deliberately small tokenizer. Good enough to make
# Rust/JSON/bash readable at a glance; not a parser, and never pretends to be.
# --------------------------------------------------------------------------- #


@dataclass
class LangSpec:
    keywords: set[str]
    types: set[str] = field(default_factory=set)
    line_comment: str = "#"
    block_comment: bool = False
    consts: set[str] = field(default_factory=set)


def _kw(words: str) -> set[str]:
    """A keyword set from a whitespace-separated blob — kept readable in source."""
    return set(words.split())


RUST_KW = _kw("""as async await break const continue crate dyn else enum extern false fn for
if impl in let loop match mod move mut pub ref return self Self static struct super trait true
type unsafe use where while union macro_rules""")
RUST_TY = _kw("""u8 u16 u32 u64 u128 usize i8 i16 i32 i64 i128 isize f32 f64 bool char str String
Vec Option Result Box Arc Rc RefCell Mutex RwLock HashMap HashSet BTreeMap VecDeque Duration
Instant Path PathBuf Cow""")
PY_KW = _kw("""and as assert async await break class continue def del elif else except finally
for from global if import in is lambda nonlocal not or pass raise return try while with yield
True False None self""")
SH_KW = _kw("""if then elif else fi for while do done case esac function return exit export local
set unset echo cd source read shift trap eval exec printf""")
SQL_KW = _kw("""select from where insert into values update set delete create table drop alter
index unique primary key foreign references join left right inner outer on group by order having
limit offset returning with as and or not null default constraint begin commit rollback
distinct count sum avg min max case when then else end exists in between like asc desc""")
TS_KW = _kw("""abstract any as async await boolean break case catch class const continue declare
default delete do else enum export extends false finally for from function get if implements
import in instanceof interface let new null number of private protected public readonly return
set static string super switch this throw true try type typeof undefined var void while yield""")
GO_KW = _kw("""break case chan const continue default defer else fallthrough for func go goto if
import interface map package range return select struct switch type var true false nil""")

LANG_SPECS: dict[str, LangSpec] = {
    "rust": LangSpec(RUST_KW, RUST_TY, "//", True),
    "python": LangSpec(PY_KW, set(), "#", False),
    "bash": LangSpec(SH_KW, set(), "#", False),
    "sql": LangSpec(SQL_KW, set(), "--", True),
    "typescript": LangSpec(TS_KW, set(), "//", True),
    "javascript": LangSpec(TS_KW, set(), "//", True),
    "go": LangSpec(GO_KW, set(), "//", True),
    "json": LangSpec(set(), set(), "", False, {"true", "false", "null"}),
    "toml": LangSpec(set(), set(), "#", False, {"true", "false"}),
    "yaml": LangSpec(set(), set(), "#", False, {"true", "false", "null", "yes", "no"}),
    "makefile": LangSpec(set(), set(), "#", False),
    "dockerfile": LangSpec(
        _kw("FROM RUN CMD LABEL EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG"),
        set(), "#", False,
    ),
    "text": LangSpec(set(), set(), "", False),
}
LANG_ALIAS = {
    "rs": "rust", "py": "python", "sh": "bash", "shell": "bash", "zsh": "bash",
    "console": "bash", "ts": "typescript", "tsx": "typescript", "js": "javascript",
    "jsx": "javascript", "yml": "yaml", "psql": "sql", "postgres": "sql", "golang": "go",
    "docker": "dockerfile", "make": "makefile", "": "text", "plain": "text", "txt": "text",
}


def _esc(s: str, quote: bool = False) -> str:
    return html_mod.escape(s, quote=quote)


def highlight(code: str, lang: str) -> str:
    """Escaped HTML for `code`, with <span class=…> tokens when we know the lang."""
    key = LANG_ALIAS.get(lang.lower().strip(), lang.lower().strip())
    spec = LANG_SPECS.get(key)
    if spec is None:
        return _esc(code)

    parts = []
    if spec.block_comment:
        parts.append(r"(?P<cmtb>/\*.*?\*/)" if key != "sql" else r"(?P<cmtb>/\*.*?\*/)")
    if spec.line_comment:
        parts.append(rf"(?P<cmt>{re.escape(spec.line_comment)}[^\n]*)")
    if key == "rust":
        parts.append(r"(?P<attr>\#!?\[[^\]\n]*\])")
        parts.append(r"(?P<life>&?'[a-z_][a-z_0-9]*\b)")
    parts.append(r'(?P<str>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\\n])*\')')
    parts.append(r"(?P<num>\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?[a-z0-9]*\b)")
    if key == "bash":
        parts.append(r"(?P<flag>(?<=\s)--?[A-Za-z][\w-]*)")
        parts.append(r"(?P<var>\$\{?\w+\}?)")
    parts.append(r"(?P<word>[A-Za-z_][A-Za-z_0-9]*!?)")
    pattern = re.compile("|".join(parts), re.DOTALL)

    out: list[str] = []
    pos = 0
    for m in pattern.finditer(code):
        out.append(_esc(code[pos : m.start()]))
        kind = m.lastgroup
        tok = m.group()
        if kind in ("cmt", "cmtb"):
            cls = "c-cmt"
        elif kind == "str":
            cls = "c-str"
        elif kind == "num":
            cls = "c-num"
        elif kind == "attr":
            cls = "c-attr"
        elif kind == "life":
            cls = "c-life"
        elif kind == "flag":
            cls = "c-flag"
        elif kind == "var":
            cls = "c-var"
        else:  # word
            low = tok.lower()
            after = code[m.end() : m.end() + 1]
            if tok in spec.keywords or (key in ("sql", "dockerfile") and low in spec.keywords):
                cls = "c-kw"
            elif low in spec.consts:
                cls = "c-num"
            elif tok in spec.types or (tok[:1].isupper() and key in ("rust", "typescript", "go")):
                cls = "c-ty"
            elif tok.endswith("!") or after == "(":
                cls = "c-fn"
            else:
                cls = ""
        out.append(f'<span class="{cls}">{_esc(tok)}</span>' if cls else _esc(tok))
        pos = m.end()
    out.append(_esc(code[pos:]))
    return "".join(out)


# --------------------------------------------------------------------------- #
# Markdown → HTML. A focused GFM subset covering exactly what this repo's docs
# use: headings, fences, tables, task lists (including the SPEC's [~]/[✔]),
# blockquotes, nested lists, and links that resolve between docs and sources.
# --------------------------------------------------------------------------- #

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*(\S.*)?$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HR_RE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
BQ_RE = re.compile(r"^\s*>\s?(.*)$")
ULI_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
OLI_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
TASK_RE = re.compile(r"^\[([ xX~✔✓])\]\s*(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:-]*-{2,}[\s:|-]*\|?\s*$")
HTML_LINE_RE = re.compile(r"^\s*</?([A-Za-z][\w-]*)")
INLINE_HTML_OK = ("br", "kbd", "sub", "sup", "b", "i", "em", "strong", "small", "u", "mark")
BLOCK_HTML_OK = {
    "details", "summary", "div", "img", "picture", "source", "table", "thead", "tbody",
    "tr", "td", "th", "p", "a", "span", "center", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "hr", "br", "video", "figure", "figcaption", "pre",
}


class Markdown:
    """One instance per rendered document (it accumulates the TOC and slugs)."""

    def __init__(self, doc_rel: str):
        self.dir = PurePosixPath(doc_rel).parent
        self.toc: list[dict] = []
        self.slugs: dict[str, int] = {}
        self.mermaid = False
        self.title = ""
        self._stash: list[str] = []

    # -- links ------------------------------------------------------------- #

    def _slug(self, text: str) -> str:
        base = _slugify(text)
        n = self.slugs.get(base, 0)
        self.slugs[base] = n + 1
        return base if n == 0 else f"{base}-{n}"

    def _resolve(self, href: str) -> tuple[str, str]:
        """(href, css-class) — repo-relative targets become in-app hash routes."""
        href = href.strip()
        if not href:
            return "#", "x"
        if re.match(r"^(https?:|mailto:|vscode:|data:|//)", href):
            return _esc(href, True), "ext"
        if href.startswith("#"):
            return _esc(href, True), "anchor"
        anchor = ""
        if "#" in href:
            href, anchor = href.split("#", 1)
            if not href:
                return _esc("#" + anchor, True), "anchor"
        target = (self.dir / href) if not href.startswith("/") else PurePosixPath(href.lstrip("/"))
        rel = PurePosixPath(*_normalize(target.parts)).as_posix()
        abs_path = ROOT / rel
        if not abs_path.exists():
            return _esc(f"#/{rel}", True), "miss"
        if abs_path.is_dir():
            return _esc(f"#/{rel}", True), "dir"
        suffix = abs_path.suffix.lower()
        if suffix in RAW_SUFFIXES:
            return _esc(f"/raw?p={rel}", True), "ext"
        frag = f"::{anchor}" if anchor else ""
        cls = "doc" if suffix in DOC_SUFFIXES else "src"
        return _esc(f"#/{rel}{frag}", True), cls

    def _img_src(self, src: str) -> str:
        src = src.strip()
        if re.match(r"^(https?:|data:|//)", src):
            return _esc(src, True)
        target = (self.dir / src) if not src.startswith("/") else PurePosixPath(src.lstrip("/"))
        rel = PurePosixPath(*_normalize(target.parts)).as_posix()
        return _esc(f"/raw?p={rel}", True)

    # -- inline ------------------------------------------------------------ #

    def _keep(self, html: str) -> str:
        self._stash.append(html)
        return f"\x00{len(self._stash) - 1}\x00"

    def inline(self, text: str) -> str:
        # 1. code spans come out first so nothing rewrites their insides
        def code_span(m: re.Match) -> str:
            return self._keep(f"<code>{_esc(m.group(2).strip())}</code>")

        text = re.sub(r"(`+)([^`]+?)\1", code_span, text)
        text = _esc(text)
        # a tiny whitelist of inline HTML the docs actually use
        for tag in INLINE_HTML_OK:
            text = re.sub(rf"&lt;(/?{tag})\s*/?&gt;", r"<\1>", text, flags=re.I)

        def image(m: re.Match) -> str:
            alt, src = m.group(1), m.group(2)
            return self._keep(
                f'<img src="{self._img_src(src)}" alt="{_esc(alt, True)}" loading="lazy">'
            )

        text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)", image, text)

        def link(m: re.Match) -> str:
            label, href = m.group(1), m.group(2)
            url, cls = self._resolve(href)
            ext = ' target="_blank" rel="noreferrer"' if cls == "ext" else ""
            return self._keep(f'<a class="l-{cls}" href="{url}"{ext}>{self._emphasis(label)}</a>')

        text = re.sub(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\(([^)\s]+)(?:\s+[^)]*)?\)", link, text)
        text = self._emphasis(text)

        def autolink(m: re.Match) -> str:
            u = m.group(0)
            return self._keep(
                f'<a class="l-ext" href="{_esc(u, True)}" '
                f'target="_blank" rel="noreferrer">{u}</a>'
            )

        text = re.sub(r"(?<![\"'=(>])\bhttps?://[^\s<>()\[\]]+", autolink, text)

        # restore stashes (links may contain stashed code spans → loop)
        for _ in range(6):
            if "\x00" not in text:
                break
            text = re.sub(r"\x00(\d+)\x00", lambda m: self._stash[int(m.group(1))], text)
        return text

    def _emphasis(self, text: str) -> str:
        text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<strong><em>\1</em></strong>", text)
        text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w\\])__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<![\w*\\])\*(?=\S)([^*]+?)(?<=\S)\*(?!\w)", r"<em>\1</em>", text)
        text = re.sub(r"(?<![\w_\\])_(?=\S)([^_]+?)(?<=\S)_(?!\w)", r"<em>\1</em>", text)
        text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text)
        return text

    # -- blocks ------------------------------------------------------------ #

    def render(self, text: str) -> str:
        lines = text.replace("\r\n", "\n").replace("\t", "    ").split("\n")
        return self.blocks(lines)

    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            m = FENCE_RE.match(line)
            if m:
                i = self._fence(lines, i, m, out)
                continue

            if line.lstrip().startswith("<!--"):
                while i < n and "-->" not in lines[i]:
                    i += 1
                i += 1
                continue

            m = HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                raw = m.group(2)
                text_html = self.inline(raw)
                plain = _plain(raw)
                slug = self._slug(raw)
                if level == 1 and not self.title:
                    self.title = plain
                if 2 <= level <= 3:
                    self.toc.append({"level": level, "id": slug, "text": plain})
                out.append(
                    f'<h{level} id="{slug}">'
                    f'<a class="hlink" href="#{slug}">#</a>{text_html}</h{level}>'
                )
                i += 1
                continue

            if HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue

            if BQ_RE.match(line):
                i = self._blockquote(lines, i, out)
                continue

            if (
                "|" in line
                and i + 1 < n
                and "|" in lines[i + 1]
                and TABLE_SEP_RE.match(lines[i + 1])
            ):
                i = self._table(lines, i, out)
                continue

            if ULI_RE.match(line) or OLI_RE.match(line):
                i = self._list(lines, i, out)
                continue

            hm = HTML_LINE_RE.match(line)
            if hm and hm.group(1).lower() in BLOCK_HTML_OK:
                buf = []
                while i < n and lines[i].strip():
                    buf.append(lines[i])
                    i += 1
                out.append("\n".join(buf))
                continue

            before = i
            i = self._paragraph(lines, i, out)
            if i <= before:  # belt-and-braces: never spin on a line
                i = before + 1
        return "\n".join(out)

    def _fence(self, lines: list[str], i: int, m: re.Match, out: list[str]) -> int:
        indent, ticks = len(m.group(1)), m.group(2)[0]
        lang, label_text = _fence_info(m.group(3) or "")
        close = re.compile(rf"^\s*{re.escape(ticks)}{{{len(m.group(2))},}}\s*$")
        body: list[str] = []
        i += 1
        while i < len(lines) and not close.match(lines[i]):
            body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
            i += 1
        i += 1
        code = "\n".join(body)
        if lang.lower() == "mermaid":
            self.mermaid = True
            out.append(f'<pre class="mermaid">{_esc(code)}</pre>')
            return i
        cls = "lang path" if ("/" in label_text or ":" in label_text) else "lang"
        label = f'<span class="{cls}">{_esc(label_text)}</span>' if label_text else ""
        out.append(
            f'<div class="cb">{label}<button class="copy" type="button">copy</button>'
            f'<pre><code class="lang-{_esc(lang.lower(), True) or "text"}">'
            f"{highlight(code, lang)}</code></pre></div>"
        )
        return i

    def _blockquote(self, lines: list[str], i: int, out: list[str]) -> int:
        inner: list[str] = []
        while i < len(lines):
            m = BQ_RE.match(lines[i])
            if m:
                inner.append(m.group(1))
                i += 1
            elif lines[i].strip() and not HEADING_RE.match(lines[i]) and inner:
                inner.append(lines[i].strip())  # lazy continuation
                i += 1
            else:
                break
        body = self.blocks(inner)
        cls = "note"
        head = inner[0] if inner else ""
        alert = re.match(r"^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", head, re.I)
        if alert:
            cls = f"note {alert.group(1).lower()}"
            body = self.blocks(inner[1:])
        out.append(f'<blockquote class="{cls}">{body}</blockquote>')
        return i

    def _table(self, lines: list[str], i: int, out: list[str]) -> int:
        header = _split_row(lines[i])
        aligns = []
        for cell in _split_row(lines[i + 1]):
            c = cell.strip()
            if c.startswith(":") and c.endswith(":"):
                aligns.append("center")
            elif c.endswith(":"):
                aligns.append("right")
            else:
                aligns.append("left")
        i += 2
        rows: list[list[str]] = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            rows.append(_split_row(lines[i]))
            i += 1

        def cells(cs: list[str], tag: str) -> str:
            got = []
            for k, c in enumerate(cs):
                a = aligns[k] if k < len(aligns) else "left"
                got.append(f'<{tag} class="a-{a}">{self.inline(c.strip())}</{tag}>')
            return "".join(got)

        body = "".join(f"<tr>{cells(r, 'td')}</tr>" for r in rows)
        out.append(
            f'<div class="tw"><table><thead><tr>{cells(header, "th")}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )
        return i

    def _list(self, lines: list[str], i: int, out: list[str]) -> int:
        first = ULI_RE.match(lines[i]) or OLI_RE.match(lines[i])
        if first is None:  # unreachable — callers only enter on a list line
            return self._paragraph(lines, i, out)
        ordered = OLI_RE.match(lines[i]) is not None and ULI_RE.match(lines[i]) is None
        base = len(first.group(1))
        items: list[list[str]] = []
        n = len(lines)

        while i < n:
            line = lines[i]
            if not line.strip():
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and (_indent(lines[j]) > base or _is_item(lines[j], base)):
                    if items:
                        items[-1].append("")
                    i = j
                    continue
                break
            ind = _indent(line)
            m = ULI_RE.match(line) or OLI_RE.match(line)
            if m and ind <= base + 1:
                if ind < base:
                    break
                items.append([m.group(3)])
                i += 1
            elif ind > base and items:
                items[-1].append(line[min(base + 2, ind) :])
                i += 1
            else:
                break

        rendered = []
        has_task = False
        for content in items:
            li_class = ""
            tm = TASK_RE.match(content[0]) if content else None
            if tm:
                has_task = True
                mark = tm.group(1)
                state = (
                    "done" if mark in "xX✔✓" else ("open-field" if mark == "~" else "open")
                )
                box = {"done": "✔", "open-field": "~", "open": ""}[state]
                content = [tm.group(2)] + content[1:]
                li_class = f' class="task {state}"'
                prefix = f'<span class="box {state}">{box}</span>'
            else:
                prefix = ""
            # a "lead" of plain lines renders inline; the rest as nested blocks
            k = 0
            while k < len(content) and content[k].strip() and not (
                ULI_RE.match(content[k]) or OLI_RE.match(content[k]) or FENCE_RE.match(content[k])
            ):
                k += 1
            lead = self.inline(" ".join(x.strip() for x in content[:k])) if k else ""
            rest = self.blocks(content[k:]) if k < len(content) else ""
            rendered.append(f"<li{li_class}>{prefix}{lead}{rest}</li>")

        tag = "ol" if ordered else "ul"
        cls = ' class="tasks"' if has_task else ""
        out.append(f"<{tag}{cls}>{''.join(rendered)}</{tag}>")
        return i

    def _paragraph(self, lines: list[str], i: int, out: list[str]) -> int:
        # Always consumes its first line — `blocks()` relies on that to make
        # progress, and a paragraph is the branch of last resort.
        n = len(lines)
        buf: list[str] = [lines[i].strip()]
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                break
            if (
                HEADING_RE.match(line)
                or FENCE_RE.match(line)
                or HR_RE.match(line)
                or BQ_RE.match(line)
                or ULI_RE.match(line)
                or OLI_RE.match(line)
                or line.lstrip().startswith("<!--")
            ):
                break
            if "|" in line and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]):
                break
            buf.append(line.strip())
            i += 1
        out.append(f"<p>{self.inline(' '.join(buf))}</p>")
        return i


def _fence_info(info: str) -> tuple[str, str]:
    """(highlight language, display label) for a fence's info string.

    Covers the three forms these docs use: a bare language (```rust), a
    language with attributes (```rust,no_run), and the file-reference form
    (```199:200:projects/06-object-store/src/routes.rs) — where the language
    has to come from the referenced file's extension.
    """
    info = info.strip()
    if not info:
        return "", ""
    head = re.split(r"[,\s]", info, maxsplit=1)[0]
    if ":" in head or "/" in head:
        suffix = PurePosixPath(head.split(":")[-1]).suffix.lstrip(".")
        return suffix, info
    return head, info


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_item(line: str, base: int) -> bool:
    m = ULI_RE.match(line) or OLI_RE.match(line)
    return bool(m) and _indent(line) >= base


def _normalize(parts: tuple[str, ...]) -> list[str]:
    stack: list[str] = []
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            if stack:
                stack.pop()
        else:
            stack.append(p)
    return stack


def _split_row(line: str) -> list[str]:
    """Split a table row on `|`, respecting code spans and escapes."""
    s = line.strip()
    cells: list[str] = []
    cur: list[str] = []
    in_code = False
    k = 0
    while k < len(s):
        c = s[k]
        if c == "\\" and k + 1 < len(s):
            cur.append(s[k + 1])
            k += 2
            continue
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        k += 1
    cells.append("".join(cur))
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return cells


# --------------------------------------------------------------------------- #
# Formats. `FORMATS` is the one place that decides what counts as a document —
# discovery, sidebar titles, link classing, search, and rendering all read from
# it, so adding a format means adding an entry here and nothing else.
#
# Neither of the non-markdown ones is a second parser, deliberately. Org is
# *translated* into markdown and handed to `Markdown`; a notebook is JSON, so
# its cells are handed over the same way. Both inherit tables, the TOC, link
# resolution, and syntax highlighting without the markdown parser growing a
# single branch — which is the only way to add formats and still keep the rule
# that it must not grow toward CommonMark.
# --------------------------------------------------------------------------- #


def _as_written(text: str) -> str:
    """Search default: the file's own bytes are what it says."""
    return text


@dataclass
class Format:
    """One document format: how to name it, render it, and search it."""

    suffixes: tuple[str, ...]
    render: Callable[[str, str], dict]  # (doc rel path, text) → html/toc/mermaid/title
    title: Callable[[Path], str]  # a cheap title for the sidebar, never a full parse
    text: Callable[[str], str] = _as_written  # what search greps, when the file isn't it
    max_bytes: int = 0  # 0 = no ceiling


def _render_markdown(rel: str, text: str) -> dict:
    md = Markdown(rel)
    html = md.render(text)
    return {"html": html, "toc": md.toc, "mermaid": md.mermaid, "title": md.title}


def _md_title(path: Path) -> str:
    """A markdown doc's own `# Title`, if it has one near the top."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        in_fence = False
        for _ in range(60):
            line = fh.readline()
            if not line:
                break
            s = line.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence and s.startswith("# "):
                return _plain(s[2:].strip())
    return ""


# -- org-mode --------------------------------------------------------------- #

ORG_HEADING_RE = re.compile(r"^(\*+)\s+(.*)$")
ORG_BLOCK_RE = re.compile(r"^\s*#\+(begin|end)_(\w+)\s*(.*)$", re.I)
ORG_KEYWORD_RE = re.compile(r"^\s*#\+(\w+):\s*(.*)$")
ORG_DRAWER_RE = re.compile(r"^\s*:(\w[\w-]*):\s*$")
ORG_COMMENT_RE = re.compile(r"^\s*#(\s|$)")
ORG_TABLE_SEP_RE = re.compile(r"^\s*\|[-+|\s]+\|?\s*$")
ORG_LINK_RE = re.compile(r"\[\[([^\]]+?)\](?:\[([^\]]*?)\])?\]")
ORG_TAGS_RE = re.compile(r"\s+(?::[\w@%#]+)+:\s*$")
ORG_DESC_RE = re.compile(r"^(\s*[-+]\s+)(.+?)\s+::\s+(.*)$")
ORG_VERBATIM_RE = re.compile(r"(?<![\w=~])([=~])(?=\S)([^\n]+?)(?<=\S)\1(?![\w=~])")
ORG_PARTIAL_RE = re.compile(r"^(\s*(?:[-+]|\d+[.)])\s+)\[-\]")
ORG_SRC_BLOCKS = ("src", "example", "verse")


def _org_href(href: str) -> str:
    """An org link target, spelled the way markdown would spell it."""
    href = href.strip()
    if href.startswith("file:"):
        href = href[5:]
    if href.startswith("*"):  # [[*Section Title]] — a link to a heading in this doc
        return "#" + _slugify(href[1:])
    if href.startswith("id:"):  # only ever resolvable inside emacs
        return "#"
    return href


def _org_inline(text: str) -> str:
    """Org's inline markup as markdown's, with verbatim spans held aside so
    the emphasis rules cannot reach inside them."""
    spans: list[str] = []

    def hold(m: re.Match) -> str:
        spans.append(f"`{m.group(2)}`")
        return f"\x00{len(spans) - 1}\x00"

    def link(m: re.Match) -> str:
        href = m.group(1)
        # a bare [[*Section]] shows the heading, not org's spelling of it
        label = (m.group(2) or "").strip() or href.lstrip("*").strip()
        return f"[{label}]({_org_href(href)})"

    text = ORG_VERBATIM_RE.sub(hold, text)
    text = ORG_LINK_RE.sub(link, text)
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\w)", r"**\1**", text)
    text = re.sub(r"(?<![\w/:])/(?=\S)([^/\n]+?)(?<=\S)/(?!\w)", r"*\1*", text)
    if spans:
        text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text


def _org_to_markdown(text: str) -> tuple[list[str], str]:
    """Org source → markdown lines, plus `#+TITLE:` if the file declared one.

    Headings shift down a level when there is a title, so the title becomes the
    H1 and org's top-level sections land on H2 — which is where `Markdown`
    starts collecting the TOC. Without a title, `* Section` becomes the H1,
    exactly as `# Section` would in a markdown doc.
    """
    lines = text.replace("\r\n", "\n").replace("\t", "    ").split("\n")
    title = ""
    for line in lines[:80]:
        m = ORG_KEYWORD_RE.match(line)
        if m and m.group(1).lower() == "title":
            title = m.group(2).strip()
            break

    out: list[str] = [f"# {_org_inline(title)}"] if title else []
    shift = 1 if title else 0
    in_code = skipping = quoting = drawer = False

    for raw in lines:
        m = ORG_BLOCK_RE.match(raw)
        if m:
            opening, kind = m.group(1).lower() == "begin", m.group(2).lower()
            if kind in ORG_SRC_BLOCKS:
                info = m.group(3).split()
                in_code = opening
                out.append("```" + (info[0] if opening and kind == "src" and info else ""))
            elif kind == "quote":
                quoting = opening
                out.append("")
            elif kind == "comment":
                skipping = opening
            else:  # export, center, anything else — keep the contents, drop the edge
                out.append("")
            continue
        if in_code:
            out.append(raw)
            continue
        if skipping:
            continue

        m = ORG_DRAWER_RE.match(raw)
        if m:  # :PROPERTIES: … :END: — bookkeeping emacs keeps, not content
            drawer = m.group(1).upper() != "END"
            continue
        if drawer or ORG_KEYWORD_RE.match(raw) or ORG_COMMENT_RE.match(raw):
            continue

        m = ORG_HEADING_RE.match(raw)
        if m:
            level = min(len(m.group(1)) + shift, 6)
            head = ORG_TAGS_RE.sub("", m.group(2).strip())
            out.append(f"{'#' * level} {_org_inline(head)}")
            continue

        if ORG_TABLE_SEP_RE.match(raw):
            out.append(raw.replace("+", "|"))
            continue

        m = ORG_DESC_RE.match(raw)
        if m:  # `- term :: definition`, which markdown has no spelling for
            raw = f"{m.group(1)}**{m.group(2)}** — {m.group(3)}"
        raw = ORG_PARTIAL_RE.sub(r"\1[~]", raw)  # org's half-done box is tome's [~]
        line = _org_inline(raw)
        out.append(f"> {line}" if quoting else line)

    return out, title


def _render_org(rel: str, text: str) -> dict:
    lines, title = _org_to_markdown(text)
    md = Markdown(rel)
    html = md.blocks(lines)
    return {"html": html, "toc": md.toc, "mermaid": md.mermaid, "title": md.title or _plain(title)}


def _org_title(path: Path) -> str:
    """`#+TITLE:` wins over the first heading, wherever it turns up."""
    heading = ""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(60):
            line = fh.readline()
            if not line:
                break
            m = ORG_KEYWORD_RE.match(line)
            if m and m.group(1).lower() == "title":
                return _plain(m.group(2).strip())
            m = ORG_HEADING_RE.match(line)
            if m and not heading:
                heading = _plain(_org_inline(ORG_TAGS_RE.sub("", m.group(2).strip())))
    return heading


# -- jupyter notebooks ------------------------------------------------------ #

NB_OUTPUT_LIMIT = 4000  # characters per output — plots are the point, logs are not
NB_MAX_BYTES = 10_000_000  # a notebook full of embedded plots is legitimately large
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
B64_RE = re.compile(r"[A-Za-z0-9+/=]+")

# Cached because, unlike every other format, there is no way to read a title out
# of a notebook without parsing all of it — and `_first_heading` runs for every
# doc on every tree build. Keyed on size as well as mtime: ext4 stamps mtime
# from a coarse clock, so two writes inside the same tick are indistinguishable
# by time alone.
_NB_TITLES: dict[str, tuple[tuple[float, int], str]] = {}


def _nb_load(text: str) -> dict:
    try:
        nb = json.loads(text)
    except ValueError:
        return {}
    return nb if isinstance(nb, dict) else {}


def _nb_cells(nb: dict) -> list:
    cells = nb.get("cells")
    return [c for c in cells if isinstance(c, dict)] if isinstance(cells, list) else []


def _nb_source(cell: dict) -> str:
    src = cell.get("source")
    return "".join(str(x) for x in src) if isinstance(src, list) else str(src or "")


def _nb_text(value: object, sep: str = "") -> str:
    text = sep.join(str(x) for x in value) if isinstance(value, list) else str(value or "")
    text = ANSI_RE.sub("", text)
    if len(text) > NB_OUTPUT_LIMIT:
        text = f"{text[:NB_OUTPUT_LIMIT]}\n… (truncated)"
    return text.rstrip("\n")


def _nb_language(nb: dict) -> str:
    meta = nb.get("metadata") or {}
    info = meta.get("language_info") or {}
    spec = meta.get("kernelspec") or {}
    for name in (info.get("name"), spec.get("language"), spec.get("name")):
        if isinstance(name, str) and name.strip():
            name = name.strip().lower()
            # "python3" names a kernel, not a language the highlighter knows
            return name if name in LANG_SPECS or name in LANG_ALIAS else re.sub(r"\d+$", "", name)
    return "python"


def _nb_png(value: object) -> str:
    """The base64 payload of a PNG output, or "" if it isn't one — it goes
    straight into a data: URI, so anything that isn't base64 stays out."""
    if not value:
        return ""
    joined = "".join(str(x) for x in value) if isinstance(value, list) else str(value)
    data = re.sub(r"\s+", "", joined)
    return data if B64_RE.fullmatch(data) else ""


def _nb_out(text: str, cls: str = "") -> str:
    return f'<div class="cb out{cls}"><pre><code>{_esc(text)}</code></pre></div>'


def _nb_outputs(outputs: object) -> list[str]:
    """A cell's outputs, in the order the notebook recorded them. Anything
    richer than text or a PNG is skipped rather than guessed at."""
    parts: list[str] = []
    for out in outputs if isinstance(outputs, list) else []:
        if not isinstance(out, dict):
            continue
        kind = out.get("output_type")
        if kind == "stream":
            text = _nb_text(out.get("text"))
            if text:
                parts.append(_nb_out(text))
        elif kind in ("execute_result", "display_data"):
            data = out.get("data") or {}
            png = _nb_png(data.get("image/png"))
            if png:
                parts.append(
                    f'<p class="nb-img"><img src="data:image/png;base64,{png}" '
                    f'alt="cell output" loading="lazy"></p>'
                )
                continue
            text = _nb_text(data.get("text/plain"))
            if text:
                parts.append(_nb_out(text))
        elif kind == "error":
            trace = _nb_text(out.get("traceback"), "\n")
            parts.append(_nb_out(trace or f"{out.get('ename')}: {out.get('evalue')}", " err"))
    return parts


def _render_notebook(rel: str, text: str) -> dict:
    """Notebooks are JSON, so this is assembly rather than parsing: markdown
    cells go through one `Markdown` instance (so the TOC and slugs accumulate
    across the whole notebook), code cells through the highlighter a fence uses.
    """
    nb = _nb_load(text)
    if not nb:
        return {"html": "<p>this notebook is not valid JSON</p>", "toc": [],
                "mermaid": False, "title": ""}
    md = Markdown(rel)
    lang = _nb_language(nb)
    parts: list[str] = []
    for cell in _nb_cells(nb):
        source = _nb_source(cell)
        if cell.get("cell_type") == "markdown":
            parts.append(md.render(source))
        elif cell.get("cell_type") == "code":
            if source.strip():
                parts.append(
                    f'<div class="cb"><span class="lang">{_esc(lang)}</span>'
                    f'<button class="copy" type="button">copy</button>'
                    f'<pre><code class="lang-{_esc(lang, True)}">'
                    f"{highlight(source, lang)}</code></pre></div>"
                )
            parts.extend(_nb_outputs(cell.get("outputs")))
    return {"html": "\n".join(parts), "toc": md.toc, "mermaid": md.mermaid, "title": md.title}


def _nb_search_text(text: str) -> str:
    """What a notebook actually says — searching its JSON would match base64
    image data and metadata keys, which is worse than not searching it at all."""
    return "\n".join(_nb_source(c) for c in _nb_cells(_nb_load(text)))


def _ipynb_title(path: Path) -> str:
    key = str(path)
    stat = path.stat()
    stamp = (stat.st_mtime, stat.st_size)
    hit = _NB_TITLES.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    title = ""
    for cell in _nb_cells(_nb_load(path.read_text(encoding="utf-8", errors="replace"))):
        if cell.get("cell_type") != "markdown":
            continue
        for line in _nb_source(cell).split("\n"):
            if line.strip().startswith("# "):
                title = _plain(line.strip()[2:])
                break
        if title:
            break
    _NB_TITLES[key] = (stamp, title)
    return title


MARKDOWN = Format((".md", ".markdown", ".mdown", ".mkd"), _render_markdown, _md_title)

FORMATS = (
    MARKDOWN,
    Format((".org",), _render_org, _org_title),
    Format((".ipynb",), _render_notebook, _ipynb_title, _nb_search_text, NB_MAX_BYTES),
)

# suffix → format, in registration order, so `_find_stem` prefers a README.md
# over a README.org when a directory happens to hold both.
DOC_SUFFIXES = {suffix: fmt for fmt in FORMATS for suffix in fmt.suffixes}


# --------------------------------------------------------------------------- #
# Documents — render any registered format, or a source file as one block.
# --------------------------------------------------------------------------- #


def safe_path(rel: str) -> Path | None:
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return None
    try:
        p = (ROOT / rel).resolve()
        p.relative_to(ROOT)
    except (ValueError, OSError):
        return None
    if any(part in CFG.skip_dirs for part in p.relative_to(ROOT).parts[:-1]):
        return None
    if _is_secret(p.name):
        return None
    return p if p.is_file() else None


# Loopback-only is not an excuse to hand out credentials: a doc that links to
# `../.env` should render a "not found", not the file. Sample/example variants
# are fine — they exist to be read.
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk")
SECRET_NAMES = {".netrc", ".npmrc", ".pypirc", ".htpasswd", "credentials", "id_rsa", "id_ed25519"}
SAMPLE_HINTS = (".example", ".sample", ".template", ".dist", ".defaults")


def _is_secret(name: str) -> bool:
    low = name.lower()
    if any(low.endswith(h) or h.strip(".") in low.split(".") for h in SAMPLE_HINTS):
        return False
    return (
        low.startswith(".env")
        or low in SECRET_NAMES
        or low.endswith(SECRET_SUFFIXES)
        or low.startswith("secrets.")
        or "secret" in low and low.endswith((".json", ".yaml", ".yml", ".toml"))
    )


def render_doc(rel: str) -> dict:
    path = safe_path(rel)
    if path is None:
        return {"error": f"not found: {rel}"}
    suffix = path.suffix.lower()
    stat = path.stat()
    fmt = DOC_SUFFIXES.get(suffix)
    if fmt is not None:
        if fmt.max_bytes and stat.st_size > fmt.max_bytes:
            return {"error": f"{rel} is too large to display ({stat.st_size // 1024} KB)"}
        out = fmt.render(rel, path.read_text(encoding="utf-8", errors="replace"))
        return {
            "path": rel,
            "abs": str(path),
            "title": out["title"] or _first_heading(path),
            "html": out["html"],
            "toc": out["toc"],
            "mermaid": out["mermaid"],
            "mtime": stat.st_mtime,
            "kind": _kind_of(path),
            "source": False,
        }
    if stat.st_size > 2_000_000:
        return {"error": f"{rel} is too large to display ({stat.st_size // 1024} KB)"}
    text = path.read_text(encoding="utf-8", errors="replace")
    lang = LANG_ALIAS.get(suffix.lstrip("."), suffix.lstrip("."))
    return {
        "path": rel,
        "abs": str(path),
        "title": path.name,
        "html": f'<div class="cb src"><pre><code>{highlight(text, lang)}</code></pre></div>',
        "toc": [],
        "mermaid": False,
        "mtime": stat.st_mtime,
        "kind": "source",
        "source": True,
        "lines": text.count("\n") + 1,
    }


def search(query: str, limit: int = 60) -> list[dict]:
    """Case-insensitive full-text sweep. ~170 docs — brute force is instant."""
    q = query.strip().lower()
    if len(q) < 2:
        return []
    results: list[dict] = []
    for g in build_tree():
        for doc in g.docs:
            path = ROOT / doc.rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fmt = DOC_SUFFIXES.get(path.suffix.lower())
            if fmt is not None:
                text = fmt.text(text)
            low = text.lower()
            if q not in low:
                continue
            hits = 0
            for ln, line in enumerate(text.split("\n"), 1):
                if q in line.lower():
                    hits += 1
                    if hits <= 3:
                        snippet = line.strip()
                        if len(snippet) > 190:
                            at = snippet.lower().find(q)
                            snippet = "…" + snippet[max(0, at - 70) : at + 120] + "…"
                        results.append(
                            {
                                "path": doc.rel,
                                "title": doc.title,
                                "group": g.title,
                                "line": ln,
                                "snippet": snippet,
                                "count": 0,
                            }
                        )
            for r in results[-min(hits, 3) :]:
                r["count"] = hits
    results.sort(key=lambda r: (-r["count"], r["path"], r["line"]))
    return results[:limit]


def version_stamp() -> str:
    """Cheap fingerprint of every doc's mtime — drives the browser's live reload."""
    acc = 0.0
    count = 0
    for path in _walk_docs():
        try:
            acc += path.stat().st_mtime
            count += 1
        except OSError:
            pass
    return f"{count}:{acc:.0f}"


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "gauntlet-docs"

    def log_message(self, format, *args):  # quiet — keep the terminal clean
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # A browser that navigated away mid-response is not an error.
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(body)

    def _json(self, payload) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        route = url.path

        if route in ("/", "/index.html"):
            self._send(200, page_html().encode(), "text/html; charset=utf-8")
            return

        if route == "/api/tree":
            self._json(
                {
                    "groups": [
                        {
                            "id": g.gid,
                            "title": g.title,
                            "num": g.num,
                            "state": g.state,
                            "prefix": g.prefix,
                            "docs": [
                                {"path": d.rel, "title": d.title, "label": d.label, "kind": d.kind}
                                for d in g.docs
                            ],
                        }
                        for g in build_tree()
                    ],
                    "version": version_stamp(),
                }
            )
            return

        if route == "/api/doc":
            self._json(render_doc(qs.get("p", [""])[0]))
            return

        if route == "/api/search":
            self._json({"results": search(qs.get("q", [""])[0])})
            return

        if route == "/api/version":
            self._json({"version": version_stamp()})
            return

        if route == "/raw":
            path = safe_path(qs.get("p", [""])[0])
            if path is None:
                self._send(404, b"not found", "text/plain")
                return
            ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
            self._send(200, path.read_bytes(), ctype, cache=True)
            return

        self._send(404, b"not found", "text/plain")


def _primary_doc(d: Path) -> str:
    """The doc that best represents a directory — its README, SPEC, or first."""
    for stem in CFG.pinned:
        p = _find_stem(d, stem)
        if p is not None:
            return p.relative_to(ROOT).as_posix()
    shallow = sorted(p for p in d.glob("*") if p.suffix.lower() in DOC_SUFFIXES and p.is_file())
    deep = (
        p
        for suffix in DOC_SUFFIXES
        for p in sorted(d.rglob(f"*{suffix}"))
        if not set(p.parts) & CFG.skip_dirs
    )
    found = next(iter(shallow), None) or next(deep, None)
    return found.relative_to(ROOT).as_posix() if found else ""


def _resolve_start(arg: str | None) -> str:
    """A CLI target → the doc to open on load.

    Takes a file path, a directory (opens its primary doc), a numbered package
    (`10` → `projects/10-…`), a package-name fragment, or failing all of those
    a fuzzy match against every doc's path and title.
    """
    if not arg:
        return CFG.home
    p = ROOT / arg
    if p.is_file():
        return PurePosixPath(arg).as_posix()
    if p.is_dir() and (hit := _primary_doc(p)):
        return hit

    nn = f"{int(arg):02d}" if arg.isdigit() else ""
    for gdir in CFG.group_dirs:
        base = ROOT / gdir
        if not base.is_dir():
            continue
        hits = sorted(base.glob(f"{nn}-*")) if nn else []
        hits = hits or sorted(x for x in base.glob(f"*{arg}*") if x.is_dir())
        for h in hits:
            if doc := _primary_doc(h):
                return doc

    low = arg.lower()
    for g in build_tree():
        for d in g.docs:
            if low in d.rel.lower() or low in d.title.lower():
                return d.rel
    say(f"warning: nothing matched {arg!r} — opening the repo root", err=True)
    return CFG.home


def _bind(port: int, span: int = 20) -> tuple[ThreadingHTTPServer, int] | None:
    """Bind `port`, or the next free one — so several repos can be open at once."""
    last: OSError | None = None
    for candidate in range(port, port + span):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), Handler), candidate
        except OSError as e:
            last = e
    say(f"error: no free port in {port}–{port + span - 1} — {last}", err=True)
    return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tome",
        description="Read any repo's markdown in a browser tab. No dependencies, no network.",
        epilog="Homepage: https://github.com/utkarsh5026/tome",
    )
    ap.add_argument("target", nargs="?", help="a path, a directory, or a package number to open")
    ap.add_argument("--root", metavar="DIR", help="repo to serve (default: found from cwd)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TOME_PORT") or 7979),
                    help="preferred port; advances if taken (default: 7979)")
    ap.add_argument("--open", action="store_true", help="launch a browser tab")
    ap.add_argument("--init-config", action="store_true",
                    help=f"write a starter {CONFIG_NAME} and exit")
    ap.add_argument("--version", action="version", version=f"tome {__version__}")
    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    root = find_root(Path(args.root).expanduser() if args.root else Path.cwd())
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    if args.init_config:
        dest = root / CONFIG_NAME
        if dest.exists():
            say(f"{dest} already exists — leaving it alone", err=True)
            return 1
        dest.write_text(SAMPLE_CONFIG, encoding="utf-8")
        say(f"✅ wrote {dest}")
        return 0

    configure(load_config(root))
    tree = build_tree()
    total = sum(len(g.docs) for g in tree)
    if not total:
        print(f"no markdown found under {root}", file=sys.stderr)
        return 1

    bound = _bind(args.port)
    if bound is None:
        return 1
    srv, port = bound

    url = f"http://127.0.0.1:{port}/"
    if start := _resolve_start(args.target):
        url += f"#/{start}"

    say(f"📖 {CFG.name} docs → {url}")
    say(
        f"  {total} markdown file{'s' * (total != 1)} · live-reloads on save"
        " · ctrl-K to search · ctrl-C to stop"
    )
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        say("\n👋 bye")
        srv.shutdown()
    return 0


def cli() -> None:
    """Console-script entry point (`tome`)."""
    sys.exit(main(sys.argv[1:]))


# --------------------------------------------------------------------------- #
# The page. Inline HTML/CSS/JS so it works fully offline: no CDN, no build
# step, no fonts to fetch. The server fills in three {{…}} slots before
# sending it — everything else is static.
# --------------------------------------------------------------------------- #


# An emoji drawn into an inline SVG: a real favicon with no file to serve and
# no network fetch, so the tab is recognisable even fully offline. Repos pick
# their own via `"icon"`, which is what makes a row of tome tabs tellable apart.
FAVICON = (
    "data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 "
    "viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>{icon}</text></svg>"
)


def page_html() -> str:
    """The page with this repo's name, brand, icon, and settings baked in."""
    name = CFG.name
    head, dash, tail = name.partition("-")
    settings = json.dumps(
        {
            "name": name,
            "theme": CFG.theme or "mocha",
            "home": CFG.home or _primary_doc(ROOT),
            "editorUrl": CFG.editor_url,
            # Per-repo localStorage namespace, so "last doc I read" and which
            # sections are expanded don't leak between repos sharing a port.
            "repo": str(ROOT),
            "version": __version__,
        }
    ).replace("<", "\\u003c")  # so a "</script>" in a path cannot close the tag
    return (
        PAGE.replace("{{TITLE}}", _esc(name))
        .replace("{{BRAND}}", f'{_esc(head)}<span class="g">{_esc(dash + tail)}</span>')
        .replace("{{FAVICON}}", FAVICON.format(icon=_esc(CFG.icon, True)))
        .replace("{{SETTINGS}}", settings)
    )


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}} · docs</title>
<link rel="icon" href="{{FAVICON}}">
<script>
  const TOME = {{SETTINGS}};
  /* Preferences are shared across every repo you open; reading position is
     not. `K` builds a global key, `RK` a repo-scoped one.               */
  const K = (n) => "tome." + n;
  const RK = (n) => "tome." + TOME.repo + "." + n;
  /* Runs before first paint so a saved theme never flashes the default one. */
  try { document.documentElement.dataset.theme =
          localStorage.getItem(K("theme")) || TOME.theme; } catch (e) {}
</script>
<style>
  :root{
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,SFMono-Regular,"JetBrainsMono Nerd Font",Menlo,Consolas,monospace;
    --sidebar:290px; --toc:210px;
  }

  /* ── themes ───────────────────────────────────────────────────────────────
     Every colour in this page comes from these 18 variables, so a theme is
     just one block. They are written as plain [data-theme=…] (not :root[…])
     on purpose: the picker previews a theme by putting the attribute on a
     swatch card, which then inherits that palette for free — no duplicated
     hex codes in the JavaScript.                                          */

  /* dark */
  :root, [data-theme="mocha"]{
    --bg:#181825; --panel:#1e1e2e; --panel2:#11111b; --line:#313244; --line2:#45475a;
    --text:#cdd6f4; --sub:#9399b2; --dim:#6c7086; --strong:#e9edfb;
    --green:#a6e3a1; --red:#f38ba8; --yellow:#f9e2af; --peach:#fab387;
    --blue:#89b4fa; --sky:#89dceb; --mauve:#cba6f7; --pink:#f5c2e7; --teal:#94e2d5;
  }
  [data-theme="macchiato"]{
    --bg:#1e2030; --panel:#24273a; --panel2:#181926; --line:#363a4f; --line2:#494d64;
    --text:#cad3f5; --sub:#939ab7; --dim:#6e738d; --strong:#e6ecff;
    --green:#a6da95; --red:#ed8796; --yellow:#eed49f; --peach:#f5a97f;
    --blue:#8aadf4; --sky:#91d7e3; --mauve:#c6a0f6; --pink:#f5bde6; --teal:#8bd5ca;
  }
  [data-theme="tokyo"]{
    --bg:#1a1b26; --panel:#1f2335; --panel2:#16161e; --line:#292e42; --line2:#3b4261;
    --text:#c0caf5; --sub:#a9b1d6; --dim:#565f89; --strong:#e2e8ff;
    --green:#9ece6a; --red:#f7768e; --yellow:#e0af68; --peach:#ff9e64;
    --blue:#7aa2f7; --sky:#7dcfff; --mauve:#bb9af7; --pink:#ff75a0; --teal:#73daca;
  }
  [data-theme="nord"]{
    --bg:#2e3440; --panel:#3b4252; --panel2:#272c36; --line:#434c5e; --line2:#4c566a;
    --text:#d8dee9; --sub:#aeb8c8; --dim:#6b7689; --strong:#eceff4;
    --green:#a3be8c; --red:#bf616a; --yellow:#ebcb8b; --peach:#d08770;
    --blue:#81a1c1; --sky:#88c0d0; --mauve:#b48ead; --pink:#c9a3bd; --teal:#8fbcbb;
  }
  [data-theme="dracula"]{
    --bg:#282a36; --panel:#2f3140; --panel2:#21222c; --line:#44475a; --line2:#565a70;
    --text:#f8f8f2; --sub:#c3c6d4; --dim:#6272a4; --strong:#ffffff;
    --green:#50fa7b; --red:#ff5555; --yellow:#f1fa8c; --peach:#ffb86c;
    --blue:#bd93f9; --sky:#8be9fd; --mauve:#bd93f9; --pink:#ff79c6; --teal:#8be9fd;
  }
  [data-theme="gruvbox"]{
    --bg:#282828; --panel:#32302f; --panel2:#1d2021; --line:#3c3836; --line2:#504945;
    --text:#ebdbb2; --sub:#bdae93; --dim:#928374; --strong:#fbf1c7;
    --green:#b8bb26; --red:#fb4934; --yellow:#fabd2f; --peach:#fe8019;
    --blue:#83a598; --sky:#8ec07c; --mauve:#d3869b; --pink:#d3869b; --teal:#8ec07c;
  }
  [data-theme="rosepine"]{
    --bg:#191724; --panel:#1f1d2e; --panel2:#15131f; --line:#26233a; --line2:#403d52;
    --text:#e0def4; --sub:#908caa; --dim:#6e6a86; --strong:#f5f3ff;
    --green:#5dc2a3; --red:#eb6f92; --yellow:#f6c177; --peach:#ebbcba;
    --blue:#31748f; --sky:#9ccfd8; --mauve:#c4a7e7; --pink:#ebbcba; --teal:#9ccfd8;
  }
  [data-theme="onedark"]{
    --bg:#282c34; --panel:#2f343e; --panel2:#21252b; --line:#3e4451; --line2:#4b5263;
    --text:#abb2bf; --sub:#9199a6; --dim:#5c6370; --strong:#dfe3ea;
    --green:#98c379; --red:#e06c75; --yellow:#e5c07b; --peach:#d19a66;
    --blue:#61afef; --sky:#56b6c2; --mauve:#c678dd; --pink:#e06c95; --teal:#56b6c2;
  }
  [data-theme="kanagawa"]{
    --bg:#1f1f28; --panel:#2a2a37; --panel2:#16161d; --line:#363646; --line2:#54546d;
    --text:#dcd7ba; --sub:#c8c093; --dim:#727169; --strong:#f2ecdc;
    --green:#98bb6c; --red:#e46876; --yellow:#e6c384; --peach:#ffa066;
    --blue:#7e9cd8; --sky:#7fb4ca; --mauve:#957fb8; --pink:#d27e99; --teal:#6a9589;
  }
  [data-theme="everforest"]{
    --bg:#2d353b; --panel:#343f44; --panel2:#232a2e; --line:#3d484d; --line2:#4f585e;
    --text:#d3c6aa; --sub:#9da9a0; --dim:#859289; --strong:#e8ded0;
    --green:#a7c080; --red:#e67e80; --yellow:#dbbc7f; --peach:#e69875;
    --blue:#7fbbb3; --sky:#83c092; --mauve:#d699b6; --pink:#d699b6; --teal:#83c092;
  }
  [data-theme="nightowl"]{
    --bg:#011627; --panel:#0b2942; --panel2:#001122; --line:#1d3b53; --line2:#2c4f6b;
    --text:#d6deeb; --sub:#a3b3cc; --dim:#637777; --strong:#ffffff;
    --green:#addb67; --red:#ef5350; --yellow:#ecc48d; --peach:#f78c6c;
    --blue:#82aaff; --sky:#7fdbca; --mauve:#c792ea; --pink:#ff6ac1; --teal:#7fdbca;
  }
  [data-theme="solarized"]{
    --bg:#002b36; --panel:#073642; --panel2:#00212b; --line:#12414d; --line2:#2b5b66;
    --text:#93a1a1; --sub:#839496; --dim:#657b83; --strong:#eee8d5;
    --green:#859900; --red:#dc322f; --yellow:#b58900; --peach:#cb4b16;
    --blue:#268bd2; --sky:#2aa198; --mauve:#6c71c4; --pink:#d33682; --teal:#2aa198;
  }
  [data-theme="ayu"]{
    --bg:#1f2430; --panel:#232834; --panel2:#1a1f29; --line:#2d3441; --line2:#3d4552;
    --text:#cbccc6; --sub:#a8adb5; --dim:#707a8c; --strong:#f0f2ee;
    --green:#bae67e; --red:#f28779; --yellow:#ffd580; --peach:#ffa759;
    --blue:#73d0ff; --sky:#95e6cb; --mauve:#d4bfff; --pink:#f29e74; --teal:#95e6cb;
  }
  [data-theme="monokai"]{
    --bg:#272822; --panel:#2f302a; --panel2:#1e1f1a; --line:#3e3d32; --line2:#55564a;
    --text:#f8f8f2; --sub:#c8c8bd; --dim:#75715e; --strong:#ffffff;
    --green:#a6e22e; --red:#f92672; --yellow:#e6db74; --peach:#fd971f;
    --blue:#66d9ef; --sky:#66d9ef; --mauve:#ae81ff; --pink:#f92672; --teal:#a1efe4;
  }

  /* light */
  [data-theme="latte"]{
    --bg:#eff1f5; --panel:#e6e9ef; --panel2:#dce0e8; --line:#ccd0da; --line2:#bcc0cc;
    --text:#4c4f69; --sub:#6c6f85; --dim:#9ca0b0; --strong:#33364d;
    --green:#40a02b; --red:#d20f39; --yellow:#df8e1d; --peach:#fe640b;
    --blue:#1e66f5; --sky:#04a5e5; --mauve:#8839ef; --pink:#ea76cb; --teal:#179299;
  }
  [data-theme="github"]{
    --bg:#ffffff; --panel:#f6f8fa; --panel2:#f0f3f6; --line:#d0d7de; --line2:#afb8c1;
    --text:#1f2328; --sub:#656d76; --dim:#8c959f; --strong:#010409;
    --green:#1a7f37; --red:#cf222e; --yellow:#9a6700; --peach:#bc4c00;
    --blue:#0969da; --sky:#0550ae; --mauve:#8250df; --pink:#bf3989; --teal:#1b7c83;
  }
  [data-theme="solarized-light"]{
    --bg:#fdf6e3; --panel:#eee8d5; --panel2:#f5efdc; --line:#e0d8c0; --line2:#c9c2ab;
    --text:#586e75; --sub:#657b83; --dim:#93a1a1; --strong:#073642;
    --green:#859900; --red:#dc322f; --yellow:#b58900; --peach:#cb4b16;
    --blue:#268bd2; --sky:#2aa198; --mauve:#6c71c4; --pink:#d33682; --teal:#2aa198;
  }
  [data-theme="rosepine-dawn"]{
    --bg:#faf4ed; --panel:#fffaf3; --panel2:#f2e9e1; --line:#dfd9d3; --line2:#cecacd;
    --text:#575279; --sub:#797593; --dim:#9893a5; --strong:#3d3a54;
    --green:#3f8f7d; --red:#b4637a; --yellow:#ea9d34; --peach:#d7827e;
    --blue:#286983; --sky:#56949f; --mauve:#907aa9; --pink:#d7827e; --teal:#56949f;
  }
  [data-theme="gruvbox-light"]{
    --bg:#fbf1c7; --panel:#f2e5bc; --panel2:#ebdbb2; --line:#d5c4a1; --line2:#bdae93;
    --text:#3c3836; --sub:#665c54; --dim:#7c6f64; --strong:#282828;
    --green:#79740e; --red:#9d0006; --yellow:#b57614; --peach:#af3a03;
    --blue:#076678; --sky:#427b58; --mauve:#8f3f71; --pink:#b16286; --teal:#427b58;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--text);font:15px/1.7 var(--sans);
    -webkit-font-smoothing:antialiased}
  ::selection{background:color-mix(in srgb, var(--blue) 28%, transparent)}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px}
  ::-webkit-scrollbar-thumb:hover{background:var(--line2)}
  ::-webkit-scrollbar-track{background:transparent}

  /* ── layout ───────────────────────────────────────────────────────────── */
  .app{display:grid;grid-template-columns:var(--sidebar) minmax(0,1fr);min-height:100vh}
  .app.nonav{grid-template-columns:minmax(0,1fr)}
  .app.nonav aside{display:none}

  aside{position:sticky;top:0;height:100vh;overflow:hidden;background:var(--panel2);
    border-right:1px solid var(--line);display:flex;flex-direction:column}
  .brand{padding:16px 18px 12px;display:flex;align-items:center;gap:9px;flex:none}
  .brand b{font-size:14px;font-weight:600;letter-spacing:.2px}
  .brand .g{color:var(--dim);font-weight:400}
  .brand .sp{margin-left:auto;font-size:11px;color:var(--dim);font-family:var(--mono)}
  .filter{padding:0 14px 12px;flex:none}
  .filter input{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--text);
    border-radius:9px;padding:8px 11px;font:13px var(--sans);outline:none}
  .filter input:focus{border-color:var(--line2)}
  .filter input::placeholder{color:var(--dim)}
  nav{overflow-y:auto;padding:0 8px 40px;flex:1}

  .grp{margin-bottom:2px}
  .ghead{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:8px;
    cursor:pointer;user-select:none;font-size:12.5px;color:var(--sub);
    text-transform:lowercase;letter-spacing:.3px}
  .ghead:hover{background:color-mix(in srgb, var(--text) 6%, transparent);color:var(--text)}
  .ghead .chev{color:var(--dim);font-size:9px;width:8px;flex:none;transition:transform .15s}
  .ghead.open .chev{transform:rotate(90deg)}
  .ghead .num{font-family:var(--mono);color:var(--dim);font-size:11px}
  .ghead .st{width:6px;height:6px;border-radius:50%;margin-left:auto;flex:none;
    background:var(--line2)}
  .st.active{background:var(--green)} .st.done{background:var(--blue)}
  .st.paused{background:var(--yellow)} .st.blocked{background:var(--red)}
  .st.not-started{background:var(--line2)}
  .gitems{display:none;margin:2px 0 8px 9px;padding-left:9px;border-left:1px solid var(--line)}
  .grp.open .gitems{display:block}

  a.item{display:flex;gap:8px;align-items:baseline;padding:5px 9px;border-radius:7px;
    color:var(--sub);text-decoration:none;font-size:13px;line-height:1.45;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  a.item:hover{background:color-mix(in srgb, var(--text) 7%, transparent);color:var(--text)}
  a.item.on{background:color-mix(in srgb, var(--blue) 15%, transparent);color:var(--blue)}
  a.item .k{font-family:var(--mono);font-size:10px;color:var(--dim);flex:none}
  a.item.on .k{color:var(--blue)}
  a.item.kSPEC .k{color:var(--peach)} a.item.kCONCEPTS .k{color:var(--mauve)}
  a.item.kRESEARCH .k{color:var(--teal)}

  /* ── main ─────────────────────────────────────────────────────────────── */
  main{min-width:0;display:flex;flex-direction:column}
  .bar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;
    padding:11px 26px;backdrop-filter:blur(10px);
    background:color-mix(in srgb, var(--bg) 88%, transparent);
    border-bottom:1px solid var(--line);font-size:12.5px;min-height:46px}
  .crumb{font-family:var(--mono);color:var(--dim);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis}
  .crumb b{color:var(--sub);font-weight:500}
  .bar .sp{margin-left:auto}
  .bar button,.bar a.btn{background:transparent;border:1px solid var(--line);color:var(--sub);
    border-radius:8px;padding:4px 11px;font:12px var(--sans);cursor:pointer;
    text-decoration:none;white-space:nowrap}
  .bar button:hover,.bar a.btn:hover{border-color:var(--line2);color:var(--text)}
  .bar button.ico{padding:4px 9px;font-size:13px;line-height:1;color:var(--dim)}
  .bar button.ico:hover{color:var(--text)}
  /* "off" = that panel is currently hidden */
  .bar button.ico.off{border-color:transparent;color:var(--line2)}
  .bar button.on{border-color:var(--blue);color:var(--blue)}
  kbd{font:11px var(--mono);background:var(--panel);border:1px solid var(--line);
    border-bottom-width:2px;border-radius:5px;padding:1px 5px;color:var(--sub)}

  .wrap{display:grid;grid-template-columns:minmax(0,1fr) var(--toc);gap:34px;
    padding:34px 30px 90px;max-width:1240px;width:100%;margin-inline:auto}
  /* No TOC column — centre the reading column instead of pinning it left. */
  .wrap.notoc{grid-template-columns:minmax(0,1fr)}
  .wrap.notoc .toc{display:none}
  .wrap.notoc article{margin-inline:auto}
  article{min-width:0;max-width:78ch}

  /* ── typography ───────────────────────────────────────────────────────── */
  article h1,article h2,article h3,article h4,article h5,article h6{
    line-height:1.3;font-weight:650;scroll-margin-top:70px;position:relative}
  article h1{font-size:29px;margin:6px 0 22px;letter-spacing:-.4px}
  article h2{font-size:21px;margin:44px 0 14px;padding-bottom:8px;
    border-bottom:1px solid var(--line);letter-spacing:-.2px}
  article h3{font-size:17px;margin:32px 0 10px;color:var(--text)}
  article h4{font-size:15px;margin:24px 0 8px;color:var(--sub)}
  .hlink{position:absolute;left:-20px;color:var(--line2);text-decoration:none;opacity:0;
    font-weight:400;transition:opacity .12s}
  h1:hover .hlink,h2:hover .hlink,h3:hover .hlink,h4:hover .hlink{opacity:1}
  .hlink:hover{color:var(--blue)}
  article p{margin:0 0 16px}
  article strong{color:var(--strong);font-weight:640}
  article em{color:var(--pink);font-style:italic}
  article del{color:var(--dim)}
  article hr{border:0;border-top:1px solid var(--line);margin:34px 0}
  article a{color:var(--blue);text-decoration:none;
    border-bottom:1px solid color-mix(in srgb, var(--blue) 32%, transparent)}
  article a:hover{border-bottom-color:var(--blue)}
  article a.l-src{color:var(--teal);
    border-bottom-color:color-mix(in srgb, var(--teal) 34%, transparent)}
  article a.l-ext{color:var(--mauve);
    border-bottom-color:color-mix(in srgb, var(--mauve) 32%, transparent)}
  article a.l-miss{color:var(--red);border-bottom:1px dotted var(--red)}
  article img{max-width:100%;border-radius:10px;margin:10px 0}

  article ul,article ol{margin:0 0 16px;padding-left:24px}
  article li{margin:5px 0}
  article li::marker{color:var(--dim)}
  ul.tasks{list-style:none;padding-left:4px}
  ul.tasks li.task{display:flex;gap:9px;align-items:flex-start;margin:7px 0}
  .box{flex:none;width:16px;height:16px;border-radius:5px;border:1px solid var(--line2);
    display:inline-flex;align-items:center;justify-content:center;font-size:10px;
    margin-top:4px;color:var(--bg);font-family:var(--mono)}
  .box.done{background:var(--green);border-color:var(--green)}
  .box.open-field{border-color:var(--teal);color:var(--teal);background:transparent}
  li.task.done{color:var(--sub)}

  article code{font-family:var(--mono);font-size:.875em;background:var(--panel);
    border:1px solid var(--line);border-radius:5px;padding:1px 5px;color:var(--peach)}
  article a code{color:inherit}
  .cb{position:relative;margin:0 0 20px;background:var(--panel2);border:1px solid var(--line);
    border-radius:11px;overflow:hidden}
  .cb pre{margin:0;padding:15px 17px;overflow-x:auto;font-family:var(--mono);
    font-size:13px;line-height:1.65}
  .cb code{background:none;border:0;padding:0;color:var(--text);font-size:13px}
  .cb .lang{position:absolute;top:8px;right:60px;font:10px var(--mono);color:var(--dim);
    text-transform:uppercase;letter-spacing:.6px;pointer-events:none;max-width:55%;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cb .lang.path{text-transform:none;letter-spacing:0;color:var(--line2)}
  .cb .copy{position:absolute;top:6px;right:8px;background:var(--panel);
    border:1px solid var(--line);color:var(--dim);border-radius:6px;padding:2px 8px;
    font:11px var(--sans);cursor:pointer;opacity:0;transition:opacity .12s}
  .cb:hover .copy{opacity:1}
  .cb .copy:hover{color:var(--text);border-color:var(--line2)}
  .cb.src{max-width:none}
  .cb.src pre{font-size:12.5px;line-height:1.6}

  /* Notebook cell output. Quieter than the code that produced it, and hung off
     the bottom of it so it reads as belonging to that cell rather than standing
     on its own. Plots get a white plate — matplotlib writes transparent PNGs,
     which are invisible on every one of the dark themes. */
  .cb.out{background:transparent;border:0;border-left:2px solid var(--line2);
    border-radius:0;margin:-12px 0 20px 2px}
  .cb.out pre{padding:9px 15px;font-size:12.5px;color:var(--sub);max-height:340px;overflow:auto}
  .cb.out.err{border-left-color:var(--red)}
  .cb.out.err pre{color:var(--red)}
  .nb-img{margin:-6px 0 20px}
  .nb-img img{max-width:100%;border-radius:8px;background:#fff}

  .c-kw{color:var(--mauve)} .c-str{color:var(--green)} .c-num{color:var(--peach)}
  .c-cmt{color:var(--dim);font-style:italic} .c-ty{color:var(--yellow)}
  .c-fn{color:var(--blue)} .c-attr{color:var(--teal)} .c-life{color:var(--pink)}
  .c-flag{color:var(--sky)} .c-var{color:var(--sky)}

  blockquote.note{margin:0 0 18px;padding:12px 18px;border-left:3px solid var(--line2);
    background:color-mix(in srgb, var(--text) 4%, transparent);
    border-radius:0 9px 9px 0;color:var(--sub)}
  blockquote.note p:last-child{margin-bottom:0}
  blockquote.note strong{color:var(--text)}
  blockquote.tip{border-left-color:var(--green)} blockquote.warning{border-left-color:var(--yellow)}
  blockquote.important{border-left-color:var(--mauve)}
  blockquote.caution{border-left-color:var(--red)}

  .tw{overflow-x:auto;margin:0 0 20px;border:1px solid var(--line);border-radius:11px}
  table{border-collapse:collapse;width:100%;font-size:13.5px}
  th,td{padding:9px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
  th{background:var(--panel2);color:var(--sub);font-weight:600;font-size:12px;
    text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
  tbody tr:last-child td{border-bottom:0}
  tbody tr:hover{background:color-mix(in srgb, var(--text) 4%, transparent)}
  td.a-right,th.a-right{text-align:right} td.a-center,th.a-center{text-align:center}
  pre.mermaid{background:var(--panel2);border:1px solid var(--line);border-radius:11px;
    padding:15px 17px;overflow-x:auto;font:12.5px/1.6 var(--mono);color:var(--sub);margin:0 0 20px}

  /* ── toc ──────────────────────────────────────────────────────────────── */
  .toc{position:sticky;top:74px;align-self:start;max-height:calc(100vh - 110px);
    overflow-y:auto;font-size:12.5px;padding-bottom:20px}
  .toc .th{color:var(--dim);text-transform:uppercase;letter-spacing:.7px;font-size:10px;
    margin-bottom:9px;font-weight:600}
  .toc a{display:block;color:var(--sub);text-decoration:none;padding:3px 0 3px 11px;
    border-left:2px solid var(--line);line-height:1.45}
  .toc a:hover{color:var(--text);border-left-color:var(--line2)}
  .toc a.on{color:var(--blue);border-left-color:var(--blue)}
  .toc a.l3{padding-left:22px;font-size:12px;color:var(--dim)}
  .toc a.l3:hover,.toc a.l3.on{color:var(--blue)}

  /* ── prev / next ──────────────────────────────────────────────────────── */
  .pn{display:flex;gap:14px;margin-top:56px;padding-top:22px;border-top:1px solid var(--line)}
  .pn a{flex:1;padding:13px 16px;border:1px solid var(--line);border-radius:11px;
    text-decoration:none;color:var(--text);border-bottom-width:1px}
  .pn a:hover{border-color:var(--line2);background:var(--panel)}
  .pn .d{display:block;font-size:11px;color:var(--dim);margin-bottom:3px;font-family:var(--mono)}
  .pn a.next{text-align:right}

  /* ── theme picker ─────────────────────────────────────────────────────── */
  .tpop{position:fixed;top:54px;right:24px;z-index:80;width:min(600px,93vw);
    background:var(--panel);border:1px solid var(--line2);border-radius:14px;
    padding:13px;display:none;max-height:74vh;overflow-y:auto;
    box-shadow:0 20px 60px rgba(0,0,0,.45)}
  .tpop.on{display:block}
  .tsec{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);
    margin:5px 4px 8px;font-weight:600}
  .tgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));
    gap:8px;margin-bottom:10px}
  /* Each card carries its own data-theme, so these vars resolve to that
     palette — the swatch is a real preview, not a hand-copied colour list. */
  .tcard{background:var(--bg);border:1px solid var(--line);border-radius:10px;
    padding:9px 11px;cursor:pointer;display:flex;flex-direction:column;gap:8px;
    transition:border-color .12s}
  .tcard:hover{border-color:var(--line2)}
  .tcard.on{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue) inset}
  .tname{font-size:12.5px;color:var(--text);display:flex;align-items:center;gap:6px}
  .tname .tick{margin-left:auto;color:var(--green);font-size:11px}
  .tdots{display:flex;gap:5px}
  .tdots i{width:13px;height:13px;border-radius:50%;display:block}
  .thint{color:var(--dim);font-size:11px;padding:2px 5px 4px}

  /* ── command palette ──────────────────────────────────────────────────── */
  .scrim{position:fixed;inset:0;backdrop-filter:blur(3px);
    background:color-mix(in srgb, var(--panel2) 78%, transparent);
    z-index:90;display:none;padding-top:11vh;justify-content:center}
  .scrim.on{display:flex}
  .pal{width:min(720px,92vw);max-height:74vh;background:var(--panel);border:1px solid var(--line2);
    border-radius:15px;overflow:hidden;display:flex;flex-direction:column;
    box-shadow:0 24px 70px rgba(0,0,0,.45)}
  .pal input{width:100%;background:transparent;border:0;border-bottom:1px solid var(--line);
    color:var(--text);padding:16px 20px;font:15px var(--sans);outline:none}
  .pal input::placeholder{color:var(--dim)}
  .pres{overflow-y:auto;padding:7px}
  .pr{display:block;padding:9px 13px;border-radius:9px;text-decoration:none;color:var(--text);
    cursor:pointer}
  .pr.sel{background:color-mix(in srgb, var(--blue) 16%, transparent)}
  .pr .t{font-size:13.5px;display:flex;gap:9px;align-items:baseline}
  .pr .t .k{font:10px var(--mono);color:var(--dim);flex:none}
  .pr .p{font:11px var(--mono);color:var(--dim);margin-top:2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pr .sn{font:12px var(--mono);color:var(--sub);margin-top:4px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pr .sn mark{background:color-mix(in srgb, var(--yellow) 25%, transparent);
    color:var(--yellow);border-radius:3px}
  .phint{padding:8px 16px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;
    display:flex;gap:14px}

  article mark.flash{background:color-mix(in srgb, var(--yellow) 32%, transparent);
    color:inherit;border-radius:3px;transition:all .5s;
    box-shadow:0 0 0 3px color-mix(in srgb, var(--yellow) 18%, transparent)}
  article mark{background:color-mix(in srgb, var(--yellow) 24%, transparent);
    color:inherit;border-radius:3px}
  .empty{color:var(--dim);padding:60px 0;text-align:center}
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--panel);
    border:1px solid var(--line2);border-radius:10px;padding:9px 18px;font-size:12.5px;
    color:var(--sub);opacity:0;transition:opacity .2s;pointer-events:none;z-index:95}
  .toast.on{opacity:1}

  @media(max-width:1150px){
    .wrap{grid-template-columns:minmax(0,1fr)} .toc{display:none}
    article{margin-inline:auto}
  }
  @media(max-width:820px){ .app{grid-template-columns:1fr} aside{display:none} }
</style>
</head>
<body>
<div class="app" id="app">
  <aside>
    <div class="brand">
      <b>{{BRAND}}</b>
      <span class="sp" id="count"></span>
    </div>
    <div class="filter"><input id="filter" placeholder="filter files…" spellcheck="false"></div>
    <nav id="nav"></nav>
  </aside>
  <main>
    <div class="bar">
      <button class="ico" id="navBtn" title="Show/hide the file list (s)">☰</button>
      <div class="crumb" id="crumb">loading…</div>
      <span class="sp"></span>
      <button id="searchBtn">search <kbd>⌘K</kbd></button>
      <button class="ico" id="tocBtn" title="Show/hide the page outline (t)">⋮≡</button>
      <button class="ico" id="themeBtn" title="Change theme (,)">◐</button>
      <button id="zenBtn" title="Hide both panels and centre the text (\)">zen</button>
      <a class="btn" id="editBtn" href="#" title="Open this file in VS Code">edit</a>
    </div>
    <div class="tpop" id="tpop"></div>
    <div class="wrap" id="wrap">
      <article id="doc"><div class="empty">loading…</div></article>
      <div class="toc" id="toc"></div>
    </div>
  </main>
</div>

<div class="scrim" id="scrim">
  <div class="pal">
    <input id="q" spellcheck="false"
      placeholder="Jump to a doc, or type 3+ chars to search inside all docs…">
    <div class="pres" id="pres"></div>
    <div class="phint"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span>
      <span><kbd>esc</kbd> close</span><span><kbd>s</kbd> files</span>
      <span><kbd>t</kbd> outline</span><span><kbd>\</kbd> zen</span></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
let TREE = [], FLAT = [], CUR = null, VERSION = "", SEL = 0, RESULTS = [], PENDING_HIT = null;

/* ── chrome (the two side panels) ────────────────────────────────────────────
   Two independent, remembered preferences. HAS_TOC is a property of the open
   document, so it is kept apart from the user's choice — otherwise navigating
   to the next doc would silently undo a hidden outline. "zen" is not a third
   state: it simply means both panels are hidden.                            */
let HIDE_NAV = localStorage.getItem(K("hideNav")) === "1";
let HIDE_TOC = localStorage.getItem(K("hideToc")) === "1";
let HAS_TOC = false;

function applyChrome(){
  const zen = HIDE_NAV && HIDE_TOC;
  $("#app").classList.toggle("nonav", HIDE_NAV);
  $("#wrap").classList.toggle("notoc", HIDE_TOC || !HAS_TOC);
  $("#navBtn").classList.toggle("off", HIDE_NAV);
  $("#tocBtn").classList.toggle("off", HIDE_TOC);
  $("#tocBtn").disabled = !HAS_TOC;
  $("#tocBtn").style.opacity = HAS_TOC ? "" : ".35";
  $("#zenBtn").classList.toggle("on", zen);
  $("#zenBtn").textContent = zen ? "exit zen" : "zen";
  localStorage.setItem(K("hideNav"), HIDE_NAV ? "1" : "");
  localStorage.setItem(K("hideToc"), HIDE_TOC ? "1" : "");
}

/* ── themes ──────────────────────────────────────────────────────────────────
   id, label, and whether it is a light or dark palette. The colours live in
   the stylesheet; this list only decides what appears in the picker.       */
const THEMES = [
  ["mocha", "Catppuccin Mocha", "dark"],
  ["macchiato", "Catppuccin Macchiato", "dark"],
  ["tokyo", "Tokyo Night", "dark"],
  ["nord", "Nord", "dark"],
  ["dracula", "Dracula", "dark"],
  ["gruvbox", "Gruvbox Dark", "dark"],
  ["rosepine", "Rosé Pine", "dark"],
  ["onedark", "One Dark", "dark"],
  ["kanagawa", "Kanagawa", "dark"],
  ["everforest", "Everforest", "dark"],
  ["nightowl", "Night Owl", "dark"],
  ["solarized", "Solarized Dark", "dark"],
  ["ayu", "Ayu Mirage", "dark"],
  ["monokai", "Monokai", "dark"],
  ["latte", "Catppuccin Latte", "light"],
  ["github", "GitHub Light", "light"],
  ["solarized-light", "Solarized Light", "light"],
  ["rosepine-dawn", "Rosé Pine Dawn", "light"],
  ["gruvbox-light", "Gruvbox Light", "light"],
];
let THEME = localStorage.getItem(K("theme")) || TOME.theme;

const previewTheme = (id) => { document.documentElement.dataset.theme = id; };

function commitTheme(id){
  THEME = id;
  localStorage.setItem(K("theme"), id);
  previewTheme(id);
  markThemes();
}

function markThemes(){
  document.querySelectorAll(".tcard").forEach(c => {
    const on = c.dataset.theme === THEME;
    c.classList.toggle("on", on);
    const tick = c.querySelector(".tick");
    if (tick) tick.textContent = on ? "✓" : "";
  });
}

function renderThemes(){
  const card = (t) =>
    `<div class="tcard" data-theme="${t[0]}"><div class="tname">${esc(t[1])}` +
    `<span class="tick"></span></div><div class="tdots">` +
    ["--blue", "--green", "--yellow", "--red", "--mauve", "--panel2"]
      .map(v => `<i style="background:var(${v})"></i>`).join("") +
    `</div></div>`;
  const group = (mode) => `<div class="tsec">${mode}</div><div class="tgrid">` +
    THEMES.filter(t => t[2] === mode).map(card).join("") + "</div>";
  $("#tpop").innerHTML = group("dark") + group("light") +
    '<div class="thint">hover to preview · click to keep · esc to cancel</div>';
  document.querySelectorAll(".tcard").forEach(c => {
    c.onmouseenter = () => previewTheme(c.dataset.theme);
    c.onclick = () => { commitTheme(c.dataset.theme); themePop(false); };
  });
  $("#tpop").onmouseleave = () => previewTheme(THEME);
  markThemes();
}

/* Closing without a click reverts whatever was being previewed. */
function themePop(show){
  const el = $("#tpop");
  const on = show === undefined ? !el.classList.contains("on") : show;
  el.classList.toggle("on", on);
  $("#themeBtn").classList.toggle("on", on);
  on ? markThemes() : previewTheme(THEME);
}

function toggleNav(){ HIDE_NAV = !HIDE_NAV; applyChrome(); }
function toggleToc(){ if (HAS_TOC){ HIDE_TOC = !HIDE_TOC; applyChrome(); } }
/* Zen hides both; pressing it again brings both back. */
function toggleZen(){
  const zen = HIDE_NAV && HIDE_TOC;
  HIDE_NAV = HIDE_TOC = !zen;
  applyChrome();
}

/* ── data ───────────────────────────────────────────────────────────────── */
async function loadTree(){
  const r = await fetch("/api/tree").then(r => r.json());
  TREE = r.groups; VERSION = r.version;
  FLAT = [];
  for (const g of TREE) for (const d of g.docs) FLAT.push({...d, group: g.title, gid: g.id});
  $("#count").textContent = FLAT.length + " docs";
  renderNav();
}

function renderNav(){
  const filter = $("#filter").value.trim().toLowerCase();
  const nav = $("#nav");
  nav.innerHTML = "";
  const saved = JSON.parse(localStorage.getItem(RK("open")) || "{}");
  for (const g of TREE){
    const docs = filter
      ? g.docs.filter(d => (d.title + " " + d.path + " " + d.label).toLowerCase().includes(filter))
      : g.docs;
    if (!docs.length) continue;
    const isOpen = filter ? true : (saved[g.id] ?? defaultOpen(g));
    const div = document.createElement("div");
    div.className = "grp" + (isOpen ? " open" : "");
    div.innerHTML =
      `<div class="ghead${isOpen ? " open" : ""}"><span class="chev">▶</span>` +
      (g.num ? `<span class="num">${g.num}</span>` : "") +
      `<span>${esc(g.title)}</span>` +
      (g.state ? `<span class="st ${esc(g.state)}" title="${esc(g.state)}"></span>` : "") +
      `</div><div class="gitems">` +
      docs.map(d =>
        `<a class="item k${d.kind}${d.path === CUR ? " on" : ""}" href="#/${d.path}">` +
        `<span class="k">${d.kind === "doc" ? "·" : d.kind[0]}</span>` +
        `<span>${esc(d.label)}</span></a>`).join("") +
      `</div>`;
    div.querySelector(".ghead").onclick = () => {
      const now = !div.classList.contains("open");
      div.classList.toggle("open", now);
      div.querySelector(".ghead").classList.toggle("open", now);
      const st = JSON.parse(localStorage.getItem(RK("open")) || "{}");
      st[g.id] = now; localStorage.setItem(RK("open"), JSON.stringify(st));
    };
    nav.appendChild(div);
  }
}
/* Open the section holding the current doc; otherwise only the repo-root one.
   `prefix` comes from the server, which is the only side that knows how this
   particular repo was grouped.                                             */
const defaultOpen = (g) =>
  Boolean(g.prefix && CUR && CUR.startsWith(g.prefix)) || g.id === "root";
const ESCAPES = {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;"};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ESCAPES[c]);

/* ── routing ────────────────────────────────────────────────────────────── */
function route(){
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  if (!h){
    const last = localStorage.getItem(RK("last"));
    if (last){ location.hash = "#/" + last; return; }
    return openDoc(TOME.home || (FLAT[0] && FLAT[0].path) || "README.md");
  }
  const [path, anchor] = h.split("::");
  openDoc(path, anchor);
}

async function openDoc(path, anchor, keepScroll){
  const scroll = keepScroll ? window.scrollY : 0;
  const d = await fetch("/api/doc?p=" + encodeURIComponent(path)).then(r => r.json());
  if (d.error){
    $("#doc").innerHTML = `<div class="empty">${esc(d.error)}</div>`;
    $("#crumb").textContent = path;
    HAS_TOC = false; $("#toc").innerHTML = ""; applyChrome();
    return;
  }
  CUR = d.path;
  localStorage.setItem(RK("last"), d.path);
  document.title = d.title + " · gauntlet docs";
  const parts = d.path.split("/");
  $("#crumb").innerHTML = parts.map((p, i) =>
    i === parts.length - 1 ? `<b>${esc(p)}</b>` : esc(p)).join(" / ");
  const ed = $("#editBtn");
  ed.href = TOME.editorUrl ? TOME.editorUrl.replace("{path}", d.abs) : "#";
  ed.style.display = TOME.editorUrl ? "" : "none";
  $("#doc").innerHTML = d.html + prevNext(d.path);
  buildToc(d.toc);
  wireCode();
  if (d.mermaid) loadMermaid();
  renderNav();
  document.querySelector(".item.on")?.scrollIntoView({block:"nearest"});
  if (keepScroll) window.scrollTo(0, scroll);
  else if (anchor){
    const el = document.getElementById(anchor);
    el ? el.scrollIntoView() : window.scrollTo(0, 0);
  } else window.scrollTo(0, 0);
  if (PENDING_HIT){ const q = PENDING_HIT; PENDING_HIT = null; scrollToText(q); }
}

/* After opening a full-text hit, land on the match instead of the page top. */
function scrollToText(q){
  const needle = q.toLowerCase();
  const walk = document.createTreeWalker($("#doc"), NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walk.nextNode())){
    const at = node.textContent.toLowerCase().indexOf(needle);
    if (at < 0) continue;
    const range = document.createRange();
    range.setStart(node, at);
    range.setEnd(node, at + needle.length);
    const mark = document.createElement("mark");
    mark.className = "flash";
    try { range.surroundContents(mark); } catch(_) { return; }
    mark.scrollIntoView({block: "center"});
    setTimeout(() => mark.classList.remove("flash"), 2200);
    return;
  }
}

function prevNext(path){
  const i = FLAT.findIndex(d => d.path === path);
  if (i < 0) return "";
  const p = FLAT[i - 1], n = FLAT[i + 1];
  let h = '<div class="pn">';
  const gap = "<span style='flex:1'></span>";
  h += p ? `<a href="#/${p.path}"><span class="d">← previous</span>${esc(p.title)}</a>` : gap;
  h += n ? `<a class="next" href="#/${n.path}"><span class="d">next →</span>${esc(n.title)}</a>`
         : gap;
  return h + "</div>";
}

function buildToc(toc){
  const el = $("#toc");
  HAS_TOC = Boolean(toc && toc.length >= 2);
  if (!HAS_TOC){ el.innerHTML = ""; applyChrome(); return; }
  applyChrome();
  el.innerHTML = '<div class="th">on this page</div>' + toc.map(t =>
    `<a class="l${t.level}" href="#${t.id}" data-id="${t.id}">${esc(t.text)}</a>`).join("");
  el.querySelectorAll("a").forEach(a => a.onclick = (e) => {
    e.preventDefault();
    document.getElementById(a.dataset.id)?.scrollIntoView();
  });
  observeHeadings(toc);
}

let OBS = null;
function observeHeadings(toc){
  OBS?.disconnect();
  const links = new Map([...$("#toc").querySelectorAll("a")].map(a => [a.dataset.id, a]));
  OBS = new IntersectionObserver((entries) => {
    for (const e of entries){
      if (!e.isIntersecting) continue;
      links.forEach(a => a.classList.remove("on"));
      links.get(e.target.id)?.classList.add("on");
      break;
    }
  }, {rootMargin: "-70px 0px -75% 0px"});
  toc.forEach(t => { const el = document.getElementById(t.id); if (el) OBS.observe(el); });
}

function wireCode(){
  document.querySelectorAll(".cb .copy").forEach(b => b.onclick = () => {
    navigator.clipboard.writeText(b.parentElement.querySelector("code").innerText);
    toast("copied");
  });
}

let toastT;
function toast(msg){
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("on"), 1400);
}

/* Mermaid is the one thing we cannot render offline — try the CDN, and if it is
   not reachable the diagram just stays readable as its source text. */
let mermaidTried = false;
function loadMermaid(){
  if (window.mermaid){ window.mermaid.run({querySelector:"pre.mermaid"}); return; }
  if (mermaidTried) return;
  mermaidTried = true;
  // Diagrams follow the active theme — read the live variables rather than
  // baking in one palette.
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  const light = (THEMES.find(t => t[0] === THEME) || [])[2] === "light";
  const s = document.createElement("script");
  s.type = "module";
  s.textContent =
    'import m from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";' +
    `window.mermaid = m; m.initialize({startOnLoad:false, theme:"${light ? "default" : "dark"}",` +
    `themeVariables:{background:"${v("--panel2")}",primaryColor:"${v("--panel")}",` +
    `lineColor:"${v("--dim")}",primaryTextColor:"${v("--text")}",` +
    `primaryBorderColor:"${v("--line2")}"}});` +
    'm.run({querySelector:"pre.mermaid"});';
  document.head.appendChild(s);
}

/* ── command palette ────────────────────────────────────────────────────── */
function fuzzy(q, s){
  q = q.toLowerCase(); s = s.toLowerCase();
  let qi = 0, score = 0, streak = 0;
  for (let i = 0; i < s.length && qi < q.length; i++){
    if (s[i] === q[qi]){ qi++; streak++; score += streak * 2 + (i === 0 ? 6 : 0); }
    else streak = 0;
  }
  return qi === q.length ? score - s.length * 0.04 : -1;
}

let searchT;
function palette(show){
  $("#scrim").classList.toggle("on", show);
  if (show){ $("#q").value = ""; $("#q").focus(); paletteRender(""); }
}

function paletteRender(q){
  const list = $("#pres");
  if (!q){
    RESULTS = FLAT.slice(0, 40).map(d => ({...d, _hit: false}));
  } else {
    RESULTS = FLAT
      .map(d => ({d, s: Math.max(fuzzy(q, d.title), fuzzy(q, d.path))}))
      .filter(x => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 25)
      .map(x => ({...x.d, _hit: false}));
  }
  SEL = 0;
  list.innerHTML = RESULTS.map((r, i) => rowHtml(r, i)).join("") ||
    '<div class="empty" style="padding:30px">no matches</div>';
  wireRows();
  if (q.length >= 3){
    clearTimeout(searchT);
    searchT = setTimeout(async () => {
      const r = await fetch("/api/search?q=" + encodeURIComponent(q)).then(r => r.json());
      if ($("#q").value.trim() !== q) return;
      const hits = r.results.map(x => ({...x, _hit: true}));
      RESULTS = RESULTS.concat(hits);
      list.innerHTML = RESULTS.map((r, i) => rowHtml(r, i, q)).join("") ||
        '<div class="empty" style="padding:30px">no matches</div>';
      wireRows();
    }, 160);
  }
}

function rowHtml(r, i, q){
  const sel = i === SEL ? " sel" : "";
  if (r._hit){
    let sn = esc(r.snippet);
    if (q){
      const re = new RegExp("(" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      sn = sn.replace(re, "<mark>$1</mark>");
    }
    return `<div class="pr${sel}" data-i="${i}"><div class="t"><span class="k">↳</span>` +
      `<span>${esc(r.title)}</span></div><div class="sn">${sn}</div>` +
      `<div class="p">${esc(r.path)}:${r.line}</div></div>`;
  }
  return `<div class="pr${sel}" data-i="${i}"><div class="t">` +
    `<span class="k">${esc((r.label || "").slice(0, 5))}</span>` +
    `<span>${esc(r.title)}</span></div><div class="p">${esc(r.path)}</div></div>`;
}

function wireRows(){
  document.querySelectorAll(".pr").forEach(el => {
    el.onclick = () => choose(+el.dataset.i);
    el.onmouseenter = () => { SEL = +el.dataset.i; highlight(); };
  });
}
function highlight(){
  document.querySelectorAll(".pr").forEach(el =>
    el.classList.toggle("sel", +el.dataset.i === SEL));
  document.querySelector(".pr.sel")?.scrollIntoView({block:"nearest"});
}
function choose(i){
  const r = RESULTS[i];
  if (!r) return;
  const q = $("#q").value.trim();
  palette(false);
  if (r._hit && q) PENDING_HIT = q;
  const target = "#/" + r.path;
  // Setting an identical hash fires no hashchange — route by hand in that case.
  if (location.hash === target) route();
  else location.hash = target;
}

/* ── events ─────────────────────────────────────────────────────────────── */
$("#filter").oninput = renderNav;
$("#searchBtn").onclick = () => palette(true);
$("#zenBtn").onclick = toggleZen;
$("#navBtn").onclick = toggleNav;
$("#tocBtn").onclick = toggleToc;
$("#themeBtn").onclick = (e) => { e.stopPropagation(); themePop(); };
document.addEventListener("click", (e) => {
  if (!$("#tpop").classList.contains("on")) return;
  if (!$("#tpop").contains(e.target)) themePop(false);
});
$("#q").oninput = (e) => paletteRender(e.target.value.trim());
$("#scrim").onclick = (e) => { if (e.target === $("#scrim")) palette(false); };

document.addEventListener("keydown", (e) => {
  const palOpen = $("#scrim").classList.contains("on");
  const typing = /input|textarea/i.test(document.activeElement.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key === "k"){ e.preventDefault(); palette(!palOpen); return; }
  if (palOpen){
    if (e.key === "Escape") palette(false);
    else if (e.key === "ArrowDown"){
      e.preventDefault(); SEL = Math.min(SEL + 1, RESULTS.length - 1); highlight(); }
    else if (e.key === "ArrowUp"){ e.preventDefault(); SEL = Math.max(SEL - 1, 0); highlight(); }
    else if (e.key === "Enter"){ e.preventDefault(); choose(SEL); }
    return;
  }
  if ($("#tpop").classList.contains("on") && e.key === "Escape"){ themePop(false); return; }
  if (typing){ if (e.key === "Escape") document.activeElement.blur(); return; }
  if (e.key === "/"){ e.preventDefault(); palette(true); }
  else if (e.key === ","){ themePop(); }
  else if (e.key === "\\"){ toggleZen(); }
  else if (e.key === "s"){ toggleNav(); }
  else if (e.key === "t"){ toggleToc(); }
  else if (e.key === "Escape" && (HIDE_NAV || HIDE_TOC)){
    HIDE_NAV = HIDE_TOC = false; applyChrome();
  }
  else if (e.key === "[" || e.key === "]"){
    const i = FLAT.findIndex(d => d.path === CUR);
    const t = FLAT[i + (e.key === "]" ? 1 : -1)];
    if (t) location.hash = "#/" + t.path;
  }
});

window.addEventListener("hashchange", route);

/* live reload — repoll mtimes; re-render the open doc in place when it changes */
setInterval(async () => {
  try{
    const r = await fetch("/api/version").then(r => r.json());
    if (r.version !== VERSION){
      VERSION = r.version;
      await loadTree();
      if (CUR) await openDoc(CUR, null, true);
      toast("reloaded");
    }
  }catch(_){}
}, 2000);

(async () => { applyChrome(); renderThemes(); await loadTree(); route(); })();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
