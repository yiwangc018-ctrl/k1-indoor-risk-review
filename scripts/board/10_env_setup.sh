#!/usr/bin/env bash
# =============================================================================
# CIE-进迭时空 赛题1 · 板端环境一键准备（MUSE Pi Pro / Bianbu LXQt）
# 用法：
#   bash 10_env_setup.sh --check     # 只自检，不安装（建议先跑这个，输出存档）
#   bash 10_env_setup.sh             # 自检 + 安装依赖 + 建 venv + 配权限
# 注意：
#   * 全程不需要联网下载大模型；模型请从 ref/cases 里解压（见输出提示）
#   * 不要装 anaconda（RISC-V 无适配，官方 FAQ 明确）
#   * Windows 复制过来的脚本先转行尾：sed -i 's/\r$//' 10_env_setup.sh
# =============================================================================
set -u

MODE="${1:-install}"
VENV="${VENV:-$HOME/hwenv}"
PIP_MIRROR="${PIP_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_EXTRA="${PIP_EXTRA:-https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple}"

ok(){   printf '  [ OK ] %s\n' "$*"; }
warn(){ printf '  [WARN] %s\n' "$*"; }
bad(){  printf '  [FAIL] %s\n' "$*"; }
hdr(){  printf '\n=== %s ===\n' "$*"; }

# -----------------------------------------------------------------------------
hdr "1. 板卡与系统"
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
printf '  model       : %s\n' "$MODEL"
printf '  arch/kernel : %s / %s\n' "$(uname -m)" "$(uname -r)"
[ -r /etc/os-release ] && printf '  os          : %s\n' "$(. /etc/os-release; echo "$PRETTY_NAME")"
printf '  cpu cores   : %s\n' "$(nproc)"
printf '  mem total   : %s\n' "$(free -h | awk '/^Mem:/{print $2}')"
printf '  disk free   : %s\n' "$(df -h / | awk 'NR==2{print $4}')"
printf '  uarch       : %s\n' "$(grep -m1 -i 'uarch' /proc/cpuinfo | cut -d: -f2- | xargs || echo '-')"

case "$MODEL" in
  *"MUSE Pi Pro"*|*"MUSE-Pi-Pro"*) ok "板型匹配赛题 1 要求的 MUSE Pi Pro" ;;
  *K1*|*k1*) warn "是 K1 平台但不是标准 MUSE Pi Pro 字符串，去 QQ 群确认参赛资格并截图存档" ;;
  *) warn "未识别为 K1 板卡（model=$MODEL），先确认板子型号" ;;
esac

MEM_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
if [ "${MEM_MB:-0}" -ge 7500 ]; then ok "内存 ${MEM_MB}MB，够跑 0.5B/1B 本地模型"
elif [ "${MEM_MB:-0}" -ge 3500 ]; then warn "内存 ${MEM_MB}MB，只能跑 0.5B，别加载 4B"
else bad "内存 ${MEM_MB}MB 偏小，检查是否被其他进程占用"; fi

# -----------------------------------------------------------------------------
hdr "2. 外设"
CAM=$(ls /dev/video* 2>/dev/null | tr '\n' ' ')
if [ -n "$CAM" ]; then
  ok "摄像头: $CAM"
  command -v v4l2-ctl >/dev/null && v4l2-ctl --list-devices 2>/dev/null | sed 's/^/         /'
  case "$CAM" in *video*) : ;; esac
else warn "未发现 /dev/video*：接 USB(UVC) 摄像头；MIPI 摄像头无 Python 接口，不要用"; fi

if command -v arecord >/dev/null; then
  MIC=$(arecord -l 2>/dev/null | grep -c '^card')
  if [ "${MIC:-0}" -gt 0 ]; then ok "录音设备 $MIC 个（arecord -l 查看 card/device）"
  else warn "arecord 认不到录音设备：接 USB 麦克风/声卡，或 sudo usermod -aG audio \$USER 后重登"; fi
else warn "未安装 alsa-utils（脚本会装），装完用 arecord -l 查麦克风"; fi

if [ -e /dev/gpiochip0 ]; then ok "GPIO: /dev/gpiochip0 存在，权限 $(stat -c '%a' /dev/gpiochip0)"
else warn "/dev/gpiochip0 不存在，没法用 gpiozero 驱动舵机"; fi

if command -v i2cdetect >/dev/null; then ok "i2c 工具已装（PCA9685 舵机板走 I2C-3/4）"; else warn "i2c-tools 未装（想用 PCA9685 才需要）"; fi

# -----------------------------------------------------------------------------
hdr "3. 工具链与运行时"
printf '  python3 : %s\n' "$(python3 -V 2>&1)"
[ "$(python3 -c 'import sys;print(sys.version_info>=(3,11))' 2>/dev/null)" = "True" ] && ok "Python ≥3.11" || warn "Python 版本偏低，部分依赖可能装不上"
for c in gcc g++ cmake make git wget curl; do
  if command -v "$c" >/dev/null; then ok "$c $(command -v $c)"; else warn "$c 未安装（编译 ai-sdk 时才需要）"; fi
done

if python3 -c 'import onnxruntime as o;print(o.get_available_providers())' >/tmp/ort_providers.txt 2>/dev/null; then
  PROV=$(cat /tmp/ort_providers.txt)
  ok "onnxruntime providers: $PROV"
  case "$PROV" in
    *SpaceMIT*|*spacemit*) ok "看到 spacemit provider —— CV 可以吃 AI 核（6 分项的关键）" ;;
    *) warn "只有 CPU provider：视觉会全压 CPU，AI 核占用那条 6 分会丢。装 python3-spacemit-ort 后重试" ;;
  esac
else
  warn "onnxruntime 尚未安装（python3-spacemit-ort 会带上）"
fi

if command -v ollama >/dev/null; then ok "ollama 已装 $(ollama --version 2>/dev/null | head -1)"
else warn "ollama 未装：LLM 决策层需要它（sudo apt install spacemit-ollama-toolkit）"; fi

# -----------------------------------------------------------------------------
if [ "$MODE" = "--check" ] || [ "$MODE" = "check" ]; then
  hdr "自检完成（未做任何修改）"
  echo "  把上面的输出整段存进 docs/env-check.txt，就是『部署便捷性/可复现』的原始证据。"
  echo "  确认无误后跑：bash $0"
  exit 0
fi

# -----------------------------------------------------------------------------
hdr "4. 安装系统依赖"
sudo apt-get update || warn "apt update 失败，检查网络/镜像源"

# 只装 apt 里真实存在的包，避免一个包名写错整条命令失败
install_if_available(){
  local avail=() p
  for p in "$@"; do
    if apt-cache show "$p" >/dev/null 2>&1; then avail+=("$p"); else warn "apt 源里没有 $p，跳过"; fi
  done
  [ ${#avail[@]} -gt 0 ] && sudo apt-get install -y "${avail[@]}"
}

install_if_available \
  python3-venv python3-dev python3-pip \
  libopenblas-dev libcjson-dev libasound2-dev portaudio19-dev \
  liblgpio1 liblgpio-dev python3-gpiozero \
  python3-spacemit-ort \
  ffmpeg alsa-utils v4l-utils i2c-tools \
  git wget curl jq htop stress-ng \
  build-essential cmake

ok "系统依赖处理完毕"

# -----------------------------------------------------------------------------
hdr "5. Python 虚拟环境"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV" && ok "创建 $VENV"
else ok "复用已有 $VENV"; fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

"$VENV/bin/pip" config set global.index-url "$PIP_MIRROR"      >/dev/null 2>&1 || true
"$VENV/bin/pip" config set global.extra-index-url "$PIP_EXTRA" >/dev/null 2>&1 || true
"$VENV/bin/pip" install -q --upgrade pip wheel setuptools || warn "pip 升级失败（不致命）"

# 基础依赖（GPIO / 音频 / 序列化）
"$VENV/bin/pip" install -q gpiozero==2.0.5 lgpio==0.2.2.0 colorzero==2.0 \
  pyserial smbus2 numpy pyyaml requests || warn "基础 python 依赖有失败项，逐条重试"

# 视觉：OpenCV + onnxruntime(spacemit 版) + spacemit-ort。
# 注意：RISC-V 上 onnxruntime 必须用 spacemit 的 local version（官方 case 固定
# onnxruntime==1.20.1+spacemit / spacemit-ort==2.0.0+beta2），从 extra-index 拉。
"$VENV/bin/pip" install -q "opencv-python==4.12.0.88" || warn "opencv-python 安装失败，可改用 apt 的 python3-opencv（需 venv --system-site-packages）"
"$VENV/bin/pip" install -q "spacemit-ort==2.0.0+beta2" || warn "spacemit-ort 安装失败：先 pip install -U spacemit-ort，或确认 extra-index 配好"
"$VENV/bin/pip" install -q "onnxruntime==1.20.1+spacemit" || warn "onnxruntime(spacemit) 安装失败：视觉只能跑 CPU，AI 核那 6 分要另想办法"

"$VENV/bin/python" - <<'PY'
try:
    import onnxruntime as o
    print("  providers:", o.get_available_providers())
except Exception as e:
    print("  [WARN] onnxruntime 导入失败:", e)
PY

# -----------------------------------------------------------------------------
hdr "6. 权限（音频 / GPIO / 串口）"
sudo usermod -aG audio "$USER" && ok "已加入 audio 组（需重新登录生效）"
sudo chmod a+rw /dev/gpiochip0 2>/dev/null && ok "临时放开 /dev/gpiochip0"
sudo tee /etc/udev/rules.d/90-k1-gpio.rules >/dev/null <<'EOF'
KERNEL=="gpiochip*", MODE="0666"
KERNEL=="ttyACM*", MODE="0666"
KERNEL=="ttyUSB*", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger || warn "udev 重载失败（不影响本次运行）"
ok "已写 udev 规则：gpiochip / ttyACM / ttyUSB 开机即可读写"

# -----------------------------------------------------------------------------
hdr "7. 目录与模型存放约定"
mkdir -p "$HOME/rv/models" "$HOME/rv/results" "$HOME/rv/docs"
ok "已建 ~/rv/{models,results,docs}"

cat <<'EOF'
  模型放置（从比赛资料包解压，别去外网重下）：
    ref/cases/asr-llm-tts.zip  -> src/asr/models/, src/tts/models/
    ref/cases/voice-control-servo.zip -> spacemit_audio/, spacemit_llm/
    YOLOv8n onnx               -> ~/rv/models/yolov8n.onnx
  ollama 模型（官方脚本会自动拉）：
    bash voice-control-servo/spacemit_audio/ollama.sh
    # 得到 qwen2.5:0.5b / qwen2.5-0.5b-elephant-fc（function calling 用后者）
EOF

# -----------------------------------------------------------------------------
hdr "8. 下一步（按顺序跑）"
cat <<EOF
  1) 重新登录（audio 组生效），然后：
       source $VENV/bin/activate
       pip install -r ~/voice-control-servo/spacemit_audio/requirements.txt   # ASR/音频全套依赖
  2) 舵机单轴验证：
       python ~/voice-control-servo/steer.py         # 输入 -1 / 0 / 1
  3) 麦克风录音：
       arecord -D plughw:1,0 -f S16_LE -r 48000 -c 1 -d 3 /tmp/t.wav && aplay /tmp/t.wav
  4) 摄像头：
       v4l2-ctl --list-devices
  5) 视觉 provider 自检：
       python -c "import onnxruntime as o;print(o.get_available_providers())"
     必须能看到 spacemit 相关 provider，否则 AI 核那 6 分要另想办法。
EOF
hdr "完成"
