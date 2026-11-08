"""Upload one photo, run inference on K1, download the complete evidence folder.

python scripts/pc/detect_photo.py photo.jpg --host 192.168.1.127
Uses ~/.ssh/id_k1_agent; no inference runs on the PC.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import posixpath
import sys
import uuid

from k1 import connect, put, run, shq, DEFAULT_HOST, DEFAULT_USER, DEFAULT_KEY

ROOT = Path(__file__).resolve().parents[2]


def download_tree(sftp, remote, local):
    import stat
    local.mkdir(parents=True, exist_ok=True)
    for entry in sftp.listdir_attr(remote):
        source = posixpath.join(remote, entry.filename)
        target = local / entry.filename
        if stat.S_ISDIR(entry.st_mode):
            download_tree(sftp, source, target)
        elif stat.S_ISREG(entry.st_mode):
            sftp.get(source, str(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "app/photo_detect_config.json")
    parser.add_argument("--skip-vlm", action="store_true")
    args = parser.parse_args()
    if not args.image.is_file():
        parser.error(f"Photo not found: {args.image}")
    ident = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    local = args.out or ROOT / "results" / "photo_chain" / ident
    if local.exists():
        parser.error(f"Output already exists: {local}")
    client = connect(args)
    try:
        # Isolate each deployment to avoid modifying a running project checkout.
        base = f"/home/{args.user}/rv/photo_chain/{ident}"
        for path in ("app/photo_detect.py", "app/photo_pipeline.py",
                     "scripts/k1_detect_image.py"):
            put(client, str(ROOT / path), base + "/" + path)
        put(client, str(args.config), base + "/app/photo_detect_config.json")
        remote_image = base + "/input" + args.image.suffix.lower()
        put(client, str(args.image), remote_image)
        if not args.skip_vlm:
            put(client, str(ROOT / "scripts/board/ensure_photo_vlm.sh"), base + "/ensure_vlm.sh")
        command = ("OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 PYTHONPATH=/home/" + args.user + "/k1demo/pylib python3 -u " +
                   shq(base + "/app/photo_detect.py") + " " + shq(remote_image) +
                   " --out " + shq(base + "/output") + (" --skip-vlm" if args.skip_vlm else " --start-vlm"))
        status, _, _ = run(client, command)
        with client.open_sftp() as sftp:
            download_tree(sftp, base + "/output", local)
        (local / "connection.json").write_text(json.dumps({"host": args.host, "user": args.user,
            "remote_run": base, "exit_code": status}, indent=2), encoding="utf-8")
        print(f"Downloaded: {local.resolve()}")
        return status
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
