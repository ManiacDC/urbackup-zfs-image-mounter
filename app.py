import json
import logging
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager

from truenas_api_client import APIKeyAuthMech, Client
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["LAST_RESTORE"] = None
STATE_FILE = "/data/restore-state.json"
logging.basicConfig(level=logging.INFO)


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


_zfs_prefix: Optional[List[str]] = None


def get_zfs_prefix() -> List[str]:
    global _zfs_prefix
    if _zfs_prefix is not None:
        return _zfs_prefix

    env_prefix = os.getenv("ZFS_PREFIX", "").strip()
    if env_prefix:
        import shlex
        _zfs_prefix = shlex.split(env_prefix)
        return _zfs_prefix

    try:
        if os.path.exists("/proc/1/cmdline"):
            cmdline = Path("/proc/1/cmdline").read_text(encoding="utf-8")
            if any(init_name in cmdline for init_name in ("systemd", "init", "runit", "openrc")):
                res = subprocess.run(
                    ["nsenter", "-t", "1", "-m", "true"],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if res.returncode == 0:
                    logging.info("Auto-detected host mount namespace via nsenter. Using nsenter prefix for ZFS commands.")
                    _zfs_prefix = ["nsenter", "-t", "1", "-m", "--"]
                    return _zfs_prefix
    except Exception:
        pass

    _zfs_prefix = []
    return _zfs_prefix


def run_command(command: List[str], check: bool = True) -> str:
    cmd = list(command)
    if cmd and cmd[0] in {"zfs", "zpool"}:
        prefix = get_zfs_prefix()
        if prefix:
            cmd = prefix + cmd
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {cmd[0]}") from exc

    if check and result.returncode != 0:
        raise RuntimeError(f"Command {' '.join(cmd)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def normalize_path(path: str) -> Path:
    return Path(os.path.normpath(Path(path).expanduser().absolute()))


def is_path_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def is_absolute_path(path: str) -> bool:
    return Path(path).is_absolute() or path.startswith("/") or path.startswith("\\")


def list_client_subdirs(backup_path: str) -> List[str]:
    backup_path = backup_path.strip()
    if not backup_path or not is_absolute_path(backup_path):
        return []

    root = Path(backup_path)
    if not root.exists():
        return []

    subdirs = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name == "clients" or name.lower().startswith("urbackup"):
            continue
        subdirs.append(name)
    return sorted(subdirs)


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


def list_snapshots_for_path(backup_path: str, client_subdir: str = "") -> List[str]:
    normalized_backup_path = backup_path.strip()
    datasets = discover_datasets_for_path(normalized_backup_path)
    snapshots: List[str] = []
    for dataset in datasets:
        output = run_command(["zfs", "list", "-H", "-r", "-t", "snapshot", "-o", "name", dataset], check=False)
        if output:
            snapshots.extend(line for line in output.splitlines() if line.strip())

    if client_subdir:
        client_slug = client_subdir.strip("/")
        snapshots = [
            snapshot
            for snapshot in snapshots
            if f"/{client_slug}" in snapshot or snapshot.startswith(f"{client_slug}@") or snapshot.startswith(f"{client_slug}/")
        ]

    return sorted(set(snapshots))


def find_raw_file(root_path: str) -> Optional[str]:
    root = Path(root_path)
    for candidate in sorted(root.rglob("*.raw")):
        if candidate.is_file():
            return str(candidate)
    return None


def find_raw_file_with_retry(root_path: str, retries: int = 3, delay: float = 1.0) -> Optional[str]:
    for attempt in range(retries):
        raw_path = find_raw_file(root_path)
        if raw_path:
            return raw_path
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def derive_target_dataset(restore_path: str, snapshot_name: str = "", client_subdir: str = "") -> str:
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
                base_dataset = dataset
            else:
                base_dataset = f"{dataset}/{suffix}".replace("//", "/")
            break
    else:
        base_dataset = str(normalized_path).replace("/", "_", 1)

    parts: List[str] = []
    if client_subdir:
        parts.append(client_subdir.strip("/"))

    if snapshot_name:
        snapshot_path = snapshot_name.strip("/")
        if "@" in snapshot_path:
            snapshot_path = snapshot_path.split("@", 1)[0]
        if snapshot_path:
            path_parts = [part for part in snapshot_path.split("/") if part]
            if path_parts:
                last_part = path_parts[-1]
                if last_part and last_part != client_subdir.strip("/"):
                    parts.append(last_part)

    if not parts:
        return base_dataset

    return "/".join([part for part in [base_dataset, *parts] if part]).replace("//", "/")


def ensure_dataset(dataset_name: str, recreate: bool = False) -> str:
    try:
        run_command(["zfs", "list", "-H", "-o", "name", dataset_name], check=True)
        if recreate:
            return dataset_name
        return dataset_name
    except RuntimeError:
        run_command(["zfs", "create", "-p", dataset_name], check=True)
    return dataset_name


def ensure_parent_dataset(dataset_name: str) -> str:
    parent_name = dataset_name.rsplit("/", 1)[0]
    if not parent_name or parent_name == dataset_name:
        return dataset_name
    return ensure_dataset(parent_name, recreate=False)


def derive_target_mountpoint(restore_path: str, snapshot_name: str = "", client_subdir: str = "") -> str:
    restore_path = restore_path.strip()
    if not restore_path:
        raise ValueError("Restore path is required")

    if not is_absolute_path(restore_path):
        normalized_path = Path(restore_path)
    else:
        normalized_path = normalize_path(restore_path)

    parts: List[str] = []
    if client_subdir:
        parts.append(client_subdir.strip("/"))

    if snapshot_name:
        snapshot_path = snapshot_name.strip("/")
        if "@" in snapshot_path:
            snapshot_path = snapshot_path.split("@", 1)[0]
        if snapshot_path:
            path_parts = [part for part in snapshot_path.split("/") if part]
            if path_parts:
                last_part = path_parts[-1]
                if last_part and last_part != client_subdir.strip("/"):
                    parts.append(last_part)

    return os.path.join(str(normalized_path), *parts).replace("\\", "/")


def get_pool_altroot(dataset_name: str) -> str:
    pool_name = dataset_name.split("/")[0]
    try:
        altroot = run_command(["zpool", "get", "-H", "-o", "value", "altroot", pool_name], check=True)
        return altroot.strip()
    except Exception:
        return "-"


def clone_snapshot(snapshot_name: str, restore_path: str, client_subdir: str = "") -> Dict[str, str]:
    restore_dataset = derive_target_dataset(restore_path, snapshot_name=snapshot_name, client_subdir=client_subdir)
    target_mountpoint = derive_target_mountpoint(restore_path, snapshot_name=snapshot_name, client_subdir=client_subdir)

    altroot = get_pool_altroot(restore_dataset)
    zfs_mountpoint_prop = target_mountpoint
    if altroot != "-" and altroot != "":
        altroot_prefix = altroot.rstrip("/") + "/"
        if target_mountpoint.startswith(altroot_prefix):
            zfs_mountpoint_prop = "/" + target_mountpoint[len(altroot_prefix):]

    ensure_parent_dataset(restore_dataset)
    run_command(["zfs", "clone", "-o", f"mountpoint={zfs_mountpoint_prop}", snapshot_name, restore_dataset], check=True)
    try:
        run_command(["zfs", "mount", restore_dataset], check=True)
    except RuntimeError as exc:
        if "already mounted" not in str(exc).lower():
            raise
    mountpoint = run_command(["zfs", "get", "-H", "-o", "value", "mountpoint", restore_dataset], check=True)
    resolved_mountpoint = mountpoint
    if altroot != "-" and altroot != "":
        altroot_prefix = altroot.rstrip("/")
        if not mountpoint.startswith(altroot_prefix + "/"):
            resolved_mountpoint = altroot_prefix + mountpoint

    # If running with nsenter in host PID namespace, the host's mount namespace
    # is accessible via the /proc/1/root symlink inside a privileged container.
    # This allows us to find the file even if bind mount propagation is not working.
    search_path = resolved_mountpoint
    prefix = get_zfs_prefix()
    if prefix and any("nsenter" in p for p in prefix):
        host_root_path = os.path.join("/proc/1/root", resolved_mountpoint.lstrip("/"))
        if os.path.exists(host_root_path):
            search_path = host_root_path

    raw_path = find_raw_file_with_retry(search_path)
    resolved_raw_path = ""
    if raw_path:
        normalized_raw = raw_path.replace("\\", "/")
        if normalized_raw.startswith("/proc/1/root/"):
            resolved_raw_path = "/" + normalized_raw[len("/proc/1/root/"):].lstrip("/")
        else:
            resolved_raw_path = raw_path

    return {"dataset": restore_dataset, "mountpoint": resolved_mountpoint, "raw_path": resolved_raw_path}




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



@contextmanager
def truenas_client():
    verify_ssl = os.getenv("TRUENAS_VERIFY_SSL", "false").lower() in {"1", "true", "yes", "on"}
    api_key = os.getenv("TRUENAS_API_KEY", "")
    if not api_key:
        raise RuntimeError("TRUENAS_API_KEY is not configured")

    host = get_truenas_host().rstrip("/")
    if host.startswith("ws://") or host.startswith("wss://"):
        uri = host
    else:
        if host.startswith("https://"):
            uri = "wss://" + host[8:]
        elif host.startswith("http://"):
            uri = "ws://" + host[7:]
        else:
            scheme = "wss" if verify_ssl else "ws"
            uri = f"{scheme}://{host}"

        if "/api/current" not in uri and "/websocket" not in uri:
            uri = uri + "/api/current"

    username = os.getenv("TRUENAS_USERNAME", "truenas_admin")
    auth_mechanism_env = os.getenv("TRUENAS_AUTH_MECHANISM", "SCRAM").upper()
    auth_mechanism = APIKeyAuthMech.PLAIN if auth_mechanism_env == "PLAIN" else APIKeyAuthMech.SCRAM

    with Client(uri=uri, verify_ssl=verify_ssl) as c:
        c.login_with_api_key(username, api_key, auth_mechanism=auth_mechanism)
        yield c


def create_truenas_extent(raw_path: str, blocksize: int = 512) -> Dict[str, str]:
    extent_name = f"urbackup-{uuid.uuid4().hex[:8]}"
    try:
        blocksize = int(blocksize)
    except (ValueError, TypeError):
        blocksize = 512
    if blocksize not in {512, 4096}:
        blocksize = 512
    extent_payload = {
        "name": extent_name,
        "type": "FILE",
        "path": raw_path,
        "filesize": 0,
        "blocksize": blocksize,
        "pblocksize": False,
    }
    target_name = os.getenv("TRUENAS_TARGET_NAME", "urbackup-restore-target")

    with truenas_client() as c:
        extent = c.call("iscsi.extent.create", extent_payload)

        # Query existing targets to see if the target already exists
        targets = c.call("iscsi.target.query")
        target = next((t for t in targets if t.get("name") == target_name), None) if isinstance(targets, list) else None

        if not target:
            # Resolve portal and initiator group IDs to construct target groups
            portals = c.call("iscsi.portal.query")
            portal_id = portals[0].get("id") if portals and isinstance(portals, list) else None

            initiators = c.call("iscsi.initiator.query")
            initiator_id = None
            if initiators and isinstance(initiators, list):
                for init in initiators:
                    init_list = init.get("initiators", [])
                    # An initiator group allowing all will have empty initiators list or "*" or "ALL"
                    if not init_list or "ALL" in init_list or "*" in init_list or "" in init_list:
                        initiator_id = init.get("id")
                        break
                if initiator_id is None and initiators:
                    initiator_id = initiators[0].get("id")

            target_payload = {
                "name": target_name,
                "alias": target_name,
                "mode": "ISCSI",
                "groups": []
            }
            if portal_id is not None and initiator_id is not None:
                target_payload["groups"].append({
                    "portal": portal_id,
                    "initiator": initiator_id,
                })
            target = c.call("iscsi.target.create", target_payload)

        c.call(
            "iscsi.targetextent.create",
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
            with truenas_client() as c:
                c.call("iscsi.extent.delete", int(state["extent_id"]))
        except Exception as exc:
            result["extent"] = str(exc)
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


@app.route("/api/clients", methods=["GET"])
def api_clients():
    backup_path = request.args.get("backup_path", os.getenv("BACKUPS_PATH", ""))
    return jsonify({"clients": list_client_subdirs(backup_path), "backup_path": backup_path})


@app.route("/api/snapshots", methods=["GET"])
def api_snapshots():
    backup_path = request.args.get("backup_path", os.getenv("BACKUPS_PATH", ""))
    client_subdir = request.args.get("client_subdir", "")
    try:
        snapshots = list_snapshots_for_path(backup_path, client_subdir)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"snapshots": snapshots, "backup_path": backup_path, "client_subdir": client_subdir})


@app.route("/api/restore", methods=["POST"])
def api_restore():
    payload = request.get_json(silent=True) or {}
    backup_path = payload.get("backup_path") or os.getenv("BACKUPS_PATH", "")
    restore_path = payload.get("restore_path") or os.getenv("RESTORE_PATH", "")
    snapshot_name = payload.get("snapshot_name", "")
    client_subdir = payload.get("client_subdir", "")
    blocksize = payload.get("blocksize", 512)

    if not backup_path or not restore_path or not snapshot_name:
        return jsonify({"error": "backup_path, restore_path and snapshot_name are required"}), 400

    try:
        clone_info = clone_snapshot(snapshot_name, restore_path, client_subdir=client_subdir)
        raw_path = clone_info.get("raw_path") or find_raw_file_with_retry(clone_info["mountpoint"])
        if not raw_path:
            raise RuntimeError(f"No .raw image found under {clone_info['mountpoint']}")
        state = {
            "snapshot": snapshot_name,
            "dataset": clone_info["dataset"],
            "mountpoint": clone_info["mountpoint"],
            "raw_path": raw_path,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        app.config["LAST_RESTORE"] = state
        save_restore_state(state)
        truenas_info = create_truenas_extent(raw_path, blocksize=blocksize)
        state.update(truenas_info)
        app.config["LAST_RESTORE"] = state
        save_restore_state(state)
        return jsonify({"status": "ok", "clone": state})
    except Exception as exc:
        app.logger.exception("Restore request failed")
        diagnostics = {
            "error": str(exc),
            "backup_path": backup_path,
            "restore_path": restore_path,
            "snapshot_name": snapshot_name,
            "client_subdir": client_subdir,
            "blocksize": blocksize,
        }
        if "clone_info" in locals():
            diagnostics["mountpoint"] = clone_info.get("mountpoint", "")
            diagnostics["dataset"] = clone_info.get("dataset", "")
            diagnostics["raw_path"] = clone_info.get("raw_path", "")
        if "raw_path" in locals():
            diagnostics["resolved_raw_path"] = raw_path
        if "state" in locals() and state:
            app.config["LAST_RESTORE"] = state
            save_restore_state(state)
        return jsonify({"error": str(exc), "diagnostics": diagnostics}), 500


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
