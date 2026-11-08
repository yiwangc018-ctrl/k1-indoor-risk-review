"""Deploy only the independent room-review files; leave old board apps untouched."""
import argparse
from pathlib import Path
import posixpath

import k1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    remote = "/home/biandu/rv/room_review"
    client = k1.connect(argparse.Namespace(host=args.host, user=k1.DEFAULT_USER, key=k1.DEFAULT_KEY, port=22))
    try:
        with client.open_sftp() as sftp:
            def mkdir(path):
                try:
                    sftp.stat(path)
                except FileNotFoundError:
                    mkdir(posixpath.dirname(path))
                    sftp.mkdir(path)

            files = ["requirements-board.txt"]
            for directory in ("app", "configs", "scripts/board"):
                files += [p.relative_to(root).as_posix() for p in (root / directory).rglob("*")
                          if p.is_file() and "__pycache__" not in p.parts
                          and p.suffix in (".py", ".json", ".txt", ".sh", ".desktop")]
            for name in files:
                target = remote + "/" + name
                mkdir(posixpath.dirname(target))
                # Shell launchers must have LF newlines even on Windows checkouts.
                if name.endswith(".sh"):
                    with sftp.open(target, "w") as stream:
                        stream.write((root / name).read_text(encoding="utf-8").replace("\r\n", "\n"))
                    # qterminal -e 直接执行脚本；644 会 Permission denied，桌面图标点了没反应。
                    sftp.chmod(target, 0o755)
                else:
                    sftp.put(str(root / name), target)
            for audio in (root / "results/room_review_20261104/audio").glob("*.wav"):
                mkdir(remote + "/audio")
                sftp.put(str(audio), remote + "/audio/" + audio.name)
            # 云台包放在 /home/biandu/rv/gimbal，与交接包文档一致：cd /home/biandu/rv 后可用
            # `python3 -m gimbal ...` 直接调试。
            gimbal_remote = "/home/biandu/rv/gimbal"
            mkdir(gimbal_remote)
            for path in (root / "gimbal").iterdir():
                if path.is_file() and path.suffix in (".py", ".json", ".md"):
                    sftp.put(str(path), gimbal_remote + "/" + path.name)
        print("Deployed:", remote)
    finally:
        client.close()


if __name__ == "__main__":
    main()
