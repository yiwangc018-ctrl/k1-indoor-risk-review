"""Download round evidence and logs, excluding model/source archives."""
import argparse
from pathlib import Path
import stat

import k1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--out", type=Path, default=Path("results/room_review_20261104/board"))
    args = parser.parse_args()
    client = k1.connect(argparse.Namespace(host=args.host, user=k1.DEFAULT_USER, key=k1.DEFAULT_KEY, port=22))
    try:
        with client.open_sftp() as sftp:
            def copy(remote, local):
                if stat.S_ISDIR(sftp.stat(remote).st_mode):
                    local.mkdir(parents=True, exist_ok=True)
                    for entry in sftp.listdir_attr(remote):
                        if not entry.filename.startswith("."):
                            copy(remote + "/" + entry.filename, local / entry.filename)
                else:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    sftp.get(remote, str(local))
            for name in ("rounds", "logs"):
                copy("/home/biandu/rv/room_review/" + name, args.out / name)
        print(args.out.resolve())
    finally:
        client.close()


if __name__ == "__main__":
    main()
