"""docs/README.md and the documents' navigation headers match their headings."""

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


if __name__ == "__main__":
    unittest.main()
