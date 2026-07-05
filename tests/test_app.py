import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import cleanup_restore_state, clone_snapshot, create_truenas_extent, derive_target_dataset, discover_datasets_for_path, ensure_dataset, find_raw_file, get_truenas_host, list_client_subdirs, list_snapshots_for_path


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

    def test_list_client_subdirs_returns_immediate_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_dir = Path(tmp_dir) / "backup"
            (backup_dir / "client-a").mkdir(parents=True)
            (backup_dir / "client-b").mkdir(parents=True)
            (backup_dir / "client-c").mkdir(parents=True)
            (backup_dir / "client-c" / "nested").mkdir(parents=True)
            self.assertEqual(list_client_subdirs(str(backup_dir)), ["client-a", "client-b", "client-c"])

    def test_list_snapshots_for_path_filters_by_client_subdir(self):
        with patch.object(app_module, "discover_datasets_for_path", return_value=["tank/backup"]):
            with patch.object(app_module, "run_command", return_value="tank/backup@daily\ntank/backup/clients/monsterserver@daily"):
                snapshots = list_snapshots_for_path("/mnt/backup", "monsterserver")
                self.assertEqual(snapshots, ["tank/backup/clients/monsterserver@daily"])

    def test_derive_target_dataset_uses_client_and_snapshot_path(self):
        with patch.object(app_module, "get_zfs_filesystems", return_value=[("tank", "/mnt/tank")]):
            self.assertEqual(
                derive_target_dataset(
                    "/mnt/tank/restore",
                    snapshot_name="TimAndPatty/SystemBackup/UrBackupStorage/MONSTERSERVER/260703-1705_Image_C",
                    client_subdir="MONSTERSERVER",
                ),
                "tank/restore/MONSTERSERVER/260703-1705_Image_C",
            )

    def test_derive_target_mountpoint(self):
        expected = f"{app_module.normalize_path('/mnt/tank/restore').as_posix()}/MONSTERSERVER/260703-1705_Image_C"
        self.assertEqual(
            app_module.derive_target_mountpoint(
                "/mnt/tank/restore",
                snapshot_name="TimAndPatty/SystemBackup/UrBackupStorage/MONSTERSERVER/260703-1705_Image_C",
                client_subdir="MONSTERSERVER",
            ),
            expected,
        )


    def test_ensure_dataset_creates_when_missing_without_destroying(self):
        calls = []

        def fake_run_command(command, check=True):
            calls.append(command)
            if command[:2] == ["zfs", "list"]:
                raise RuntimeError("dataset missing")
            return ""

        with patch.object(app_module, "run_command", side_effect=fake_run_command):
            dataset_name = ensure_dataset("tank/restore", recreate=True)

        self.assertEqual(dataset_name, "tank/restore")
        self.assertEqual(calls[0][:2], ["zfs", "list"])
        self.assertEqual(calls[1][:2], ["zfs", "create"])
        self.assertEqual(len(calls), 2)

    def test_clone_snapshot_creates_parent_dataset_not_target(self):
        calls = []

        def fake_run_command(command, check=True):
            calls.append(command)
            if command[:2] == ["zfs", "list"]:
                raise RuntimeError("missing")
            return ""

        with patch.object(app_module, "derive_target_dataset", return_value="tank/restore/MONSTERSERVER/260703-1705_Image_C"), \
             patch.object(app_module, "get_pool_altroot", return_value="-"):
            with patch.object(app_module, "run_command", side_effect=fake_run_command):
                clone_snapshot("tank/source@snap", "/mnt/tank/restore", client_subdir="MONSTERSERVER")

        self.assertEqual(calls[0][:2], ["zfs", "list"])
        self.assertEqual(calls[1][:2], ["zfs", "create"])
        self.assertEqual(calls[1][-1], "tank/restore/MONSTERSERVER")
        self.assertEqual(calls[2][:2], ["zfs", "clone"])
        self.assertEqual(calls[2][-1], "tank/restore/MONSTERSERVER/260703-1705_Image_C")
        self.assertTrue(all(command[-1] != "tank/restore/MONSTERSERVER/260703-1705_Image_C" for command in calls if command[:2] == ["zfs", "create"]))

    def test_create_truenas_extent_uses_configured_blocksize(self):
        captured = []

        def fake_truenas_request(method, path, payload=None):
            captured.append((method, path, payload))
            if method.lower() == "get":
                if "target" in path:
                    return []
                if "portal" in path:
                    return [{"id": 1}]
                if "initiator" in path:
                    return [{"id": 1, "initiators": []}]
            return {"id": 1}

        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "image.raw"
            raw_path.write_bytes(b"abc")
            with patch.object(app_module, "truenas_request", side_effect=fake_truenas_request):
                create_truenas_extent(str(raw_path), blocksize="512")

        self.assertEqual(captured[0][2]["blocksize"], 512)
        self.assertFalse(captured[0][2]["pblocksize"])

    def test_api_restore_persists_state_when_extent_creation_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "restore-state.json"
            with patch.object(app_module, "STATE_FILE", str(state_path)):
                with patch.object(app_module, "clone_snapshot", return_value={"dataset": "tank/restore/MONSTERSERVER/260703-1705_Image_C", "mountpoint": "/mnt/restore"}):
                    with patch.object(app_module, "find_raw_file", return_value="/mnt/restore/image.raw"):
                        with patch.object(app_module, "create_truenas_extent", side_effect=RuntimeError("boom")):
                            response = app_module.app.test_client().post(
                                "/api/restore",
                                json={
                                    "backup_path": "/mnt/backup",
                                    "restore_path": "/mnt/restore",
                                    "snapshot_name": "tank/source@snap",
                                    "client_subdir": "MONSTERSERVER",
                                },
                            )

            self.assertEqual(response.status_code, 500)
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["dataset"], "tank/restore/MONSTERSERVER/260703-1705_Image_C")
            self.assertEqual(state["mountpoint"], "/mnt/restore")

    def test_clone_snapshot_returns_selected_raw_file_path(self):
        calls = []

        def fake_run_command(command, check=True):
            calls.append(command)
            if command[:2] == ["zfs", "get"]:
                return "/mnt/restore"
            return ""

        with patch.object(app_module, "derive_target_dataset", return_value="tank/restore/MONSTERSERVER/260703-1705_Image_C"), \
             patch.object(app_module, "get_pool_altroot", return_value="-"):
            with patch.object(app_module, "ensure_parent_dataset", return_value="tank/restore/MONSTERSERVER"):
                with patch.object(app_module, "find_raw_file", return_value="/mnt/restore/image.raw"):
                    with patch.object(app_module, "run_command", side_effect=fake_run_command):
                        clone_info = clone_snapshot("tank/source@snap", "/mnt/tank/restore", client_subdir="MONSTERSERVER")

        self.assertEqual(clone_info["raw_path"], "/mnt/restore/image.raw")
        self.assertIn(["zfs", "mount", "tank/restore/MONSTERSERVER/260703-1705_Image_C"], calls)

    def test_cleanup_destroy_dataset_from_state(self):
        calls = []

        def fake_run_command(command, check=True):
            calls.append(command)
            return ""

        with patch.object(app_module, "run_command", side_effect=fake_run_command):
            result = cleanup_restore_state({"dataset": "tank/restore/MONSTERSERVER/260703-1705_Image_C"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls[-1][:2], ["zfs", "destroy"])
        self.assertEqual(calls[-1][-1], "tank/restore/MONSTERSERVER/260703-1705_Image_C")

    def test_run_command_prepends_prefix_from_env(self):
        app_module._zfs_prefix = None
        with patch.dict(os.environ, {"ZFS_PREFIX": "nsenter -t 1 -m --"}):
            with patch.object(app_module.subprocess, "run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "output"
                app_module.run_command(["zfs", "list"])
                mock_run.assert_called_once()
                args, kwargs = mock_run.call_args
                self.assertEqual(args[0], ["nsenter", "-t", "1", "-m", "--", "zfs", "list"])

    def test_run_command_does_not_prepend_prefix_for_non_zfs_commands(self):
        app_module._zfs_prefix = None
        with patch.dict(os.environ, {"ZFS_PREFIX": "nsenter -t 1 -m --"}):
            with patch.object(app_module.subprocess, "run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "output"
                app_module.run_command(["echo", "hello"])
                mock_run.assert_called_once()
                args, kwargs = mock_run.call_args
                self.assertEqual(args[0], ["echo", "hello"])

    def test_clone_snapshot_resolves_mountpoint_using_altroot(self):
        calls = []

        def fake_run_command(command, check=True):
            calls.append(command)
            if "mountpoint" in command:
                return "/restore"
            return ""

        with patch.object(app_module, "derive_target_dataset", return_value="tank/restore/MONSTERSERVER/260703-1705_Image_C"), \
             patch.object(app_module, "get_pool_altroot", return_value="/mnt"):
            with patch.object(app_module, "ensure_parent_dataset", return_value="tank/restore/MONSTERSERVER"):
                with patch.object(app_module, "find_raw_file", return_value="/mnt/restore/image.raw"):
                    with patch.object(app_module, "run_command", side_effect=fake_run_command):
                        clone_info = clone_snapshot("tank/source@snap", "/mnt/tank/restore", client_subdir="MONSTERSERVER")

        self.assertEqual(clone_info["mountpoint"], "/mnt/restore")

    def test_clone_snapshot_resolves_mountpoint_under_proc_1_root_when_nsenter_enabled(self):
        calls = []

        def fake_run_command(command, check=True):
            calls.append(command)
            if "mountpoint" in command:
                return "/restore"
            return ""

        with patch.object(app_module, "get_zfs_prefix", return_value=["nsenter", "-t", "1", "-m", "--"]):
            with patch.object(app_module.os.path, "exists", side_effect=lambda p: p.replace("\\", "/").startswith("/proc/1/root/")):
                with patch.object(app_module, "find_raw_file_with_retry", return_value="/proc/1/root/mnt/restore/image.raw") as mock_find:
                    with patch.object(app_module, "derive_target_dataset", return_value="tank/restore/MONSTERSERVER/260703-1705_Image_C"), \
                         patch.object(app_module, "get_pool_altroot", return_value="/mnt"):
                        with patch.object(app_module, "ensure_parent_dataset", return_value="tank/restore/MONSTERSERVER"):
                            with patch.object(app_module, "run_command", side_effect=fake_run_command):
                                clone_info = clone_snapshot("tank/source@snap", "/mnt/tank/restore", client_subdir="MONSTERSERVER")

                    expected_search_path = os.path.join("/proc/1/root", "mnt/restore").replace("\\", "/")
                    # The mock find will be called with the path using system separators
                    mock_find.assert_called_once()
                    actual_call_arg = mock_find.call_args[0][0].replace("\\", "/")
                    self.assertEqual(actual_call_arg, expected_search_path)
                    self.assertEqual(clone_info["raw_path"], "/mnt/restore/image.raw")


if __name__ == "__main__":
    unittest.main()
