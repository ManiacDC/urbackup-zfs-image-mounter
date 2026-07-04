# Project Intent

This repository contains a small Flask-based Python app for restoring UrBackup snapshots from a ZFS backup store and provisioning the restored `.raw` disk image as an iSCSI extent on TrueNAS.

## Primary intention

- List ZFS snapshots available under a configured backup path.
- Clone the selected snapshot into a configured restore location.
- Discover the restored `.raw` image inside the cloned dataset.
- Create a TrueNAS iSCSI file extent using the TrueNAS REST API.
- Offer cleanup that removes the TrueNAS iSCSI binding, extent, target, and the cloned ZFS dataset when the user is done.

## Relevant context

- The app is designed to run in a privileged Docker container on TrueNAS so it can operate ZFS from inside the container and also call the TrueNAS API.
- The implementation assumes the container has access to the backup path and that the ZFS CLI is available.
- Cleanup state is persisted to `/data/restore-state.json` so the last restore can be cleaned up even after a restart.
- The current repo uses Python 3.13 in the Docker container and has been validated with a local 3.12 conda environment for syntax and tests.

## Important files

- `app.py` — main Flask application and restore/cleanup logic
- `Dockerfile` — builds the container image for deployment
- `requirements.txt` — Python dependencies for the app
- `README.md` — usage and environment variable documentation
- `.env.example` — example environment variables, including `PORT`
- `tests/test_app.py` — simple unit tests for core path handling logic

## Notes

- The `PORT` variable controls the HTTP port the Flask UI listens on.
- The app stores restore metadata to `/data/restore-state.json`, which is used for reliable cleanup after restarts.
- This repo is intended as a tooling layer to ease the manual process of restoring ZFS snapshots and mapping them into TrueNAS iSCSI.
