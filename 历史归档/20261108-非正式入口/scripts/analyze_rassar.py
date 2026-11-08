import collections
import io
import os
import re
import zipfile

from PIL import Image

path = r"C:\Users\jim chen\Downloads\RASSAR Dataset.zip"
z = zipfile.ZipFile(path)
names = ["Door Handle", "Electric Socket", "Grab Bar", "Knife", "Medication", "Rug", "Scissors", "Smoke Alarm", "Switch"]

labels = [n for n in z.namelist() if "/filtered_labels/" in n and n.endswith(".txt") and "__MACOSX" not in n]
images = [n for n in z.namelist() if "/filtered_images/" in n and n.endswith(".jpg") and "__MACOSX" not in n]

by_prefix = collections.defaultdict(collections.Counter)
total = collections.Counter()
for n in labels:
    base = n.rsplit("/", 1)[-1][:-4]
    m = re.match(r"^(.+?)_Image_\d+$", base)
    grp = m.group(1) if m else "<hash>"
    ids = []
    for line in z.read(n).decode("utf-8", "replace").splitlines():
        t = line.split()
        if t:
            ids.append(int(t[0]))
    for i in ids:
        by_prefix[grp][i] += 1
        total[i] += 1

print("=== label id totals ===")
for i in sorted(total):
    print(f"  {i}: {names[i] if i < len(names) else '?'}  x{total[i]}")
print("\n=== ids per name group ===")
for grp in sorted(by_prefix):
    if grp.startswith("._"):
        continue
    print(f"  {grp:22s} {dict(by_prefix[grp])}")

print("\n=== boxes per label file ===")
cnt = collections.Counter()
for n in labels:
    k = len([l for l in z.read(n).decode("utf-8", "replace").splitlines() if l.strip()])
    cnt[k] += 1
print(dict(cnt))

# extract samples
os.makedirs(r"E:\AAA new door\电子协会的比赛\results\rassar_samples", exist_ok=True)
samples = []
for n in images:
    base = n.rsplit("/", 1)[-1]
    if base.startswith("grab bar_Image_") and "grab" not in samples:
        samples.append(n)
    if base.startswith("rug_Image_") and "rug" not in samples:
        samples.append(n)
    if "_Image_" not in base and len(samples) < 4:
        samples.append(n)
for n in samples[:4]:
    data = z.read(n)
    out = os.path.join(r"E:\AAA new door\电子协会的比赛\results\rassar_samples", n.rsplit("/", 1)[-1])
    open(out, "wb").write(data)
    with Image.open(io.BytesIO(data)) as im:
        print("sample", n, im.size)
