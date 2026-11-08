# scripts/vlm_eval —— VLM 能力验证工具

> 配套结论：[docs/视觉评估链路-20261102/](../../docs/视觉评估链路-20261102/README.md)
> 数据：[results/vlm_eval/](../../results/vlm_eval/)

## 流程

```text
select_sun.py     从 SUN RGB-D 选 12 个真实场景（4 卫生间/4 有桌/4 无目标）+ 2 张越界对照
make_candidates.py  从 RASSAR 生成候选缩略图（人工挑图用，可选）
make_blind.py     生成盲测编号 s01..s14（防止评测者看编号猜答案）
run_eval.py       在 K1 上跑主实验（JSON 提示词）
run_eval_b.py     变体 B：开放问句
run_eval_c.py     变体 C：两行自然语言
score.py          评分、写 results_table.csv、画 figure.jpg
```

## 跑一遍

```powershell
# 0) 板上起 VLM 服务
python scripts/pc/k1.py run "bash ~/k1demo/k1_vlm_start.sh"

# 1) 选图（需要 C:\Users\<你>\Downloads\SUNRGBD.zip）
python scripts/vlm_eval/select_sun.py

# 2) 上传图片与脚本
python scripts/pc/k1.py putdir results\vlm_eval\images "~/k1demo/vlm_eval/images"
python scripts/pc/k1.py put scripts\vlm_eval\run_eval.py "~/k1demo/vlm_eval/run_eval.py"

# 3) 推理（14 张约 5 分钟）
python scripts/pc/k1.py run "cd ~/k1demo/vlm_eval && python3 run_eval.py --dir images --out results.json"
python scripts/pc/k1.py get /home/biandu/k1demo/vlm_eval/results.json results\vlm_eval\board_results.json

# 4) 评分 + 画图
python scripts/vlm_eval/score.py --root results/vlm_eval

# 5) 收尾
python scripts/pc/k1.py run "pkill -x llama-server; spacemit-tcm-smi -c"
```

## 注意

- `board_results.json` 里的 `evidence` 是 UTF-8 中文，Windows 控制台直接打印会乱码，用 `read` 或写文件看。
- 换模型（fastvlm / LFM）时只改 `k1_vlm_start.sh` 的 `--model-dir` 和端口，脚本不用动。
- 新增提示词变体：复制 `run_eval_b.py`，改 `PROMPT`，输出新文件名，避免覆盖历史结果。
- `select_sun.py` 里真值口径：SUN RGB-D 标注多边形面积 ≥3% 画面记为 present；改阈值要重跑全部。
