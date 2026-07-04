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
- TRUENAS_HOST: the TrueNAS base URL, for example https://truenas.example.com
- TRUENAS_API_KEY: the TrueNAS API token or key
- TRUENAS_VERIFY_SSL: set to false if you use a self-signed certificate
- TRUENAS_TARGET_NAME: optional target name to reuse or create

## Docker

Build:

```bash
docker build -t urbackup-zfs-image-mounter .
```

Run:

```bash
docker run --rm -p 8000:8000 \
  --privileged \
  -e BACKUPS_PATH=/mnt/tank/urbackup \
  -e RESTORE_PATH=/mnt/tank/restore \
  -e TRUENAS_HOST=https://truenas.example.com \
  -e TRUENAS_API_KEY=your-key \
  urbackup-zfs-image-mounter
```

On TrueNAS, mount the backup path and the ZFS device as needed. The container should be started in privileged mode so the ZFS CLI can manage datasets.
