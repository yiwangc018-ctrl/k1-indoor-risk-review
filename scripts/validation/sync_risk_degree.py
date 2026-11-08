"""Copy only one risk-degree trial's evidence, not model weights."""
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
    parser.add_argument("--run", type=Path)
    parser.add_argument("--host")
    args = parser.parse_args()
    root = args.run or Path(Path("results/risk_degree/latest_run.txt").read_text(encoding="utf-8"))
    info = json.loads((root / "local_run.json").read_text())
    conn = argparse.Namespace(host=args.host or info["host"], port=22, user=k1.DEFAULT_USER, key=k1.DEFAULT_KEY)
    client = k1.connect(conn)
    try:
        with client.open_sftp() as sftp:
            def copy(remote, local):
                attr = sftp.stat(remote)
                if stat.S_ISDIR(attr.st_mode):
                    local.mkdir(parents=True, exist_ok=True)
                    for entry in sftp.listdir_attr(remote):
                        copy(posixpath.join(remote, entry.filename), local / entry.filename)
                elif stat.S_ISREG(attr.st_mode):
                    if local.exists() and local.stat().st_size == attr.st_size and local.suffix != ".json":
                        return
                    local.parent.mkdir(parents=True, exist_ok=True)
                    temp = local.with_suffix(local.suffix + ".sync")
                    sftp.get(remote, str(temp))
                    temp.replace(local)
            for name in ("processed", "inference", "detector", "preprocess.json", "usable.json", "jobs.json",
                         "schedule.json", "summary.json", "progress.json", "console.log", "run_board_study.py",
                         "run_risk_degree_study.py", "recovery", "recovery_console.log", "recover_fourth.py"):
                try:
                    copy(info["remote_root"] + "/" + name, root / name)
                except FileNotFoundError:
                    pass
        for file in (root / "progress.json", root / "inference/progress.json"):
            if file.exists():
                print(file.name, file.read_text(encoding="utf-8"))
    finally:
        client.close()


if __name__ == "__main__":
    main()
