import collections
import hashlib
import io
import json
import zipfile

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

path = r"C:\Users\jim chen\Downloads\RASSAR Dataset.zip"
z = zipfile.ZipFile(path)
images = [n for n in z.namelist() if "/filtered_images/" in n and n.endswith(".jpg") and "__MACOSX" not in n]


def dhash(im):
    im = im.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    v = list(im.tobytes())
    h = 0
    for y in range(8):
        for x in range(8):
            h = (h << 1) | int(v[y * 9 + x] > v[y * 9 + x + 1])
    return h


rows = []
for i, n in enumerate(images):
    data = z.read(n)
    with Image.open(io.BytesIO(data)) as im:
        h = dhash(im)
        w, ht = im.size
    rows.append({"name": n.rsplit("/", 1)[-1], "dhash": h, "sha": hashlib.sha256(data).hexdigest()[:16], "size": [w, ht]})
    if (i + 1) % 500 == 0:
        print("hashed", i + 1, flush=True)

exact = collections.Counter(r["sha"] for r in rows)
print("images:", len(rows), "unique sha:", len(exact), "exact dup groups:", sum(1 for v in exact.values() if v > 1))

# near-duplicate clustering: union-find over pairs sharing a 16-bit bucket, then verify distance <= 6
parent = list(range(len(rows)))


def find(a):
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[rb] = ra


buckets = collections.defaultdict(list)
for i, r in enumerate(rows):
    h = r["dhash"]
    buckets[h >> 48].append(i)
    buckets[(h >> 32) & 0xFFFF].append(i)

checked = 0
for idxs in buckets.values():
    for a in range(len(idxs)):
        for b in range(a + 1, len(idxs)):
            i, j = idxs[a], idxs[b]
            d = (rows[i]["dhash"] ^ rows[j]["dhash"]).bit_count()
            checked += 1
            if d <= 6:
                union(i, j)
print("pairs checked:", checked)

clusters = collections.Counter(find(i) for i in range(len(rows)))
sizes = collections.Counter(clusters.values())
print("near-dup clusters:", len(clusters), "size histogram:", dict(sorted(sizes.items())[:10]))
print("largest clusters:", clusters.most_common(5))

out = r"E:\AAA new door\电子协会的比赛\results\rassar_hash.json"
json.dump({"rows": rows, "clusters": [find(i) for i in range(len(rows))]}, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print("saved", out)
