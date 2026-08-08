"""Tests for tome. Stdlib `unittest` only — `python3 -m unittest -v`.

Each test builds a throwaway repo on disk and points tome at it, because
almost everything interesting here (grouping, link resolution, the secrets
guard) is a function of a real directory layout.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import tome


def make_repo(files: dict[str, str]) -> Path:
    """Materialise `{relative path: contents}` in a temp dir and configure tome."""
    root = Path(tempfile.mkdtemp(prefix="tome-test-"))
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    tome.configure(tome.load_config(root))
    return root


def render(md: str, doc_rel: str = "README.md") -> str:
    return tome.Markdown(doc_rel).render(md)


class TestRootDiscovery(unittest.TestCase):
    def test_git_dir_wins_over_nested_manifest(self):
        root = Path(tempfile.mkdtemp(prefix="tome-test-"))
        (root / ".git").mkdir()
        pkg = root / "packages" / "web"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text("{}")
        self.assertEqual(tome.find_root(pkg), root.resolve())

    def test_falls_back_to_weak_marker(self):
        root = Path(tempfile.mkdtemp(prefix="tome-test-"))
        (root / "Cargo.toml").write_text("")
        self.assertEqual(tome.find_root(root), root.resolve())


class TestConfig(unittest.TestCase):
    def test_defaults_without_a_config_file(self):
        root = make_repo({"README.md": "# hi"})
        self.assertEqual(tome.CFG.name, root.name)
        self.assertEqual(tome.CFG.pinned, tome.DEFAULT_PINNED)

    def test_config_file_overrides(self):
        make_repo({
            "README.md": "# hi",
            ".tome.json": json.dumps({
                "title": "custom", "pinned": ["SPEC"], "skip": ["fixtures"], "editor": "zed",
            }),
        })
        self.assertEqual(tome.CFG.name, "custom")
        self.assertEqual(tome.CFG.pinned, ("SPEC",))
        self.assertIn("fixtures", tome.CFG.skip_dirs)
        self.assertIn("node_modules", tome.CFG.skip_dirs)  # `skip` adds, not replaces
        self.assertEqual(tome.CFG.editor_url, "zed://file/{path}")

    def test_broken_config_is_ignored_not_fatal(self):
        root = make_repo({"README.md": "# hi", ".tome.json": "{ not json"})
        self.assertEqual(tome.CFG.name, root.name)

    def test_root_is_always_resolved(self):
        """Windows 8.3 names and macOS's /tmp symlink break every path compare."""
        root = make_repo({"README.md": "# hi"})
        self.assertEqual(tome.ROOT, root.resolve())
        self.assertEqual(tome.ROOT, tome.ROOT.resolve())

    def test_custom_editor_template_passes_through(self):
        cfg = tome.Config(root=Path("/tmp"), editor="mine://x?f={path}")
        self.assertEqual(cfg.editor_url, "mine://x?f={path}")
        self.assertEqual(tome.Config(root=Path("/tmp"), editor="nope").editor_url, "")


class TestGrouping(unittest.TestCase):
    def test_three_shapes(self):
        make_repo({
            "README.md": "# root",
            "docs/guide.md": "# guide",
            "projects/10-api-gateway/SPEC.md": "# spec",
            "projects/02-cache/README.md": "# cache",
        })
        groups = {g.gid: g for g in tome.build_tree()}
        self.assertEqual(groups["root"].title, "repo")
        self.assertEqual(groups["docs"].prefix, "docs/")
        self.assertEqual(groups["projects/10-api-gateway"].num, "10")
        self.assertEqual(groups["projects/10-api-gateway"].title, "api gateway")

    def test_root_section_sorts_first_then_numbered(self):
        make_repo({
            "README.md": "# root",
            "tools/x.md": "# x",
            "projects/03-c/README.md": "#",
            "projects/01-a/README.md": "#",
        })
        order = [g.gid for g in tome.build_tree()]
        self.assertEqual(order[0], "root")
        self.assertLess(order.index("projects/01-a"), order.index("projects/03-c"))

    def test_skips_noise_directories(self):
        make_repo({"README.md": "# r", "node_modules/pkg/README.md": "# no"})
        paths = [d.rel for g in tome.build_tree() for d in g.docs]
        self.assertEqual(paths, ["README.md"])

    def test_pinned_order_drives_sorting(self):
        make_repo({
            "p/one/README.md": "# r", "p/one/SPEC.md": "# s",
            ".tome.json": json.dumps({"pinned": ["SPEC", "README"], "groupDirs": ["p"]}),
        })
        labels = [d.label for g in tome.build_tree() for d in g.docs]
        self.assertEqual(labels, ["SPEC", "README"])

    def test_status_block_becomes_a_state(self):
        make_repo({"projects/01-a/SPEC.md": "<!-- status:\nstate: blocked\n-->\n# a"})
        self.assertEqual(tome.build_tree()[0].state, "blocked")


class TestStartTarget(unittest.TestCase):
    def setUp(self):
        make_repo({
            "README.md": "# root",
            "docs/adr/0001-thing.md": "# adr",
            "projects/10-api-gateway/SPEC.md": "# spec",
        })

    def test_number_resolves_to_a_package(self):
        self.assertEqual(tome._resolve_start("10"), "projects/10-api-gateway/SPEC.md")

    def test_directory_resolves_to_its_primary_doc(self):
        self.assertEqual(tome._resolve_start("docs/adr"), "docs/adr/0001-thing.md")

    def test_exact_file(self):
        self.assertEqual(tome._resolve_start("README.md"), "README.md")

    def test_fuzzy_fragment(self):
        self.assertEqual(tome._resolve_start("gateway"), "projects/10-api-gateway/SPEC.md")

    def test_no_match_falls_back_to_home(self):
        self.assertEqual(tome._resolve_start("nothing-like-this"), "")


class TestSafePath(unittest.TestCase):
    def setUp(self):
        self.root = make_repo({
            "README.md": "# r",
            ".env": "SECRET=1",
            ".env.example": "SECRET=",
            "certs/server.pem": "-----BEGIN",
            "src/main.rs": "fn main() {}",
        })

    def test_serves_a_normal_file(self):
        self.assertIsNotNone(tome.safe_path("src/main.rs"))

    def test_rejects_traversal(self):
        self.assertIsNone(tome.safe_path("../../../etc/passwd"))

    def test_rejects_secrets_but_allows_examples(self):
        self.assertIsNone(tome.safe_path(".env"))
        self.assertIsNone(tome.safe_path("certs/server.pem"))
        self.assertIsNotNone(tome.safe_path(".env.example"))

    def test_secret_name_matching(self):
        for name in (".env", ".env.local", "id_rsa", "server.key", ".netrc", "secrets.json"):
            self.assertTrue(tome._is_secret(name), name)
        for name in (".env.sample", "README.md", "monkey.md", "keynote.md"):
            self.assertFalse(tome._is_secret(name), name)


class TestMarkdown(unittest.TestCase):
    def test_headings_get_slugs_and_toc(self):
        md = tome.Markdown("README.md")
        html = md.render("# Title\n\n## A Section\n")
        self.assertIn('id="a-section"', html)
        self.assertEqual(md.title, "Title")
        self.assertEqual(md.toc[0]["id"], "a-section")

    def test_duplicate_headings_get_unique_slugs(self):
        md = tome.Markdown("README.md")
        html = md.render("## Same\n\n## Same\n")
        self.assertIn('id="same"', html)
        self.assertIn('id="same-1"', html)

    def test_table_alignment(self):
        html = render("| a | b |\n|:--|--:|\n| 1 | 2 |\n")
        self.assertIn('<td class="a-left">1</td>', html)
        self.assertIn('<td class="a-right">2</td>', html)

    def test_task_list_states(self):
        html = render("- [x] done\n- [ ] open\n- [~] field\n")
        self.assertIn('class="box done"', html)
        self.assertIn('class="box open"', html)
        self.assertIn('class="box open-field"', html)

    def test_fenced_code_is_highlighted_and_escaped(self):
        html = render("```rust\nfn main() { let x = \"<hi>\"; }\n```")
        self.assertIn('class="c-kw"', html)
        self.assertIn("&lt;hi&gt;", html)
        self.assertNotIn("<hi>", html)

    def test_inline_code_is_not_reprocessed(self):
        html = render("Use `**not bold**` here")
        self.assertIn("<code>**not bold**</code>", html)

    def test_html_is_escaped_in_prose(self):
        html = render("a <script>alert(1)</script> b")
        self.assertNotIn("<script>", html)

    def test_alert_callouts(self):
        self.assertIn('class="note warning"', render("> [!WARNING]\n> careful\n"))

    def test_mermaid_is_flagged(self):
        md = tome.Markdown("README.md")
        md.render("```mermaid\ngraph TD;\n```")
        self.assertTrue(md.mermaid)

    def test_links_resolve_by_target_type(self):
        make_repo({"docs/a.md": "# a", "docs/b.md": "# b", "src/main.rs": "fn main() {}"})
        html = tome.Markdown("docs/a.md").render(
            "[doc](b.md) [src](../src/main.rs) [gone](nope.md) [web](https://x.dev)"
        )
        self.assertIn('class="l-doc" href="#/docs/b.md"', html)
        self.assertIn('class="l-src" href="#/src/main.rs"', html)
        self.assertIn('class="l-miss"', html)
        self.assertIn('class="l-ext"', html)

    def test_images_are_served_through_raw(self):
        make_repo({"docs/a.md": "# a", "docs/img/x.png": "fake"})
        html = tome.Markdown("docs/a.md").render("![alt](img/x.png)")
        self.assertIn('src="/raw?p=docs/img/x.png"', html)

    def test_nested_lists_do_not_hang(self):
        html = render("- one\n  - two\n    - three\n- four\n")
        self.assertEqual(html.count("<li"), 4)


class TestHighlight(unittest.TestCase):
    def test_known_language_tags_tokens(self):
        out = tome.highlight('let x = "s"; // c', "rust")
        self.assertIn('<span class="c-kw">let</span>', out)
        self.assertIn('class="c-str"', out)
        self.assertIn('class="c-cmt"', out)

    def test_unknown_language_is_escaped_untouched(self):
        self.assertEqual(tome.highlight("a < b", "brainfuck"), "a &lt; b")

    def test_aliases(self):
        self.assertIn("c-kw", tome.highlight("def f(): pass", "py"))


class TestSearch(unittest.TestCase):
    def test_finds_and_ranks(self):
        make_repo({
            "a.md": "# a\nneedle here\nneedle again\n",
            "b.md": "# b\nneedle once\n",
            "c.md": "# c\nnothing\n",
        })
        results = tome.search("needle")
        self.assertEqual(results[0]["path"], "a.md")
        self.assertEqual(results[0]["count"], 2)
        self.assertNotIn("c.md", [r["path"] for r in results])

    def test_short_queries_are_ignored(self):
        make_repo({"a.md": "# a"})
        self.assertEqual(tome.search("a"), [])


class TestServer(unittest.TestCase):
    """End-to-end: boot the real handler and talk HTTP to it."""

    @classmethod
    def setUpClass(cls):
        make_repo({
            "README.md": "# Home\n\n[src](src/main.rs)\n",
            "src/main.rs": "fn main() {}\n",
            ".env": "SECRET=1",
        })
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), tome.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read().decode()

    def test_index_carries_brand_and_settings(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("const TOME = {", body)
        self.assertIn("<title>", body)
        self.assertNotIn("{{TITLE}}", body)
        self.assertNotIn("{{BRAND}}", body)
        self.assertNotIn("{{SETTINGS}}", body)

    def test_tree_includes_prefix(self):
        _, body = self.get("/api/tree")
        data = json.loads(body)
        self.assertTrue(data["version"])
        self.assertIn("prefix", data["groups"][0])

    def test_doc_renders_markdown(self):
        _, body = self.get("/api/doc?p=README.md")
        self.assertIn("<h1", json.loads(body)["html"])

    def test_source_file_renders_highlighted(self):
        data = json.loads(self.get("/api/doc?p=src/main.rs")[1])
        self.assertTrue(data["source"])
        self.assertIn("c-kw", data["html"])

    def test_secret_is_not_served(self):
        self.assertIn("error", json.loads(self.get("/api/doc?p=.env")[1]))


class TestSay(unittest.TestCase):
    """Windows hands back a legacy code page on a redirected stdout, and the
    banner is full of emoji. Printing it must never be what kills the process."""

    @staticmethod
    def capture(msg: str, encoding: str, *, err: bool = False) -> str:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="")
        attr = "stderr" if err else "stdout"
        saved = getattr(sys, attr)
        setattr(sys, attr, stream)
        try:
            tome.say(msg, err=err)
        finally:
            setattr(sys, attr, saved)
        stream.flush()
        return raw.getvalue().decode(encoding)

    def test_utf8_stream_keeps_the_emoji(self):
        self.assertIn("📖", self.capture("📖 tome docs → http://x", "utf-8"))

    def test_legacy_codepage_degrades_instead_of_raising(self):
        out = self.capture("📖 tome docs → http://127.0.0.1:7979/", "cp1252")
        self.assertNotIn("📖", out)
        self.assertIn("tome docs", out)
        self.assertIn("http://127.0.0.1:7979/", out)

    def test_ascii_stream_survives_every_decorated_line(self):
        # every non-ASCII character tome prints, in one go
        out = self.capture("📖 ✅ 👋 a — b – c · d → e", "ascii")
        self.assertEqual(out.strip(), "a b c d e")

    def test_no_ghost_indent_where_a_dropped_emoji_used_to_be(self):
        self.assertEqual(self.capture("✅ wrote .tome.json", "ascii").strip(), "wrote .tome.json")

    def test_err_routes_to_stderr(self):
        self.assertIn("warning: x", self.capture("warning: x — y", "ascii", err=True))


if __name__ == "__main__":
    unittest.main()
