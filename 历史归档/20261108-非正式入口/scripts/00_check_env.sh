#!/usr/bin/env bash
# 00_check_env.sh —— 板子身份确认 + 环境自检
# 用法: bash 00_check_env.sh
set -u
ok(){ printf "  \033[32m[PASS]\033[0m %s\n" "$1"; }
warn(){ printf "  \033[33m[WARN]\033[0m %s\n" "$1"; }
bad(){ printf "  \033[31m[FAIL]\033[0m %s\n" "$1"; }

echo "==================== 1. 身份 ===================="
echo "内核: $(uname -srm)"
if [ -r /proc/device-tree/model ]; then
  echo "板型(model): $(tr -d '\0' < /proc/device-tree/model)"
else
  echo "板型(model): 无 /proc/device-tree/model（可能是云实例或 x86 交叉环境）"
fi
echo "CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs)  | 核数: $(nproc)"
echo "uarch: $(grep -m1 'uarch' /proc/cpuinfo | cut -d: -f2- | xargs)"
echo "架构: $(uname -m)"
[ -r /etc/os-release ] && echo "系统: $(. /etc/os-release; echo "$PRETTY_NAME")"
for f in /etc/bianbu-release /etc/bianbu_version /etc/version; do
  [ -r "$f" ] && echo "Bianbu: $(head -c 200 "$f")"
done

echo "==================== 2. 内存 / 磁盘 ===================="
free -h
TOTAL_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if [ "$TOTAL_MB" -ge 7600 ]; then ok "内存 ${TOTAL_MB}MB —— 满足 8GB 要求（4B 模型必需）"
else bad "内存仅 ${TOTAL_MB}MB —— 4B 模型会 OOM，只能跑 2B"; fi
df -h / | tail -1
AVAIL_G=$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}')
if [ "${AVAIL_G:-0}" -ge 20 ]; then ok "根分区可用 ${AVAIL_G}G"; else warn "根分区仅 ${AVAIL_G}G，模型+编译产物可能不够（建议 ≥20G）"; fi

echo "==================== 3. 工具链 ===================="
for c in git cmake make g++ python3 wget curl; do
  if command -v "$c" >/dev/null 2>&1; then ok "$c -> $($c --version 2>&1 | head -1)"; else bad "缺少 $c"; fi
done
CM_VER=$(cmake --version 2>/dev/null | head -1 | awk '{print $3}')
if [ -n "${CM_VER:-}" ]; then
  MAJ=$(echo "$CM_VER" | cut -d. -f1); MIN=$(echo "$CM_VER" | cut -d. -f2)
  if [ "$MAJ" -gt 3 ] || { [ "$MAJ" -eq 3 ] && [ "$MIN" -ge 20 ]; }; then ok "cmake $CM_VER ≥ 3.20"; else warn "cmake $CM_VER 偏旧，llama.cpp 可能要求 ≥3.20"; fi
fi

echo "==================== 4. 温度 / 频率 ===================="
for z in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$z" ] && echo "  $(dirname "$z" | xargs basename): $(( $(cat "$z") / 1000 ))°C"
done
[ -r /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ] && \
  echo "  cpu0 当前频率: $(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq) / 1000 )) MHz"

echo "==================== 5. 结论 ===================="
if [ "$(uname -m)" = "riscv64" ] && [ "$TOTAL_MB" -ge 7600 ]; then
  echo "  ✅ riscv64 + ≥8GB：可以原生编译跑 赛题2（不需要交叉工具链）"
else
  echo "  ⚠️ 非 riscv64 或内存不足：请用 Bianbu Cloud 8G 实例"
fi
echo
echo "把上面「板型(model)」那一行发给我，我能确认是不是 MUSE Pi Pro。"
