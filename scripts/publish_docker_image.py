#!/usr/bin/env python3
import os
import subprocess
import sys

REPO = "maniacdc/urbackup-zfs-image-mounter"
TAG = "latest"
IMAGE = f"{REPO}:{TAG}"


def run(command: list[str]) -> None:
    print(f"$ {' '.join(command)}")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    print(f"Building Docker image {IMAGE}...")
    run(["docker", "build", "-t", IMAGE, "."])

    print(f"Pushing {IMAGE} to Docker Hub...")
    run(["docker", "push", IMAGE])

    print("Done.")


if __name__ == "__main__":
    main()
