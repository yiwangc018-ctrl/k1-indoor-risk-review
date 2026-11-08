import collections, json, pathlib
from PIL import Image, ImageDraw

root = pathlib.Path(r"C:\Users\jim chen\Downloads\RASSAR Dataset\RASSAR Dataset")
classes = [l.strip() for l in (root/"classes.txt").read_text(encoding="utf-8-sig").splitlines() if l.strip()]
out = pathlib.Path(r"E:\AAA new door\电子协会的比赛\results\vlm_eval")
img_dir, lab_dir = root/"filtered_images", root/"filtered_labels"

def info(img):
    lab = lab_dir/(img.stem + ".txt")
    boxes = []
    if lab.is_file():
        for line in lab.read_text(encoding="utf-8-sig").splitlines():
            p = line.split()
            if len(p) == 5:
                c = int(p[0]); x,y,w,h = map(float, p[1:])
                boxes.append((classes[c] if c < len(classes) else str(c), w*h, x, y, w, h))
    return boxes

cands = collections.defaultdict(list)
for img in sorted(img_dir.glob("*.jpg")):
    try: w,h = Image.open(img).size
    except Exception: continue
    if min(w,h) < 700: continue
    boxes = info(img)
    kinds = {b[0] for b in boxes}
    area = sum(b[1] for b in boxes if b[0]=="Grab Bar")
    stem = img.stem.split("_Image_")[0]
    if "Grab Bar" in kinds and 0.004 < area < 0.6: cands["grab"].append((img, boxes, w, h, area))
    if stem == "handrail": cands["handrail"].append((img, boxes, w, h, 0))
    if stem in ("03c38ab45e569005","090e1a5934783118","102b50f06b79b1fc"): cands["knife"].append((img, boxes, w, h, 0))
    if stem == "rug": cands["rug"].append((img, boxes, w, h, 0))
    if stem == "child safety gates": cands["gate"].append((img, boxes, w, h, 0))

def pick(k, n, key=None):
    v = cands[k]
    v.sort(key=key or (lambda t: -t[4]))
    return v[:n]

sel = []
sel += [("A%02d"%i, x, "grab") for i,x in enumerate(pick("grab", 12), 1)]
sel += [("B%02d"%i, x, "handrail") for i,x in enumerate(pick("handrail", 8), 1)]
sel += [("C%02d"%i, x, "knife") for i,x in enumerate(pick("knife", 3), 1)]
sel += [("D%02d"%i, x, "rug") for i,x in enumerate(pick("rug", 6), 1)]
sel += [("E%02d"%i, x, "gate") for i,x in enumerate(pick("gate", 3), 1)]

tile_w, tile_h, cols = 320, 260, 5
rows = (len(sel)+cols-1)//cols
sheet = Image.new("RGB", (cols*tile_w, rows*tile_h), "white")
draw = ImageDraw.Draw(sheet)
manifest = []
for i,(code,(img,boxes,w,h,area),kind) in enumerate(sel):
    im = Image.open(img).convert("RGB"); im.thumbnail((tile_w-8, tile_h-30))
    x0, y0 = (i%cols)*tile_w, (i//cols)*tile_h
    sheet.paste(im, (x0+4, y0+24))
    draw.text((x0+6, y0+6), f"{code} {kind} {w}x{h}", fill="black")
    manifest.append(dict(code=code, kind=kind, file=str(img), w=w, h=h,
                         classes=sorted({b[0] for b in boxes}), box_area=round(area,4)))
sheet.save(out/"candidates.jpg", quality=88)
(out/"candidates.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print("candidates:", len(sel), "->", out/"candidates.jpg")
