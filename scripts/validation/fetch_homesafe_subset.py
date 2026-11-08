"""Fetch selected public HomeSafe images via bounded ZIP byte ranges.

Does not download the 34 GB archive. Dataset labels are exploratory references,
not proof that edited scenes have correct physical ground truth.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.request
import zipfile


class RemoteZipFile(io.RawIOBase):
    def __init__(self, url, length, tail=None):
        self.url, self.length, self.position = url, length, 0
        self.cache = [] if tail is None else [(length - len(tail), tail)]
        self.transferred = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else self.position + offset if whence == 1 else self.length + offset
        if not 0 <= self.position <= self.length:
            raise ValueError("Invalid seek")
        return self.position

    def read(self, size=-1):
        start = self.position
        size = self.length - start if size < 0 else min(size, self.length - start)
        if not size:
            return b""
        if size > 16 * 1024 * 1024:
            raise ValueError("Refusing an unbounded archive read")
        for cached_start, data in self.cache:
            if cached_start <= start and start + size <= cached_start + len(data):
                self.position += size
                return data[start - cached_start:start - cached_start + size]
        # Include nearby ZIP headers/data in one small request.
        end = min(self.length - 1, start + max(size, 65536) - 1)
        last_error = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(self.url, headers={
                    "Range": f"bytes={start}-{end}", "User-Agent": "K1-home-risk-evaluation/1.0"})
                with urllib.request.urlopen(request, timeout=40) as response:
                    expected = f"bytes {start}-{end}/{self.length}"
                    if response.status != 206 or response.headers.get("Content-Range") != expected:
                        raise ValueError("Server did not honor the exact byte range")
                    data = response.read(end - start + 2)
                    if len(data) != end - start + 1:
                        raise ValueError("Incomplete range response")
                self.transferred += len(data)
                self.cache.append((start, data))
                self.position += size
                return data[:size]
            except Exception as exc:
                last_error = exc
                time.sleep(attempt + 1)
        raise last_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("results/k1_validation_20261103"))
    parser.add_argument("--catalog-only", action="store_true")
    args = parser.parse_args()
    root = args.root
    url = "https://huggingface.co/datasets/Ursulalala/HomeSafe/resolve/main/homesafe_edit_image.zip"
    remote = RemoteZipFile(url, 33985582312, (root / "homesafe_zip_tail.bin").read_bytes())
    with zipfile.ZipFile(remote) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        (root / "homesafe_zip_catalog.json").write_text(json.dumps([
            {"name": x.filename, "compressed": x.compress_size, "bytes": x.file_size}
            for x in entries], ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"entries": len(names), "examples": names[:8], "transferred": remote.transferred}), flush=True)
        if args.catalog_only:
            return
        rows = json.loads((root / "homesafe_target_metadata.json").read_text(encoding="utf-8"))
        groups = {}
        for row in rows:
            principle = row["safety_principle"].lower()
            kind = "stack" if "unsecured stacking" in principle else "trip" if "tripping hazard" in principle else "route"
            key = (kind, row.get("source_image_path"), row.get("action"))
            groups.setdefault(key, {})[bool(row["is_safe"])] = row
        chosen = []
        counts = {"stack": 0, "trip": 0, "route": 0}
        for key, pair in groups.items():
            if set(pair) != {True, False} or counts[key[0]] >= {"stack": 4, "trip": 2, "route": 2}[key[0]]:
                continue
            counts[key[0]] += 1
            for safe in (True, False):
                row = dict(pair[safe])
                suffix = row["edit_image_path"].split("/edit_image/", 1)[-1]
                matching = [name for name in names if name.endswith("/" + suffix)]
                if len(matching) != 1:
                    raise ValueError("Ambiguous archive member: " + suffix)
                ident = f"{key[0]}_{counts[key[0]]:02d}_{'safe' if safe else 'unsafe'}"
                out = root / "domain_images" / (ident + ".png")
                out.parent.mkdir(exist_ok=True)
                if not out.exists():
                    out.write_bytes(archive.read(matching[0]))
                row.update(eval_id=ident, local_image=str(out), zip_member=matching[0],
                           sha256=hashlib.sha256(out.read_bytes()).hexdigest(),
                           label_scope="dataset conditional-action label, not physical truth")
                chosen.append(row)
                (root / "domain_reference.json").write_text(json.dumps(chosen,ensure_ascii=False,indent=2),encoding="utf-8")
                print(json.dumps({"downloaded":ident,"bytes":out.stat().st_size,"transferred":remote.transferred}),flush=True)
        print(json.dumps({"pairs":counts,"images":len(chosen),"archive_bytes_transferred":remote.transferred}),flush=True)


if __name__ == "__main__":
    main()
