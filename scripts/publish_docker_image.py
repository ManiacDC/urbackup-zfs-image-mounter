#!/usr/bin/env python3
import os
import subprocess
import sys

REPO = "maniacdc/urbackup-zfs-image-mounter"


def get_tag_from_requirements() -> str:
    req_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "requirements.txt")
    if not os.path.exists(req_path):
        return "latest"
    try:
        with open(req_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "truenas/api_client" in line and "@" in line:
                    tag = line.split("@")[-1].strip()
                    if tag.startswith("TS-"):
                        tag = tag[3:]
                    return tag
    except Exception:
        pass
    return "latest"


TAG = get_tag_from_requirements()
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
