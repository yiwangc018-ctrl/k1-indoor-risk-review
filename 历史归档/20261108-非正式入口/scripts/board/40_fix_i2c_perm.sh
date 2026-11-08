#!/usr/bin/env bash
# 需要 root：sudo bash 40_fix_i2c_perm.sh
# 1) 装 i2c-tools  2) 给 /dev/i2c-* 开 0666  3) 立刻扫 3/4 两条总线
set -u
export DEBIAN_FRONTEND=noninteractive

echo '=== 1. 安装 i2c-tools ==='
if command -v i2cdetect >/dev/null 2>&1; then
  echo '  已安装，跳过'
else
  apt-get update -qq || echo '  [warn] apt update 失败，继续尝试安装'
  apt-get install -y -qq i2c-tools || { echo '  [FAIL] 装不上 i2c-tools'; exit 1; }
fi
i2cdetect -V 2>&1 | sed 's/^/  /'

echo '=== 2. udev 规则（/dev/i2c-* -> 0666）==='
cat > /etc/udev/rules.d/99-i2c.rules <<'EOF'
KERNEL=="i2c-[0-9]*", MODE="0666"
EOF
udevadm control --reload >/dev/null 2>&1
udevadm trigger --subsystem-match=i2c-dev >/dev/null 2>&1
udevadm trigger >/dev/null 2>&1
sleep 1
ls -l /dev/i2c-* | sed 's/^/  /'

echo '=== 3. 扫描总线 ==='
for b in 4 3; do
  echo "--- i2c-$b ---"
  if i2cdetect -y -r "$b" 2>&1 | sed 's/^/  /'; then
    :
  else
    echo '  [FAIL] 扫描失败'
  fi
done

echo '=== 4. 结论 ==='
found=0
for b in 4 3; do
  if i2cdetect -y -r "$b" 2>/dev/null | grep -qw '3c'; then
    echo "  [ OK ] 在 i2c-$b 上发现 0x3C  —— 屏接在 /dev/i2c-$b"
    found=1
  fi
done
if [ "$found" = "0" ]; then
  echo '  [FAIL] 两条总线上都没看到 0x3C'
  echo '         先确认：屏的 VCC/GND/SCL/SDA 真的接上了吗？SCL 和 SDA 必须同属一组。'
  echo '         用 I2C-4 组：SCL->AP_I2C4_SCL_3V3  SDA->AP_I2C4_SDA_3V3'
  echo '         用 I2C-3 组：SCL->AP_I2C3_SCL_3V3  SDA->AP_I2C3_SDA_3V3'
else
  echo '  下一步（普通用户即可，不用 sudo）：'
  echo '    python3 ~/rv/app/display.py --selftest'
fi
