# UrBackup ZFS Snapshot Restore

This project provides a small Flask app that can:

- list ZFS snapshots for a backup path,
- clone a selected snapshot into a restore location,
- find the resulting .raw disk image,
- create a TrueNAS iSCSI file extent for it.

## Requirements

- Python 3.13
- ZFS tools available in the container
- TrueNAS host with a valid API key
- The container must run with elevated privileges on TrueNAS

## Environment variables

Copy [.env.example](.env.example) to .env and adjust the values.

- BACKUPS_PATH: the path to the backup tree to inspect
- RESTORE_PATH: the path where the cloned dataset should be created
- TRUENAS_HOST: optional TrueNAS base URL, for example https://truenas.example.com. If omitted, the app will try common Docker host names such as host.docker.internal and fall back to http://host.docker.internal.
- TRUENAS_API_KEY: the TrueNAS API token or key
- TRUENAS_VERIFY_SSL: set to false if you use a self-signed certificate
- TRUENAS_TARGET_NAME: optional target name to reuse or create

## TrueNAS API key permissions

This app talks to the TrueNAS REST API to create and remove iSCSI resources. The API key therefore needs permission to manage iSCSI objects, specifically:

- create and delete iSCSI extents,
- create and delete iSCSI targets, and
- create and delete the target-to-extent bindings.

In practice, create a dedicated API key with read/write access to the iSCSI area (or a full permission level for iSCSI, if your TrueNAS version exposes that option). The app does not need broader system, account, or shell permissions.

### Generate the key in TrueNAS

1. Open the TrueNAS web UI.
2. Go to Credentials -> API Keys (on some versions this may be Accounts -> API Keys).
3. Click Add or Create.
4. Give the key a descriptive name such as `urbackup-zfs-image-mounter`.
5. Grant it the minimum iSCSI permissions you can. If the UI offers a scope or ACL selection, choose read/write for iSCSI resources.
6. Save the key and copy it immediately; TrueNAS usually shows it only once.
7. Paste the copied value into `TRUENAS_API_KEY` in your environment or compose configuration.

If your TrueNAS build uses API key ACLs, the safest setup is a limited key scoped to iSCSI management only. Avoid using a full administrative key unless you are comfortable with that level of access.

## Docker and Docker Compose

A compose file is included for running the app in a privileged container with the required ZFS device and host mounts.

### Docker Compose

1. Create a dedicated ZFS dataset for the compose state volume in the TrueNAS web UI. For example, in Storage -> Pools, select your pool, create a child dataset named `applicationdata/urbackup-zfs-image-mounter/data` (or another name that matches your preferred layout), and leave the default settings unless you need something specific.

2. Review and adjust the paths and values in [docker-compose.yml](docker-compose.yml) to match your TrueNAS/host environment.
3. Start the app from the TrueNAS web UI Apps page.
4. Open the UI at http://<host>:31842.
5. Stop it when you are done from the same TrueNAS Apps page.

The compose configuration expects:

- the backup tree mounted at `/mnt/tank/urbackup` (read-only is recommended),
- the restore location mounted at `/mnt/tank/restore` (using `:rshared` propagation),
- a persistent state directory at `/mnt/tank/applicationdata/urbackup-zfs-image-mounter/data` so `/data/restore-state.json` inside the container persists across restarts,
- the ZFS device exposed as `/dev/zfs`,
- the container running in privileged mode (`privileged: true`), and
- the container sharing the host's PID namespace (`pid: host` or `--pid=host`). This allows the container to automatically run ZFS/zpool commands via `nsenter` in the host's mount namespace. This ensures ZFS mounts occur on the host, making them visible to both the host OS and TrueNAS middleware.

The relevant environment variables are set in [docker-compose.yml](docker-compose.yml):

- `PORT` (default `8000`)
- `BACKUPS_PATH`
- `RESTORE_PATH`
- `TRUENAS_API_KEY`
- `TRUENAS_VERIFY_SSL`
- `TRUENAS_TARGET_NAME`
- `ZFS_PREFIX`: optional command prefix for ZFS/zpool tools (e.g., `nsenter -t 1 -m --`). If omitted, the app will auto-detect if host namespace execution is possible.

### Manual Docker build/run

If you prefer to run the image manually instead of with compose, build it first:

```bash
docker build -t urbackup-zfs-image-mounter .
```

Then run:

```bash
docker run --rm -p 8000:8000 \
  --privileged \
  --pid=host \
  -e BACKUPS_PATH=/mnt/tank/urbackup \
  -e RESTORE_PATH=/mnt/tank/restore \
  -e TRUENAS_HOST=https://truenas.example.com \
  -e TRUENAS_API_KEY=your-key \
  urbackup-zfs-image-mounter
```

On TrueNAS, mount the backup path and the ZFS device as needed. The container should be started in privileged mode so the ZFS CLI can manage datasets.

## Quick Usage Instructions (Windows client)

Launch the UI, Select a client, then a snapshot, then hit "Restore snapshot". This will create an iscsi target/extent for the snapshot. You will need to manually enable the service.

Then go to your start menu, type "iscsi" and hit enter on "iSCSI Initiator". In the "Target" field, enter the server hostname and hit Quick Connect. Uncheck the box about favorite targets as you don't want to auto reconnect. (Second time around, just click "Connect" on the selected urbackup-restore-target)
This should then open up the drive as a new drive attached to your PC. You can copy whatever files off of it you need. Not sure if you can edit it - you shouldn't even if you can, but it shouldn't matter if you do.

When done, Hit "Disconnect" in the iSCSI Initiator, then back in the web UI, click "Clean up last restore". This will delete the iscsi extent and clean up the temporary snapshot clone.