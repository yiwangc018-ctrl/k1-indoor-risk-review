#!/usr/bin/env bash
# 03_bench.sh —— baseline / 优化版 性能测试，自动出 CSV（赛题2 的"证据链"）
# 用法: bash 03_bench.sh <模型.gguf> <标签>           例: 03_bench.sh qwen2b.gguf 2b_baseline
#       LABEL=2b_opt_kvq8 bash 03_bench.sh qwen2b.gguf 2b_opt
# 依赖: llama-bench 在 PATH 里（01_build_llamacpp.sh 产物）
set -euo pipefail
MODEL="${1:?用法: 03_bench.sh <模型.gguf> <标签>}"
LABEL="${2:-run}"
OUT="results"; mkdir -p "$OUT"
STAMP=$(date +%Y%m%d-%H%M%S)
CSV="$OUT/bench_${LABEL}_${STAMP}.csv"
LOG="$OUT/bench_${LABEL}_${STAMP}.log"
THREADS="${THREADS:-4 8}"          # 扫描的线程数
BATCH="${BATCH:-512 2048}"          # prefill 用的 batch（-b）
CTK="${CTK:-f16}"                   # KV cache 类型：f16 / q8_0 / q4_0
CTKV="${CTKV:-$CTK}"

echo "== 记录环境（评委要复现，这些必须写进报告）" | tee "$LOG"
{ uname -srm; nproc; grep -m1 'uarch' /proc/cpuinfo; free -h | head -2;
  [ -r /sys/class/thermal/thermal_zone0/temp ] && echo "temp=$(( $(cat /sys/class/thermal/thermal_zone0/temp)/1000 ))C";
  echo "model=$MODEL"; echo "kv=$CTK/$CTKV"; echo "threads=$THREADS"; echo "batch=$BATCH"; } | tee -a "$LOG"

echo "== A. 短上下文矩阵（pp512 / tg128）" | tee -a "$LOG"
: > "$CSV"
echo "phase,model,label,threads,batch,kv_type,test,pp,tg,t_s,tg_ts" > "$CSV.tmp"
for t in $THREADS; do
  for b in $BATCH; do
    echo "--- threads=$t batch=$b" | tee -a "$LOG"
    llama-bench -m "$MODEL" -p 512 -n 128 -t "$t" -b "$b" -ub "$b" \
      -ctk "$CTK" -ctv "$CTKV" -r 3 -o csv 2>>"$LOG" \
      | awk -F, -v t="$t" -v b="$b" -v lab="$LABEL" -v kv="$CTK" 'NR>1 && NF>=7 {
          printf "short,pp512_tg128,%s,%s,%s,%s,%s,%s,%s,%s\n", $1, lab, t, b, kv, $2, $3, $4, $5 }' \
      >> "$CSV.tmp" || echo "⚠️ llama-bench 失败，见 $LOG" | tee -a "$LOG"
  done
done

echo "== B. 长上下文衰减（2K / 4K / 8K / 16K，只出 prefill）" | tee -a "$LOG"
for p in 2048 4096 8192 16384; do
  echo "--- pp$p" | tee -a "$LOG"
  llama-bench -m "$MODEL" -p "$p" -n 32 -t 8 -ctk "$CTK" -ctv "$CTKV" -r 2 -o csv 2>>"$LOG" \
    | awk -F, -v p="$p" -v lab="$LABEL" -v kv="$CTK" 'NR>1 && NF>=7 {
        printf "long,pp%s,%s,8,,%s,%s,%s,%s,%s\n", p, lab, kv, $1, $3, $4, $5 }' \
    >> "$CSV.tmp" || echo "⚠️ 长上下文 $p 失败（8G 内存大概率 OOM，这本身就是可写的结论）" | tee -a "$LOG"
done

mv "$CSV.tmp" "$CSV"
echo | tee -a "$LOG"
echo "✅ 结果: $CSV" | tee -a "$LOG"
column -s, -t "$CSV" 2>/dev/null || cat "$CSV"
echo
echo "写进报告的三句话模板："
echo "  1) 测试环境：K1(X60×8) / ${CTK} KV / batch ${BATCH} / 3 次取中位数"
echo "  2) baseline：pp512=__ t/s, tg128=__ t/s（见 $CSV）"
echo "  3) 优化后：__ → __（+__%），收益来源：__"
