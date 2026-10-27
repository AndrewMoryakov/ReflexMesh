"""State roots passed directly by Pi/MCP expand the user's home without a shell."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reflexmesh.api.service import TaskService


class StateHome(unittest.TestCase):
    def test_pi_default_expands_before_private_directories_are_created(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "home"
            home.mkdir()
            with patch.dict(os.environ, {"HOME": str(home)}):
                service = TaskService("~/.local/state/reflexmesh")
            expected = home / ".local/state/reflexmesh"
            self.assertEqual(service.root, expected.resolve())
            self.assertTrue((expected / "attempts").is_dir())
            self.assertEqual(expected.stat().st_mode & 0o777, 0o700)
            self.assertEqual((expected / "attempts").stat().st_mode & 0o777, 0o700)

    def test_explicit_absolute_path_keeps_its_location(self):
        with tempfile.TemporaryDirectory() as root:
            expected = Path(root) / "custom"
            service = TaskService(expected)
            self.assertEqual(service.root, expected.resolve())

    def test_only_leading_home_not_an_embedded_tilde_is_expanded(self):
        with tempfile.TemporaryDirectory() as root:
            expected = Path(root) / "literal~directory" / "state"
            service = TaskService(expected)
            self.assertEqual(service.root, expected.resolve())


if __name__ == "__main__":
    unittest.main()
