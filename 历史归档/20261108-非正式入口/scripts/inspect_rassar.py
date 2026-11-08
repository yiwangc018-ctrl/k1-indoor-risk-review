import collections
import io
import re
import zipfile

from PIL import Image

path = r"C:\Users\jim chen\Downloads\RASSAR Dataset.zip"
z = zipfile.ZipFile(path)

classes = z.read("RASSAR Dataset/classes.txt").decode("utf-8", "replace")
print("=== classes.txt ===")
print(classes)

labels = [n for n in z.namelist() if "/filtered_labels/" in n and n.endswith(".txt")]
images = [n for n in z.namelist() if "/filtered_images/" in n and n.endswith(".jpg")]
print("labels:", len(labels), "images:", len(images))

# group by basename prefix: "class_Image_N" vs hex hash
prefix = collections.Counter()
for n in images:
    base = n.rsplit("/", 1)[-1][:-4]
    m = re.match(r"^(.+?)_Image_\d+$", base)
    prefix[m.group(1) if m else "<hash>"] += 1
print("\nimage name groups:", prefix.most_common())

print("\n=== sample labels ===")
for n in labels[:3]:
    print("---", n)
    print(z.read(n).decode("utf-8", "replace")[:400])

# pick one named and one hash sample, print size + label
def show(img_name, label_candidates):
    with z.open(img_name) as f:
        im = Image.open(io.BytesIO(f.read()))
        print(f"{img_name}  size={im.size} mode={im.mode}")
    for ln in label_candidates:
        if ln in z.namelist():
            print("  label:", ln)
            print("  ", z.read(ln).decode("utf-8", "replace")[:300].replace("\n", "\n   "))
            break

named = [n for n in images if "_Image_" in n][:1]
hashed = [n for n in images if "_Image_" not in n][:1]
label_names = set(labels)
for im in named + hashed:
    base = im.rsplit("/", 1)[-1][:-4]
    cands = [f"RASSAR Dataset/filtered_labels/{base}.txt"]
    show(im, cands)
