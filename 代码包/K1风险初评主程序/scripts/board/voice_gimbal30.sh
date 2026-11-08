#!/bin/bash
# 完整检查入口：提示语 + 嘀一次 → 云台 30 秒四角度 → 拍照/旋转/分析。
# 参数原样传给 gimbal_flow30（例如 --audio x.wav 用于复用录音测试）。
set -u
cd /home/biandu/rv/room_review
# 预热常驻 ASR 与 VLM：ASR 约 30s，VLM 13–16s，和用户看提示/说话重叠。
python3 -c "import sys; sys.path.insert(0,'app'); import asr_client, room_review; asr_client.ensure_service(room_review.load('configs/room_review_k1.json'))" >/dev/null 2>&1 &
bash scripts/board/ensure_room_vlm.sh >/dev/null 2>&1 &
if ! pactl list short sinks 2>/dev/null | grep -q .; then
    echo "没有可用播放设备；改用无提示音模式：按回车后说“可以开始了”。"
    set -- --no-cue --wait-enter "$@"
fi
BEFORE=$(ls -dt rounds/round_* 2>/dev/null | head -1 || true)
status=0
python3 app/gimbal_flow30.py run --voice "$@" || status=$?
AFTER=$(ls -dt rounds/round_* 2>/dev/null | head -1 || true)
if [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ] && [ -f "$AFTER/analysis.json" ]; then
    echo
    echo "生成买家版 HTML..."
    if python3 app/buyer_report.py "$AFTER"; then
        echo "HTML：$AFTER/report.html"
    else
        echo "HTML 生成失败；报告仍可用 $AFTER/report.md"
    fi
fi
if [ -t 0 ] && [ -t 1 ]; then
    echo
    read -r -p "按回车关闭窗口..." _ || true
fi
exit $status
