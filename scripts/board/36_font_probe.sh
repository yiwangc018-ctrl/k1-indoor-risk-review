#!/usr/bin/env bash
# 字体/渲染能力探针
set -u
echo '--- Pillow ---'
python3 -c 'import PIL; print("Pillow", PIL.__version__)' 2>&1 | tail -1

echo '--- 系统信息 ---'
. /etc/os-release 2>/dev/null && echo "  $PRETTY_NAME"
echo "  arch=$(uname -m)"

echo '--- fc-list 中文字体 ---'
if command -v fc-list >/dev/null 2>&1; then
  fc-list :lang=zh 2>/dev/null | head -5
  echo "  (共 $(fc-list :lang=zh 2>/dev/null | wc -l) 条)"
else
  echo '  fc-list 不可用（fontconfig 没装）'
fi

echo '--- 常见字体目录 ---'
for d in /usr/share/fonts/truetype /usr/share/fonts/opentype /usr/share/fonts; do
  [ -d "$d" ] && { echo "  $d:"; ls "$d" 2>/dev/null | head -6 | sed 's/^/    /'; }
done

echo '--- 候选 CJK 字体文件 ---'
find /usr/share/fonts -iname '*.tt[fc]' -o -iname '*.otf' 2>/dev/null | grep -iE 'wqy|noto.*cjk|droid|source|zenhei|microhei|unifont' | head -8 \
  || echo '  没找到'

echo '--- freetype 库 ---'
ls /usr/lib/riscv64-linux-gnu/libfreetype.so* 2>/dev/null | head -3 || echo '  没有 libfreetype'
