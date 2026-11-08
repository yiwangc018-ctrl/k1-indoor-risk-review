"""Copy only study evidence (not model archives) from the connected K1."""
import argparse
import json
from pathlib import Path
import posixpath
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
import k1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.137.240")
    parser.add_argument("--port", default=22, type=int)
    parser.add_argument("--user", default=k1.DEFAULT_USER)
    parser.add_argument("--key", default=k1.DEFAULT_KEY)
    parser.add_argument("--local", type=Path, default=Path("results/k1_validation_20261103"))
    parser.add_argument("--remote", default="/home/biandu/rv/validation_20261103")
    args = parser.parse_args()
    client = k1.connect(args)
    updated = []
    try:
        with client.open_sftp() as sftp:
            def copy(remote, local):
                info = sftp.stat(remote)
                if stat.S_ISDIR(info.st_mode):
                    local.mkdir(parents=True, exist_ok=True)
                    for item in sftp.listdir_attr(remote):
                        copy(posixpath.join(remote, item.filename), local / item.filename)
                elif stat.S_ISREG(info.st_mode):
                    # Progress/metadata JSON may change without changing size.
                    if remote.endswith((".jsonl", ".log")) and local.exists() and local.stat().st_size == info.st_size:
                        return
                    local.parent.mkdir(parents=True, exist_ok=True)
                    tmp = local.with_suffix(local.suffix + ".sync")
                    sftp.get(remote, str(tmp))
                    tmp.replace(local)
                    updated.append(str(local))
            for name in ("presence", "domain", "format", "text", "batch", "batch_full", "batch_full_console.log", "presence_console.log",
                         "domain_console.log", "text_console.log", "batch_console.log",
                         "presence_transition.json", "transition_console.log",
                         "format_console.log",
                         "models_2b/download_status.json", "models_4b/download_status.json"):
                try:
                    copy(posixpath.join(args.remote, name), args.local / name)
                except FileNotFoundError:
                    continue
        print(json.dumps({"updated_files": len(updated)}, ensure_ascii=False))
        for name in ("presence/progress.json", "domain/progress.json", "format/progress.json", "text/progress.json", "batch/progress.json"):
            p = args.local / name
            if p.exists():
                print(name, p.read_text(encoding="utf-8"))
    finally:
        client.close()


if __name__ == "__main__":
    main()
