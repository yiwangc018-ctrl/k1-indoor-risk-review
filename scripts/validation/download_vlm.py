"""Download and checksum a vendor VLM into a new study directory (board)."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import urllib.request

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
name = sys.argv[2]
base = "https://archive.spacemit.com/spacemit-ai/model_zoo/vlm/"
archive = root / (name + ".tar.gz")
status = root / "download_status.json"
started = time.time()


def save(**values):
    status.write_text(json.dumps({"model": name, "started": started,
        "updated": time.time(), **values}, indent=2), encoding="utf-8")


try:
    expected_line = urllib.request.urlopen(base + archive.name + ".md5", timeout=30).read().decode().strip()
    expected = expected_line.split()[0].lower()
    (root / (archive.name + ".md5")).write_text(expected_line + "\n")
    save(phase="downloading", expected_md5=expected)
    result = subprocess.run(["curl", "--fail", "--location", "--retry", "4",
        "--connect-timeout", "20", "--max-time", "5400", "--continue-at", "-",
        "--output", str(archive), base + archive.name])
    if result.returncode:
        raise RuntimeError("curl exit " + str(result.returncode))
    save(phase="checksumming", bytes=archive.stat().st_size, expected_md5=expected)
    md5, sha = hashlib.md5(), hashlib.sha256()
    with archive.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            md5.update(chunk)
            sha.update(chunk)
    if md5.hexdigest() != expected:
        raise ValueError("MD5 mismatch: expected %s got %s" % (expected, md5.hexdigest()))
    save(phase="extracting", bytes=archive.stat().st_size, md5=md5.hexdigest(), sha256=sha.hexdigest())
    destination = root / "extracted"
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or member.issym() or member.islnk():
                raise ValueError("Unsafe archive member: " + member.name)
        tar.extractall(destination, filter="data")
    save(phase="ready", bytes=archive.stat().st_size, md5=md5.hexdigest(), sha256=sha.hexdigest(),
         files=[{"path": str(p), "bytes": p.stat().st_size} for p in destination.rglob("*") if p.is_file()])
except Exception as exc:
    save(phase="failed", error=type(exc).__name__ + ": " + str(exc))
    raise
