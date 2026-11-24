"""docs/README.md and the documents' navigation headers match their headings."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DocsIndex(unittest.TestCase):
    def test_up_to_date(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "docs_index.py"), "--check"],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_up_to_date_in_a_non_utf8_locale(self):
        # Windows reads text in its ANSI code page by default: the pages are UTF-8 (#217)
        env = dict(os.environ, LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        r = subprocess.run([sys.executable, "-X", "utf8=0", str(ROOT / "tools" / "docs_index.py"), "--check"],
                           capture_output=True, text=True, check=False, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
