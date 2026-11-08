#!/usr/bin/env bash
# M3 验收：连续 5 条指令，统计"契约成功率"和热态延迟
set -u
cd /home/biandu/rv || exit 1

QUERIES=("输出数字123" "输出数字7" "输出数字8888" "输出数字42" "输出数字5")

for q in "${QUERIES[@]}"; do
  echo "=== $q"
  python3 app/ask.py "$q" 2>&1 | grep -E '^\[ask\] (模型|契约|失败|注意)'
done

echo
echo "=== 统计（最近 5 条日志）==="
python3 - <<'PY'
import json, os, statistics
p = "/home/biandu/rv/results/llm_display.jsonl"
if not os.path.exists(p):
    print("  没有日志文件"); raise SystemExit
rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()][-5:]
ok = [r for r in rows if r.get("ok")]
ms = [r["ms"] for r in ok]
print("  成功率 : %d/%d" % (len(ok), len(rows)))
print("  来源   : %s" % ", ".join(sorted({r.get("source","?") for r in rows})))
if ms:
    print("  延迟   : 首条 %.0f ms（含冷启动） | 热态中位 %.0f ms | 最快 %.0f ms"
          % (ms[0], statistics.median(ms[1:]) if len(ms) > 1 else ms[0], min(ms)))
for r in rows:
    print("    %-14s -> %-6s %-6s %6.0f ms  match=%s"
          % (r.get("input","")[:14], r.get("parsed"), r.get("source"), r.get("ms",0), r.get("match")))
PY
