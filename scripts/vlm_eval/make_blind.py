import json, random, pathlib
from PIL import Image, ImageDraw
root = pathlib.Path(r"E:\AAA new door\电子协会的比赛\results\vlm_eval")
labels = json.loads((root/"labels.json").read_text(encoding="utf-8"))
imgs = [labels[i]["qid"] for i in range(len(labels))]
order = imgs[:]; random.Random(2024).shuffle(order)
mapping = {("s%02d"%(i+1)): q for i,q in enumerate(order)}
(root/"blind_map.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
files = sorted((root/"images").glob("q*.jpg"))
files = [f for f in files if f.stem in imgs]
blind = root/"blind"; blind.mkdir(exist_ok=True)
for s, q in mapping.items():
    src = root/"images"/(q+".jpg")
    (blind/(s+".jpg")).write_bytes(src.read_bytes())

codes = ["s%02d"%i for i in range(1,15)]
tile=(480,380)
for page in range(2):
    part = codes[page*7:(page+1)*7]
    sheet = Image.new("RGB", (3*tile[0], 3*tile[1]), "white")
    d = ImageDraw.Draw(sheet)
    for i, code in enumerate(part):
        im = Image.open(blind/(code+".jpg")).convert("RGB"); im.thumbnail((tile[0]-8, tile[1]-28))
        x,y = (i%3)*tile[0], (i//3)*tile[1]
        sheet.paste(im,(x+4,y+24)); d.text((x+6,y+6), code, fill="black")
    sheet.save(root/("blind_sheet_%d.jpg"%(page+1)), quality=88)
print("sheets ready", len(mapping))
