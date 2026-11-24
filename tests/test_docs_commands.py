"""tools/docs_commands.py (#215): it finds every command block in README.md and docs/, its skip list
follows the docs, and its swaps still apply."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    def test_every_skip_entry_matches_a_docs_command(self):
        self.assertEqual(dc.check_skips(dc.blocks()), [])

    def test_every_swap_still_applies(self):
        self.assertEqual(dc.check_swaps(dc.blocks()), [])

    def test_the_quick_start_and_the_api_examples_run(self):
        found = {(b.file, b.lang) for b in dc.blocks()}
        self.assertIn(("README.md", "sh"), found)
        self.assertIn(("docs/api.md", "python"), found)
        self.assertIn(("docs/user-guide.md", "console"), found)
        runs = [c.text for b in dc.blocks() for c in b.commands if not dc.skip_of(b, c)]
        self.assertIn("pysacd-extract -2 -s -i album.iso", runs)

    def test_no_command_names_an_undocumented_disc(self):
        # every *.iso a command that runs names is one the run provides
        import re
        for b in dc.blocks():
            for c in b.commands:
                if dc.skip_of(b, c):
                    continue
                for name in re.findall(r"[\w./-]+\.iso\b", c.text):
                    self.assertIn(Path(name).name, dc.DISCS, f"{b.file}:{b.line}: {c.text}")


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

    def test_a_stale_skip_entry_is_reported(self):
        # the docs changed a skipped command: the list must follow
        with tree("```sh\nmake\n```\n") as tmp, \
                mock.patch.dict(dc.SKIP, {("README.md", "make"): ("x", None), ("README.md", "gone"): ("y", None)},
                                clear=True), \
                mock.patch.dict(dc.SKIP_FILES, {}, clear=True):
            self.assertEqual(dc.check_skips(dc.blocks(Path(tmp))), ["README.md: 'gone'"])

    def test_a_skipped_file_skips_every_command_and_must_exist(self):
        with tree("```sh\nmake\npysacd toc album.iso\n```\n") as tmp, \
                mock.patch.dict(dc.SKIP, {}, clear=True), \
                mock.patch.dict(dc.SKIP_FILES, {"README.md": "x", "docs/gone.md": "y"}, clear=True):
            got = dc.blocks(Path(tmp))
            self.assertEqual([dc.skip_of(b, c) for b in got for c in b.commands], [("x", None), ("x", None)])
            self.assertEqual(dc.check_skips(got), ["docs/gone.md: (the whole file)"])

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
