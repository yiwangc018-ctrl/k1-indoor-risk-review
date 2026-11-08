import collections
import os
import sys
import zipfile

path = r"C:\Users\jim chen\Downloads\RASSAR Dataset.zip"
z = zipfile.ZipFile(path)
infos = z.infolist()
print("entries:", len(infos))
print("total uncompressed GB:", round(sum(i.file_size for i in infos) / 1024**3, 2))

ext = collections.Counter(os.path.splitext(i.filename)[1].lower() for i in infos if not i.is_dir())
print("extensions:", ext.most_common(15))

top = collections.Counter(i.filename.split("/")[0] for i in infos if i.filename.strip())
print("top-level:", top.most_common(10))

print("\n--- first 40 entries ---")
for i in infos[:40]:
    kind = "DIR " if i.is_dir() else "FILE"
    print(f"{kind} {i.file_size:>12,}  {i.filename}")
