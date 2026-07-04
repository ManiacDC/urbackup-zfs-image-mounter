import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["LAST_RESTORE"] = None
STATE_FILE = "/data/restore-state.json"


def save_restore_state(state: Dict) -> None:
    state_path = Path(STATE_FILE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_restore_state() -> Optional[Dict]:
    state_path = Path(STATE_FILE)
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def clear_restore_state() -> None:
    state_path = Path(STATE_FILE)
    if state_path.exists():
        state_path.unlink()


def run_command(command: List[str], check: bool = True) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {command[0]}") from exc

    if check and result.returncode != 0:
        raise RuntimeError(f"Command {' '.join(command)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def normalize_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def is_path_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def is_absolute_path(path: str) -> bool:
    return Path(path).is_absolute() or path.startswith("/") or path.startswith("\\")


def get_zfs_filesystems() -> List[Tuple[str, str]]:
    output = run_command(["zfs", "list", "-H", "-o", "name,mountpoint", "-t", "filesystem"])
    entries: List[Tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            entries.append((parts[0], parts[1]))
    return entries


def discover_datasets_for_path(backup_path: str) -> List[str]:
    backup_path = backup_path.strip()
    if not backup_path:
        return []

    if is_absolute_path(backup_path):
        normalized_path = normalize_path(backup_path)
        candidates: List[Tuple[str, str]] = []
        for dataset, mountpoint in get_zfs_filesystems():
            if mountpoint:
                normalized_mountpoint = normalize_path(mountpoint)
                if normalized_path == normalized_mountpoint or is_path_within(normalized_path, normalized_mountpoint):
                    candidates.append((dataset, str(normalized_mountpoint)))
        if candidates:
            candidates.sort(key=lambda item: len(item[1]), reverse=True)
            return [dataset for dataset, _mountpoint in candidates]

    try:
        run_command(["zfs", "list", "-H", "-o", "name", backup_path], check=True)
        return [backup_path]
    except RuntimeError:
        return []


def list_snapshots_for_path(backup_path: str) -> List[str]:
    datasets = discover_datasets_for_path(backup_path)
    snapshots: List[str] = []
    for dataset in datasets:
        output = run_command(["zfs", "list", "-H", "-t", "snapshot", "-o", "name", dataset], check=False)
        if output:
            snapshots.extend(line for line in output.splitlines() if line.strip())
    return sorted(set(snapshots))


def find_raw_file(root_path: str) -> Optional[str]:
    root = Path(root_path)
    for candidate in sorted(root.rglob("*.raw")):
        if candidate.is_file():
            return str(candidate)
    return None


def derive_target_dataset(restore_path: str) -> str:
    restore_path = restore_path.strip()
    if not restore_path:
        raise ValueError("Restore path is required")

    if not is_absolute_path(restore_path):
        return restore_path

    normalized_path = normalize_path(restore_path)
    for dataset, mountpoint in get_zfs_filesystems():
        normalized_mountpoint = normalize_path(mountpoint)
        if mountpoint and (normalized_path == normalized_mountpoint or is_path_within(normalized_path, normalized_mountpoint)):
            suffix = os.path.relpath(str(normalized_path), str(normalized_mountpoint))
            if suffix == ".":
                return dataset
            return f"{dataset}/{suffix}".replace("//", "/")
    return str(normalized_path).replace("/", "_", 1)


def ensure_dataset(dataset_name: str) -> str:
    try:
        run_command(["zfs", "list", "-H", "-o", "name", dataset_name], check=True)
    except RuntimeError:
        run_command(["zfs", "create", "-p", dataset_name], check=True)
    return dataset_name


def clone_snapshot(snapshot_name: str, restore_path: str) -> Dict[str, str]:
    dataset_name = ensure_dataset(derive_target_dataset(restore_path))
    run_command(["zfs", "clone", snapshot_name, dataset_name], check=True)
    mountpoint = run_command(["zfs", "get", "-H", "-o", "value", "mountpoint", dataset_name], check=True)
    return {"dataset": dataset_name, "mountpoint": mountpoint}


def get_truenas_host() -> str:
    configured_host = os.getenv("TRUENAS_HOST", "").strip()
    if configured_host:
        return configured_host.rstrip("/")

    for hostname in ("host.docker.internal", "host-gateway", "gateway.docker.internal"):
        try:
            socket.gethostbyname(hostname)
            return f"http://{hostname}"
        except OSError:
            continue

    return "http://host.docker.internal"


def truenas_request(method: str, path: str, payload: Optional[Dict] = None) -> Dict:
    base_url = get_truenas_host().rstrip("/")
    api_key = os.getenv("TRUENAS_API_KEY", "")
    if not api_key:
        raise RuntimeError("TRUENAS_API_KEY is not configured")
    if not api_key:
        raise RuntimeError("TRUENAS_API_KEY is not configured")

    verify_ssl = os.getenv("TRUENAS_VERIFY_SSL", "false").lower() in {"1", "true", "yes", "on"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.request(
        method,
        f"{base_url}/api/v2.0{path}",
        headers=headers,
        json=payload,
        verify=verify_ssl,
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"TrueNAS API error {response.status_code}: {response.text}")
    if response.text:
        try:
            return response.json()
        except ValueError:
            return {"message": response.text}
    return {}


def create_truenas_extent(raw_path: str) -> Dict[str, str]:
    extent_name = f"urbackup-{uuid.uuid4().hex[:8]}"
    extent_payload = {
        "name": extent_name,
        "type": "FILE",
        "path": raw_path,
        "filesize": os.path.getsize(raw_path),
        "blocksize": 512,
        "pblocksize": 512,
    }
    extent = truenas_request("post", "/iscsi/extent/", extent_payload)
    target_name = os.getenv("TRUENAS_TARGET_NAME", "urbackup-restore-target")
    target = truenas_request(
        "post",
        "/iscsi/target/",
        {"name": target_name, "alias": target_name, "mode": "ISCSI", "groups": []},
    )
    truenas_request(
        "post",
        "/iscsi/targetextent/",
        {"target": target.get("id"), "extent": extent.get("id")},
    )
    return {
        "extent_id": str(extent.get("id", "")),
        "target_id": str(target.get("id", "")),
        "target_name": target_name,
        "extent_name": extent_name,
    }


def cleanup_restore_state(state: Dict) -> Dict[str, str]:
    result: Dict[str, str] = {"status": "ok"}
    if state.get("extent_id"):
        try:
            truenas_request("delete", f"/iscsi/extent/id/{state['extent_id']}")
        except RuntimeError as exc:
            result["extent"] = str(exc)
    if state.get("target_id"):
        try:
            truenas_request("delete", f"/iscsi/target/id/{state['target_id']}")
        except RuntimeError as exc:
            result["target"] = str(exc)
    if state.get("dataset"):
        try:
            run_command(["zfs", "destroy", "-f", state["dataset"]], check=True)
        except RuntimeError as exc:
            result["dataset"] = str(exc)
    return result


@app.route("/", methods=["GET"])
def index():
    backup_path = os.getenv("BACKUPS_PATH", "")
    restore_path = os.getenv("RESTORE_PATH", "")
    return render_template("index.html", backup_path=backup_path, restore_path=restore_path)


@app.route("/api/snapshots", methods=["GET"])
def api_snapshots():
    backup_path = request.args.get("backup_path", os.getenv("BACKUPS_PATH", ""))
    try:
        snapshots = list_snapshots_for_path(backup_path)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"snapshots": snapshots, "backup_path": backup_path})


@app.route("/api/restore", methods=["POST"])
def api_restore():
    payload = request.get_json(silent=True) or {}
    backup_path = payload.get("backup_path") or os.getenv("BACKUPS_PATH", "")
    restore_path = payload.get("restore_path") or os.getenv("RESTORE_PATH", "")
    snapshot_name = payload.get("snapshot_name", "")

    if not backup_path or not restore_path or not snapshot_name:
        return jsonify({"error": "backup_path, restore_path and snapshot_name are required"}), 400

    try:
        clone_info = clone_snapshot(snapshot_name, restore_path)
        raw_path = find_raw_file(clone_info["mountpoint"])
        if not raw_path:
            raise RuntimeError(f"No .raw image found under {clone_info['mountpoint']}")
        truenas_info = create_truenas_extent(raw_path)
        state = {
            "snapshot": snapshot_name,
            "dataset": clone_info["dataset"],
            "mountpoint": clone_info["mountpoint"],
            "raw_path": raw_path,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **truenas_info,
        }
        app.config["LAST_RESTORE"] = state
        save_restore_state(state)
        return jsonify({"status": "ok", "clone": state})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    state = app.config.get("LAST_RESTORE") or load_restore_state()
    if not state:
        return jsonify({"status": "noop", "message": "No restore to clean up"})
    result = cleanup_restore_state(state)
    app.config["LAST_RESTORE"] = None
    clear_restore_state()
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
