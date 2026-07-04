import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import discover_datasets_for_path, find_raw_file, get_truenas_host


class AppTests(unittest.TestCase):
    def test_find_raw_file_returns_first_match(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            nested_dir = f"{tmp_dir}/nested"
            os.makedirs(nested_dir)
            first_file = f"{tmp_dir}/image.raw"
            second_file = f"{nested_dir}/image2.raw"
            open(first_file, "w", encoding="utf-8").close()
            open(second_file, "w", encoding="utf-8").close()

            self.assertEqual(Path(find_raw_file(tmp_dir)), Path(first_file))

    def test_discover_datasets_for_path_uses_mountpoint_prefix(self):
        with patch.object(app_module, "get_zfs_filesystems", return_value=[("tank/backup", "/mnt/tank/backup")]):
            datasets = discover_datasets_for_path("/mnt/tank/backup/clients/server")
            self.assertEqual(datasets, ["tank/backup"])

    def test_get_truenas_host_uses_host_docker_internal_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(app_module.socket, "gethostbyname", return_value="172.17.0.1"):
            self.assertEqual(get_truenas_host(), "http://host.docker.internal")

    def test_restore_state_is_persisted_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "restore-state.json"
            with patch.object(app_module, "STATE_FILE", str(state_path)):
                state = {"dataset": "tank/restore"}
                app_module.save_restore_state(state)
                self.assertTrue(state_path.exists())
                self.assertEqual(app_module.load_restore_state(), state)
                app_module.clear_restore_state()
                self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
