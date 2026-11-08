import zipfile, json, random, pathlib, shutil
zip_path = r"C:\Users\jim chen\Downloads\SUNRGBD.zip"
out = pathlib.Path(r"E:\AAA new door\电子协会的比赛\results\vlm_eval")
img_out = out/"images"; img_out.mkdir(parents=True, exist_ok=True)
z = zipfile.ZipFile(zip_path)
idx_names = [n for n in z.namelist() if n.endswith("annotation2Dfinal/index.json")]

def bbox_area(poly, W=640, H=480):
    xs, ys = [], []
    for pt in poly:
        x, y = pt.get("x"), pt.get("y")
        if isinstance(x, (int, float)):
            xs.append(x); ys.append(y)
        else:
            xs += list(x or []); ys += list(y or [])
    if not xs:
        return 0
    return max(0,(max(xs)-min(xs)))*(max(0,(max(ys)-min(ys))))/(W*H)

def scan():
    buckets = {"toilet":[], "table":[], "neg":[]}
    for n in idx_names:
        try: d = json.loads(z.read(n).decode("utf-8-sig","replace"))
        except Exception: continue
        objs = d.get("objects") or []
        names = [(o or {}).get("name","") for o in objs]
        polys = d["frames"][0].get("polygon", [])
        area = {}
        for p in polys:
            i = p.get("object")
            if i is not None and i < len(names):
                area[names[i]] = area.get(names[i],0) + bbox_area([p])
        scene = d.get("name","")
        rec = dict(scene=scene, file=d.get("fileList"), objects=names, area={k:round(v,4) for k,v in area.items()})
        if area.get("toilet",0) >= 0.03 and len(buckets["toilet"]) < 40:
            buckets["toilet"].append(rec)
        elif area.get("table",0) >= 0.03 and "toilet" not in area and len(buckets["table"]) < 40:
            buckets["table"].append(rec)
        elif "toilet" not in area and "table" not in area and len(buckets["neg"]) < 40 and len(set(names)) >= 6:
            buckets["neg"].append(rec)
        if all(len(v) >= 40 for v in buckets.values()):
            break
    return buckets

b = scan()
picks = []
for key, n in (("toilet",10), ("table",6), ("neg",6)):
    for r in random.Random(11).sample(b[key], n):
        r["bucket"] = key
        picks.append(r)

labels = []
all_names = set(z.namelist())
counts = {"toilet":0, "table":0, "neg":0}
for r in picks:
    if counts[r["bucket"]] >= 4:
        continue
    scene = r["scene"]; fname = r["file"]
    if isinstance(fname, list):
        fname = str(fname[0]).rstrip("/").split("/")[-1]
    rel = scene.split("SUN-RGBD/")[-1] if scene.startswith("http") else scene
    rel = rel.replace("b3do/", "b3dodata/", 1) if rel.startswith("b3do/") else rel
    cand = [
        f"SUNRGBD/kv1/{rel}/image/{fname}",
        f"SUNRGBD/kv1/{rel}/fullres/{fname}",
        f"SUNRGBD/{rel}/image/{fname}",
        f"SUNRGBD/{rel}/fullres/{fname}",
    ]
    src = next((c for c in cand if c in all_names), None)
    if src is None:
        # last resort: any jpg whose path ends with scene basename + /image/ + fname
        tail = f"/{rel.split('/')[-1]}/image/{fname}"
        src = next((n for n in all_names if n.endswith(tail) and n.lower().endswith(".jpg")), None)
    if src is None:
        print("MISS", scene, fname); continue
    counts[r["bucket"]] += 1
    qid = "q%02d" % (len(labels) + 1)
    dst = img_out/(qid+".jpg")
    with z.open(src) as f, open(dst,"wb") as g: shutil.copyfileobj(f,g)
    labels.append(dict(qid=qid, source=src, bucket=r["bucket"],
                       toilet="present" if r["area"].get("toilet",0)>=0.03 else "absent",
                       table="present" if r["area"].get("table",0)>=0.03 else "absent",
                       scene_objects=r["objects"], areas=r["area"]))

# two out-of-scope controls
for src in (pathlib.Path(r"E:\AAA new door\电子协会的比赛\results\k1_006_test.jpg"),
            pathlib.Path(r"C:\Users\jim chen\Downloads\RASSAR Dataset\RASSAR Dataset\filtered_images\grab bar_Image_300.jpg")):
    if src.is_file():
        qid = "q%02d" % (len(labels) + 1)
        shutil.copy2(src, img_out/(qid+src.suffix))
        labels.append(dict(qid=qid, source=str(src), bucket="control",
                           toilet="absent", table="absent", scene_objects=["out_of_scope"], areas={}))

(out/"labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
print("scanned", len(idx_names), "selected", len(labels))
for r in labels: print(r["qid"], r["bucket"], "toilet=",r["toilet"], "table=",r["table"], r["source"].split("/")[-1])
z.close()
