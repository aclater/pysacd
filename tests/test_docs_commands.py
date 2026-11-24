"""tools/docs_commands.py (#215): it finds every command block in the Markdown, reads the shared
docs-run markers (the one list of what runs), and its swaps still apply; tools/docs_fixtures.py
provides every disc a command that runs names."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import docs_commands as dc


def tree(readme: str, **docs: str) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "README.md").write_text(readme, encoding="utf-8")
    (root / "docs").mkdir()
    for name, text in docs.items():
        (root / "docs" / f"{name}.md").write_text(text, encoding="utf-8")
    return tmp


class TheDocs(unittest.TestCase):
    def test_every_command_block_is_marked(self):
        self.assertEqual(dc.check_marks(dc.blocks()), [])

    def test_every_swap_still_applies(self):
        self.assertEqual(dc.check_swaps(dc.blocks()), [])

    def test_the_quick_start_and_the_api_examples_run(self):
        found = {(b.file, b.lang) for b in dc.blocks()}
        self.assertIn(("README.md", "sh"), found)
        self.assertIn(("docs/api.md", "python"), found)
        self.assertIn(("docs/user-guide.md", "console"), found)
        runs = [c.text for b in dc.blocks() if b.status == "run" for c in b.commands]
        self.assertIn("pysacd-extract -2 -s -i album.iso", runs)

    def test_no_command_names_an_undocumented_disc(self):
        # every *.iso named by a command that runs (or runs here for a need) is one the fixture writes
        for b in dc.blocks():
            if b.status == "skip" and not dc.need_of(b):
                continue
            for c in b.commands:
                for name in re.findall(r"[\w./-]+\.iso\b", c.text):
                    if name.startswith("/"):    # a path inside a container (docs/security.md)
                        continue
                    self.assertIn(Path(name).name, dc.DISCS, f"{b.file}:{b.line}: {c.text}")

    def test_the_discs_group_makes_its_discs_from_the_checkout(self):
        # docs-run starts the discs group in a copy of the checkout (checkout=1) and runs
        # tools/docs_fixtures.py from it; no marker fetches this repository from the network
        # (docs/development.md, "Docs commands against a release")
        setups = []
        for path in dc.doc_files():
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"pysacd/archive/[0-9a-f]+\.tar\.gz", path)
            setups += re.findall(r"<!-- docs-run: group=discs checkout=1 setup=\"([^\"]*)\"", text)
        self.assertEqual(len(setups), 1, setups)
        self.assertIn("python3 -I tools/docs_fixtures.py --offline-config ~", setups[0])


class Parsing(unittest.TestCase):
    def test_continuations_comments_and_here_documents(self):
        cmds = dc._commands([
            "# a comment line",
            "pysacd toc album.iso   # trailing comment",
            "podman run --rm \\",
            "  pysacd pysacd-extract -2 -s -i /in.iso",
            "podman build -t x - <<'END'",
            "FROM base",
            "RUN pip install pysacd",
            "END",
            "echo 'a # in quotes'",
        ])
        self.assertEqual([c.text for c in cmds], [
            "pysacd toc album.iso",
            "podman run --rm pysacd pysacd-extract -2 -s -i /in.iso",
            "podman build -t x - <<'END'",
            "echo 'a # in quotes'",
        ])
        self.assertTrue(cmds[2].script.endswith("RUN pip install pysacd\nEND"))

    def test_console_blocks_run_only_the_prompt_lines(self):
        with tree("# x\n\n```console\n$ pysacd toc album.iso\n== album.iso\nDisc: x\n```\n") as tmp:
            [b] = dc.blocks(Path(tmp))
        self.assertEqual([c.text for c in b.commands], ["pysacd toc album.iso"])

    def test_indented_blocks_and_other_languages(self):
        text = "1. step\n\n   ```sh\n   pysacd toc album.iso\n   ```\n\n```toml\nformat = 'flac'\n```\n"
        with tree(text, api="```python\nprint(1)\n```\n") as tmp:
            got = dc.blocks(Path(tmp))
        self.assertEqual([(b.file, b.lang) for b in got], [("README.md", "sh"), ("docs/api.md", "python")])
        self.assertEqual(got[0].commands[0].text, "pysacd toc album.iso")
        self.assertEqual(got[1].code, "print(1)\n")

    def test_markers_decide_what_runs(self):
        text = ("<!-- docs-run: skip=\"needs a PS3 or a player\" -->\n```sh\npysacd toc 192.0.2.10:2002\n```\n\n"
                "<!-- docs-run: group=discs expect=\"^Disc:\" -->\n```sh\npysacd toc album.iso\n```\n\n"
                "```sh\nmake\n```\n")
        with tree(text) as tmp:
            got = dc.blocks(Path(tmp))
        self.assertEqual([(b.status, b.reason, b.expects) for b in got],
                         [("skip", "needs a PS3 or a player", []), ("run", "", ["^Disc:"]), ("unmarked", "", [])])
        self.assertEqual(dc.need_of(got[0]), dc.PS3)
        self.assertIsNone(dc.need_of(got[1]))
        self.assertEqual(dc.check_marks(got),
                         ['README.md:11: command block without a docs-run marker (run, or skip="reason")'])

    def test_a_marker_reaches_only_a_fence_close_below_it(self):
        with tree("<!-- docs-run: skip=\"x\" -->\n\none\n\ntwo\n```sh\nmake\n```\n") as tmp:
            [b] = dc.blocks(Path(tmp))
        self.assertEqual(b.status, "unmarked")

    def test_powershell_blocks_are_skipped_for_windows(self):
        ok = '<!-- docs-run: skip="Windows: the py launcher" -->\n```powershell\npy -m pip install pysacd\n```\n'
        with tree(ok) as tmp:
            self.assertEqual(dc.blocks(Path(tmp)), [])
        for marker in ("", '<!-- docs-run: -->\n', '<!-- docs-run: skip="needs a GPU" -->\n'):
            with self.subTest(marker), tree(f"{marker}```powershell\npy --version\n```\n") as tmp, \
                    self.assertRaises(dc.MarkerError):
                dc.blocks(Path(tmp))

    def test_a_bad_marker_is_an_error(self):
        for marker in ('skip=""', "colour=red", "plain"):
            with self.subTest(marker), tree(f"<!-- docs-run: {marker} -->\n```sh\nmake\n```\n") as tmp, \
                    self.assertRaises(dc.MarkerError):
                dc.blocks(Path(tmp))

    def test_swaps(self):
        s = dc.swap("pip install pysacd   # note\npysacd-extract -I -i 192.0.2.10:2002\nsacd_extract -i 127.0.0.1:2002",
                    "pysacd>=0.5", 4321)
        self.assertEqual(s, "pip install 'pysacd>=0.5'   # note\npysacd-extract -I -i 127.0.0.1:4321\n"
                            "sacd_extract -i 127.0.0.1:4321")
        self.assertEqual(dc.swap("pip install pysacd-other", "w.whl", 1), "pip install pysacd-other")
        with tree("```sh\npysacd toc album.iso\n```\n") as tmp:
            self.assertEqual(len(dc.check_swaps(dc.blocks(Path(tmp)))), 1 + len(dc.PS3_ADDRESSES))


if __name__ == "__main__":
    unittest.main()
