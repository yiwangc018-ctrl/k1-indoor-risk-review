"""K1 smoke test: SpaceMIT EP, yolov8n.q.onnx, latency + node provider profile."""
import collections
import json
import time

import numpy as np
import onnxruntime as ort
import spacemit_ort

MODEL = "/home/biandu/k1demo/models/yolov8n.q.onnx"

so = ort.SessionOptions()
so.intra_op_num_threads = 4
so.inter_op_num_threads = 1
so.enable_profiling = True
so.profile_file_prefix = "/tmp/k1demo_profile"
s = ort.InferenceSession(MODEL, sess_options=so, providers=["SpaceMITExecutionProvider"])
print("session_providers", s.get_providers())

inp = s.get_inputs()[0]
x = np.random.rand(1, 3, 640, 640).astype(np.float32)
y = s.run(None, {inp.name: x})[0]
print("output", y.shape, float(y.min()), float(y.max()))

for _ in range(3):
    s.run(None, {inp.name: x})
ts = []
for _ in range(20):
    t = time.perf_counter()
    s.run(None, {inp.name: x})
    ts.append((time.perf_counter() - t) * 1000)
ts.sort()
print("p50_ms", round(ts[len(ts) // 2], 2), "min_ms", round(ts[0], 2), "max_ms", round(ts[-1], 2))

prof = s.end_profiling()
events = json.load(open(prof, encoding="utf-8"))
prov = collections.Counter(
    e.get("args", {}).get("provider") for e in events if e.get("args", {}).get("provider")
)
print("node_providers", dict(prov))
