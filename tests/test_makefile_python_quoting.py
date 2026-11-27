"""#229: the Makefile quotes $(PYTHON), so an interpreter under a path with a space still runs."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class PythonQuoting(unittest.TestCase):
    def test_every_recipe_line_quotes_the_interpreter(self):
        with tempfile.TemporaryDirectory(prefix="with space ") as d:
            py = str(Path(d) / "py 3")
            r = subprocess.run(["make", "-n", "-B", f"PYTHON={py}"], cwd=ROOT, capture_output=True, text=True,
                               check=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = [ln for ln in r.stdout.splitlines() if "tools/" in ln and ".py" in ln and "py 3" in ln]
            self.assertTrue(lines, r.stdout)
            for ln in lines:
                self.assertIn(f'"{py}" tools/', ln)
                self.assertNotIn(f" {py} tools/", ln.replace(f'"{py}"', ""))


if __name__ == "__main__":
    unittest.main()
