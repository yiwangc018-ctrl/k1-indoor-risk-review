#!/usr/bin/env bash
# 01_build_llamacpp.sh —— 在 K1 板上原生编译 spacemit-com/llama.cpp（赛后题2 用）
# 依据: 官方 docs/build-riscv64-spacemit.md（已存 ref/llamacpp_build_spacemit.md）
# 区别: 板上有原生 riscv64 gcc，不需要交叉工具链和 RISCV_ROOT_PATH
# 用法: bash 01_build_llamacpp.sh [安装目录]   默认 ~/llama
set -euo pipefail

PREFIX="${1:-$HOME/llama}"
REPO="https://github.com/spacemit-com/llama.cpp.git"
BRANCH="${LLAMA_BRANCH:-mtmd-backend}"
SPERT_VER="${SPERT_VER:-0.6.3}"
SPERT_URL="https://github.com/spacemit-com/spine-runtime/releases/download/${SPERT_VER}/spine-runtime.riscv64.${SPERT_VER}.tar.gz"

echo "== 0/5 环境检查"
[ "$(uname -m)" = "riscv64" ] || { echo "❌ 不是 riscv64 原生环境；请改用交叉编译文档或云实例"; exit 1; }
command -v cmake >/dev/null || { echo "❌ 缺 cmake: sudo apt install -y cmake build-essential git"; exit 1; }
mkdir -p "$PREFIX"; cd "$PREFIX"

echo "== 1/5 拉代码 (branch=$BRANCH)"
if [ -d llama.cpp/.git ]; then
  git -C llama.cpp fetch --all --prune && git -C llama.cpp checkout "$BRANCH" && git -C llama.cpp pull --ff-only
else
  git clone -b "$BRANCH" --depth 1 "$REPO" llama.cpp
fi

echo "== 2/5 取 spine-runtime (SPERT_DIR) —— SpacemiT 后端预编译依赖"
if [ ! -d "spine-runtime.riscv64.${SPERT_VER}" ]; then
  wget -c "$SPERT_URL" -O spine-runtime.tar.gz
  tar xzf spine-runtime.tar.gz
fi
export SPERT_DIR="$PREFIX/spine-runtime.riscv64.${SPERT_VER}"
[ -d "$SPERT_DIR/include" ] && [ -d "$SPERT_DIR/lib" ] || { echo "❌ SPERT_DIR 结构不对: $SPERT_DIR"; ls "$SPERT_DIR"; exit 1; }

echo "== 3/5 配置"
cd llama.cpp
cmake -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_SPACEMIT=ON \
  -DSPERT_DIR="$SPERT_DIR" \
  -DGGML_CPU_REPACK=OFF \
  -DLLAMA_OPENSSL=OFF \
  -DGGML_RVV=ON \
  -DGGML_RV_ZVFH=ON \
  -DGGML_RV_ZFH=ON \
  -DGGML_RV_ZICBOP=ON \
  -DGGML_RV_ZIHINTPAUSE=ON \
  -DGGML_RV_ZBA=ON \
  -DCMAKE_INSTALL_PREFIX="$PWD/build/installed"

echo "== 4/5 编译 (并行 $(nproc))，8 核约 10~25 分钟"
cmake --build build --parallel "$(nproc)" --config Release

echo "== 5/5 安装"
cmake --install build

echo
echo "✅ 完成。产物: $PWD/build/installed/bin/"
ls -1 build/installed/bin/ 2>/dev/null | head -20
echo
echo "下一步:"
echo "  export PATH=$PWD/build/installed/bin:\$PATH"
echo "  llama-cli --version   # 冒烟测试"
echo "  llama-bench --help"
echo
echo "⚠️ 若第 3 步 GGML_SPACEMIT 相关报错，退化为纯 RVV 版本再试："
echo "   cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_RVV=ON -DLLAMA_OPENSSL=OFF"
echo "   （并在报告里如实写明后端差异，别装作跑的是 IME 加速版）"
