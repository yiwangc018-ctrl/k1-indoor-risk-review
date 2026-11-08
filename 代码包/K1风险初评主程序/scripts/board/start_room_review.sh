#!/bin/bash
set -eu
cd /home/biandu/rv/room_review
# 预热常驻 ASR：SenseVoice 加载约 30 秒，让它和看菜单的时间重叠。失败不影响其它入口。
python3 -c "import sys; sys.path.insert(0,'app'); import asr_client, room_review; asr_client.ensure_service(room_review.load('configs/room_review_k1.json'))" >/dev/null 2>&1 &
# 预热常驻 VLM：0.8B 启动约 13–16 秒，提前拉起后分析不再付冷启动。
bash scripts/board/ensure_room_vlm.sh >/dev/null 2>&1 &
if [ "$#" -gt 0 ]; then
    exec python3 app/room_review.py "$@"
fi
while true; do
    printf '\n室内可见区域初评\n1. 拍一张并分析\n2. 手动换方向拍四张，再集中分析\n3. 说“可以开始了”，拍一张并分析（先“滴”提示你说，识别通过“嘀嘀”再开拍）\n4. 处理已经拍完的待分析轮次\n5. 语音测试（文字提示 + 回车后才开录，没音箱时用）\n7. 完整检查（语音→云台 30 秒四角度→拍照→分析）\n8. 完整检查（无提示音，按回车；音箱不可用时用）\n9. 恢复云台初始姿态（PCA 被清零/重启后，先托住相机）\n0. 退出\n'
    read -r -p '请选择：' choice
    case "$choice" in
        1) python3 app/room_review.py run || true ;;
        2) python3 app/room_review.py run --views 4 --manual || true ;;
        3) python3 app/room_review.py voice || true ;;
        4) python3 app/room_review.py watch --once || true ;;
        5) python3 app/room_review.py voice --no-cue --wait-enter || true ;;
        7) bash scripts/board/voice_gimbal30.sh || true ;;
        8) bash scripts/board/voice_gimbal30.sh --no-cue --wait-enter || true ;;
        9) python3 scripts/board/restore_gimbal_initial.py || true ;;
        0) exit 0 ;;
        *) printf '请输入 0 至 9。\n' ;;
    esac
done
