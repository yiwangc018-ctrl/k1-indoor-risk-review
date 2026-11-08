"""K1 image detection smoke: letterbox -> SpaceMIT EP -> YOLOv8 decode -> draw."""
import sys
import time

import cv2
import numpy as np
import onnxruntime as ort
import spacemit_ort

MODEL = "/home/biandu/k1demo/models/yolov8n.q.onnx"
COCO = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator",
    "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
]


def letterbox(img, size=640):
    h, w = img.shape[:2]
    r = min(size / w, size / h)
    nw, nh = round(w * r), round(h * r)
    left, top = (size - nw) // 2, (size - nh) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh))
    return canvas, (r, left, top)


def nms(boxes, scores, iou):
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        lt = np.maximum(boxes[i, :2], boxes[rest, :2])
        rb = np.minimum(boxes[i, 2:], boxes[rest, 2:])
        wh = np.clip(rb - lt, 0, None)
        inter = wh[:, 0] * wh[:, 1]
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        ov = inter / np.maximum(area[i] + area[rest] - inter, 1e-9)
        order = rest[ov <= iou]
    return keep


def decode(out, meta, conf=0.25, iou=0.45):
    p = out[0].T
    cls = p[:, 4:].argmax(1)
    score = p[np.arange(len(p)), 4 + cls]
    m = score >= conf
    p, cls, score = p[m], cls[m], score[m]
    if not len(p):
        return []
    xywh = p[:, :4]
    boxes = np.concatenate((xywh[:, :2] - xywh[:, 2:] / 2, xywh[:, :2] + xywh[:, 2:] / 2), axis=1)
    r, left, top = meta
    dets = []
    for c in np.unique(cls):
        idx = np.where(cls == c)[0]
        for k in nms(boxes[idx], score[idx], iou):
            b = boxes[idx][k].copy()
            b[[0, 2]] = (b[[0, 2]] - left) / r
            b[[1, 3]] = (b[[1, 3]] - top) / r
            dets.append((int(c), float(score[idx][k]), b))
    return dets


def main(path, out_path):
    img = cv2.imread(path)
    if img is None:
        raise SystemExit(f"cannot read {path}")
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    sess = ort.InferenceSession(MODEL, sess_options=so, providers=["SpaceMITExecutionProvider"])
    name = sess.get_inputs()[0].name
    print("providers", sess.get_providers(), "input", sess.get_inputs()[0].shape)

    canvas, meta = letterbox(img)
    x = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
    print("warmup x5")
    for _ in range(5):
        out = sess.run(None, {name: x})[0]
    pre, inf, post, tot = [], [], [], []
    for _ in range(20):
        t0 = time.perf_counter()
        canvas, meta = letterbox(img)
        x = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
        t1 = time.perf_counter()
        out = sess.run(None, {name: x})[0]
        t2 = time.perf_counter()
        decode(out, meta)
        t3 = time.perf_counter()
        pre.append((t1 - t0) * 1000)
        inf.append((t2 - t1) * 1000)
        post.append((t3 - t2) * 1000)
        tot.append((t3 - t0) * 1000)
    dets = decode(out, meta)
    p = lambda a: (round(sorted(a)[len(a) // 2], 2), round(sorted(a)[95 * len(a) // 100], 2))
    print(f"steady p50/p95 ms  pre {p(pre)}  infer {p(inf)}  post {p(post)}  total {p(tot)}")

    for c, s, b in sorted(dets, key=lambda d: -d[1]):
        print(f"  {COCO[c]:14s} {s:.3f}  [{b[0]:.0f},{b[1]:.0f},{b[2]:.0f},{b[3]:.0f}]")
        cv2.rectangle(img, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (0, 0, 255), 2)
        cv2.putText(img, f"{COCO[c]} {s:.2f}", (int(b[0]), max(int(b[1]) - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    print("detections", len(dets))
    cv2.imwrite(out_path, img)
    print("saved", out_path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
