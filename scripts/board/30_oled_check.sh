#!/usr/bin/env bash
# =============================================================================
# 30_oled_check.sh —— 小屏接线与 I2C 总线自检（M0）
#
# 只读脚本：不改系统、不装包、不重启服务。它回答一个问题——
#   "内核到底看见这块屏了吗？"
#
# 用法：
#   bash 30_oled_check.sh              # 扫 i2c-4 和 i2c-3
#   BUSES="4 3 5" bash 30_oled_check.sh
#   EXPECT=0x3d bash 30_oled_check.sh  # 预期待测地址
#
# 判读：
#   出现 3c            → 接线/供电/上拉都对，可以做 M1
#   全是 --            → 见文末「全 -- 排查清单」
#   UU 出现在某地址     → 该地址已被内核驱动占用（不是错，但要避开）
# =============================================================================
set -u

BUSES="${BUSES:-4 3}"
EXPECT="$(printf '%s' "${EXPECT:-0x3C}" | tr 'A-F' 'a-f')"
ADDR="$(printf '%s' "${EXPECT#0x}")"
FOUND=0

hdr() { printf '\n=== %s ===\n' "$*"; }
ok()  { printf '  [ OK ] %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; }
warn(){ printf '  [WARN] %s\n' "$*"; }
tip() { printf '    %s\n' "$*"; }

# -----------------------------------------------------------------------------
hdr "1. 板卡与环境"
printf '  arch      : %s\n' "$(uname -m)"
printf '  kernel    : %s\n' "$(uname -r)"
[ -r /proc/device-tree/model ] && printf '  model     : %s\n' "$(tr -d '\0' < /proc/device-tree/model)"
printf '  python3   : %s\n' "$(python3 -V 2>&1 | head -1)"

if [ "$(uname -m)" != "riscv64" ]; then
  warn "不是 riscv64：这脚本要在板子上跑（PC 上用 python3 display.py --mock 验证链路）"
fi

# -----------------------------------------------------------------------------
hdr "2. I2C 设备节点"
shopt -s nullglob
nodes=(/dev/i2c-*)
shopt -u nullglob
if [ ${#nodes[@]} -eq 0 ]; then
  bad "没有 /dev/i2c-* —— i2c-dev 模块没加载？试： sudo modprobe i2c-dev"
else
  for n in "${nodes[@]}"; do
    perms="$(ls -l "$n" | awk '{print $1, $3, $4}')"
    if [ -r "$n" ] && [ -w "$n" ]; then ok "$n  $perms  （可读写）"
    else warn "$n  $perms  （当前用户不能读写）"; fi
  done
fi

# -----------------------------------------------------------------------------
hdr "3. 工具链"
if command -v i2cdetect >/dev/null 2>&1; then ok "i2cdetect: $(command -v i2cdetect)"
else bad "缺 i2c-tools： sudo apt install -y i2c-tools"; fi

if python3 -c 'import smbus2' 2>/dev/null; then ok "python3 smbus2 可用"
else warn "缺 smbus2： source ~/hwenv/bin/activate && pip install smbus2"; fi

if python3 -c 'import yaml' 2>/dev/null; then ok "python3 pyyaml 可用"
else warn "缺 pyyaml（config.yaml 会退回默认值）： pip install pyyaml"; fi

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
FOUND_APP=""
for cand in "$SELF_DIR/../../app/display.py" "$PWD/app/display.py" "$HOME/cie-k1-voice-point/app/display.py"; do
  [ -f "$cand" ] && { FOUND_APP="$cand"; break; }
done
if [ -n "$FOUND_APP" ]; then
  ok "找到 $FOUND_APP"
else
  warn "没找到 app/display.py —— 把 app/ 整个目录拷到板子上（如 ~/rv/app/），或从仓库根目录跑本脚本"
fi

# -----------------------------------------------------------------------------
# 扫描一条总线：先不用 sudo，失败再试 sudo
scan_bus() {
  local b="$1" out rc
  out="$(i2cdetect -y -r "$b" 2>&1)"; rc=$?
  if [ $rc -ne 0 ]; then
    if printf '%s' "$out" | grep -qiE 'permission denied|operation not permitted'; then
      warn "i2c-$b 需要 root，改用 sudo（可能要输密码）"
      out="$(sudo i2cdetect -y -r "$b" 2>&1)"; rc=$?
    fi
  fi
  printf '%s\n' "$out"
  return $rc
}

for b in $BUSES; do
  hdr "4. 扫描 /dev/i2c-$b"
  if [ ! -e "/dev/i2c-$b" ]; then
    warn "/dev/i2c-$b 不存在，跳过"
    continue
  fi
  out="$(scan_bus "$b")"
  rc=$?
  if [ $rc -ne 0 ]; then
    bad "i2cdetect -y -r $b 失败："
    printf '%s\n' "$out" | sed 's/^/         /'
    continue
  fi
  printf '%s\n' "$out" | sed 's/^/         /'

  # 表里形如 "3c" / "68" / "UU" 的格子
  grid="$(printf '%s\n' "$out" | sed -n 's/^[0-9a-f][0-9a-f]:[[:space:]]*//p' | tr ' ' '\n' \
          | grep -E '^[0-9a-f]{2}$|^UU$' | sort -u | tr '\n' ' ')"
  if [ -z "$grid" ]; then
    warn "i2c-$b 上没有设备"
  else
    printf '  该总线上的地址: %s\n' "$grid"
    if printf '%s\n' "$out" | grep -qw "$ADDR"; then
      ok "在 i2c-$b 上找到 $EXPECT —— 屏幕已被内核看见"
      FOUND=1
    elif printf '%s\n' "$out" | grep -q 'UU'; then
      warn "只有 UU（该地址被内核驱动占用），没有可读的 $EXPECT"
    fi
  fi
done

# -----------------------------------------------------------------------------
hdr "5. 结论"
if [ "$FOUND" = "1" ]; then
  ok "M0 通过。下一步（按顺序）："
  tip "python3 app/display.py --selftest                 # 边框→四角→文字，验证寻址"
  tip "python3 app/display.py --text 123                 # 写死一个数，确认驱动"
  tip "python3 app/ask.py --mock \"输出数字123\"           # 不连模型验证链路"
  tip "python3 app/ask.py \"输出数字123\"                  # 连模型，完整闭环"
  echo
  tip "别忘了把总线号写进 app/config.yaml: display.bus = $BUSES 里命中的那个"
else
  bad "所有总线上都没看到 $EXPECT。按下面顺序查（90% 是前两条）："
  echo
  tip "1) 四根线有没有接错——两个信号必须同属一组："
  tip "     I2C-4 组: SCL->AP_I2C4_SCL_3V3   SDA->AP_I2C4_SDA_3V3"
  tip "     I2C-3 组: SCL->AP_I2C3_SCL_3V3   SDA->AP_I2C3_SDA_3V3"
  tip "   一根接 I2C-3、一根接 I2C-4 是最常见的错，总线永远扫不到。"
  tip "2) VCC 必须接 VCC3V3_SYS（3.3V），不要接 5V：模块的上拉电阻会把 SCL/SDA 拉到 5V，"
  tip "   打坏 K1 的 3.3V IO。"
  tip "3) GND 要共地；杜邦线插到底，面包板中间断开的排要跨接。"
  tip "4) 屏幕没供电/供电不足：换一根线，或直接用板子的 3.3V 排针。"
  tip "5) 屏幕本身不一定是 SSD1306：1.3 寸常见 SH1106（地址同为 0x3C）。"
  tip "   能扫到 3c 但显示偏移 2 像素 → 把 config.yaml 的 driver 改成 sh1106。"
  echo
  tip "权限不够时（节点存在但读写失败）："
  tip "  sudo usermod -aG i2c \$USER        # 然后重新登录"
  tip "  echo 'KERNEL==\"i2c-[0-9]*\", MODE=\"0666\"' | sudo tee /etc/udev/rules.d/99-i2c.rules"
  tip "  sudo udevadm control --reload && sudo udevadm trigger"
  tip "  确认没有用 sudo python 掩盖问题（会用 root 环境、埋权限坑）"
fi
echo
exit $(( FOUND == 1 ? 0 : 1 ))
