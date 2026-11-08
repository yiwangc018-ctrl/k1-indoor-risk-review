#!/usr/bin/env bash
# 1) 契约层在"模型说不清"时的表现  2) 缩短提示词能不能降低延迟
set -u
cd /home/biandu/rv || exit 1

echo '=== A. 非数字输入（应显示 ? 并返回 3）==='
for q in "你好" "今天天气怎么样" "给我讲个笑话"; do
  echo "--- $q"
  python3 app/ask.py "$q" 2>&1 | grep -E '^\[ask\] (模型|契约)'
  echo "    退出码=$?"
done

echo
echo '=== B. 延迟对比：长提示词 vs 短提示词 ==='
python3 - <<'PY'
import json, time, urllib.request

def ask(system, user, n=6):
    payload = {
        "model": "qwen2.5:0.5b",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": 0, "num_predict": n},
    }
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    ms = (time.perf_counter() - t0) * 1000
    ec = d.get("eval_count") or 0
    ed = (d.get("eval_duration") or 0) / 1e9
    pc = d.get("prompt_eval_count") or 0
    return ms, (d.get("message") or {}).get("content", "").strip(), pc, (ec / ed if ed else 0)

LONG = "你是数字提取器。只输出阿拉伯数字，不要解释、不要标点、不要单位、不要换行。"
SHORT = "只输出数字。"

for label, sysmsg in (("长提示词(%d字)" % len(LONG), LONG), ("短提示词(%d字)" % len(SHORT), SHORT)):
    for user in ("输出数字123", "输出数字7"):
        ms, out, pc, tps = ask(sysmsg, user)
        print("  %-12s %-10s -> %-6r  %.0f ms  prompt_tokens=%d  %.1f tok/s"
              % (label, user, out, ms, pc, tps))
PY
