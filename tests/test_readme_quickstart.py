"""tools/readme_quickstart.py: the Quick start's sh blocks are the steps; a block another platform
runs (marked `docs-run: skip=`, the Windows PowerShell commands) is skipped with a note; any other
non-sh block still stops the run."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import readme_quickstart as qs

README = """# T

## Quick start

1. Install.

   ```sh
   pip install pysacd
   ```

   On Windows:

   {marker}
   ```powershell
   py -m pip install pysacd
   ```

2. Read it.

   ```sh
   pysacd toc album.iso
   ```

   *You should see* `Disc:` here.

## Next
"""
MARKER = '<!-- docs-run: skip="Windows: PowerShell" -->'


class SkipMarkedBlocks(unittest.TestCase):
    def steps(self, text):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return qs.steps(text), out.getvalue()

    def test_marked_powershell_block_is_skipped_with_a_note(self):
        found, note = self.steps(README.format(marker=MARKER))
        self.assertEqual([s.lines for s in found], [["pip install pysacd"], ["pysacd toc album.iso"]])
        self.assertEqual(found[1].expect, ["Disc:"])
        self.assertIn("skipping a ```powershell block", note)

    def test_unmarked_powershell_block_still_stops_the_run(self):
        with self.assertRaises(SystemExit) as stop:
            self.steps(README.format(marker=""))
        self.assertIn("```powershell block", str(stop.exception))

    def test_marker_must_be_the_line_before_the_block(self):
        with self.assertRaises(SystemExit):
            self.steps(README.format(marker=MARKER + "\n\n   Some prose in between."))

    def test_a_marker_without_skip_does_not_skip(self):
        with self.assertRaises(SystemExit):
            self.steps(README.format(marker="<!-- docs-run: packages=python3 -->"))

    def test_a_marked_sh_block_is_still_a_step(self):
        text = README.format(marker=MARKER).replace("   ```sh\n   pysacd toc", "   " + MARKER + "\n   ```sh\n   pysacd toc")
        found, _ = self.steps(text)
        self.assertEqual(len(found), 2)

    def test_the_repository_readme_parses(self):
        found, _ = self.steps((ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertTrue(found)
        self.assertTrue(all(ln for s in found for ln in s.lines[:1]))


if __name__ == "__main__":
    unittest.main()
