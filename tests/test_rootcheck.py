"""Root handling (#17): convert refuses without --allow-root; pysacd-extract only warns."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from pysacd import __main__ as cli
from pysacd import extract, rootcheck


@unittest.skipUnless(hasattr(os, "geteuid"), "POSIX only")
class Root(unittest.TestCase):
    def run_cli(self, main, argv):
        err = io.StringIO()
        with mock.patch.object(os, "geteuid", return_value=0), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = main(argv)
        return rc, err.getvalue()

    def test_convert_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            rc, err = self.run_cli(cli.main, ["convert", "-o", d, os.path.join(d, "missing.iso")])
        self.assertEqual(rc, 2)
        self.assertIn("refusing to run as root", err)

    def test_convert_allow_root_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as d:
            rc, err = self.run_cli(cli.main, ["convert", "--allow-root", "--no-musicbrainz", "-o", d,
                                              os.path.join(d, "missing.iso")])
        self.assertNotIn("refusing", err)
        self.assertIn("warning: running as root", err)
        self.assertNotEqual(rc, 0)          # got as far as the (missing) image

    def test_extract_only_warns(self):
        with tempfile.TemporaryDirectory() as d:
            rc, err = self.run_cli(extract.main, ["--no-cfg", "-P", "-i", os.path.join(d, "missing.iso")])
        self.assertIn("warning: running as root", err)
        self.assertNotIn("refusing", err)
        self.assertEqual(rc, 255)           # failed on the missing image, not on root

    def test_not_root_is_silent(self):
        err = io.StringIO()
        with mock.patch.object(os, "geteuid", return_value=1000), contextlib.redirect_stderr(err):
            self.assertFalse(rootcheck.refuse_root("x", False))
            rootcheck.warn_root("x")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
