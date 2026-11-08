#!/usr/bin/env bash
# 板端 Python 依赖：装 smbus2。优先修好 venv，不行就退回 ~/.local
set -u

echo '--- 现状 ---'
ls -l "$HOME/hwenv/bin" 2>&1 | sed 's/^/  /'
python3 -V
python3 -m pip --version 2>&1 | sed 's/^/  system pip: /'

ok() { echo "[ OK ] $*"; }
bad() { echo "[FAIL] $*"; }

# 方案 A：venv（如果 venv 可用）
if [ -x "$HOME/hwenv/bin/python" ]; then
  if [ ! -x "$HOME/hwenv/bin/pip" ]; then
    echo '[setup] venv 里没有 pip，尝试 ensurepip'
    "$HOME/hwenv/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 \
      || echo '[warn] ensurepip 失败（缺 python3-venv？）'
  fi
  if [ -x "$HOME/hwenv/bin/pip" ]; then
    "$HOME/hwenv/bin/pip" install -q smbus2 2>/dev/null \
      || "$HOME/hwenv/bin/pip" install -q smbus2 -i https://pypi.tuna.tsinghua.edu.cn/simple
    if "$HOME/hwenv/bin/python" -c 'import smbus2' 2>/dev/null; then
      ok "smbus2 已装进 venv: $("$HOME/hwenv/bin/python" -c 'import smbus2;print(smbus2.__version__)')"
      echo "PYTHON=$HOME/hwenv/bin/python"
      exit 0
    fi
  fi
  echo '[warn] venv 路线没成，改走 ~/.local'
else
  echo '[warn] 没有 venv 解释器，改走 ~/.local'
fi

# 方案 B：系统 python3 + ~/.local（PEP 668 需要 --break-system-packages）
pip3 install -q --break-system-packages --user smbus2 2>/dev/null \
  || pip3 install -q --break-system-packages --user smbus2 -i https://pypi.tuna.tsinghua.edu.cn/simple
if python3 -c 'import smbus2' 2>/dev/null; then
  ok "smbus2 已装进 ~/.local: $(python3 -c 'import smbus2;print(smbus2.__version__)')"
  echo "PYTHON=python3"
  exit 0
fi

bad "两条路都失败，把上面的报错贴给我"
exit 1
