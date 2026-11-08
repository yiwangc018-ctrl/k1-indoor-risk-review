# 室内环境风险初评终端（K1 MUSE Pi Pro）

面向居家、宿舍和办公桌面的本地多模态终端。说一句"可以开始了"，K1 板端用 SenseVoice 做本地语音识别，
再驱动 USB 摄像头拍照，Qwen3.5-0.8B 逐张给出低/中/高关注程度和问题点，板端写出事实文件；
PC 端把照片、评估与问题点汇总成一份自包含的 HTML 报告。全程本地推理，不调用云端接口。

- 平台：K1 MUSE Pi Pro / Bianbu 2.3.5 / riscv64 / Python 3.12
- 模型：SenseVoice（ASR）+ Qwen3.5-0.8B（视觉语言，224 输入）
- 外设：UGREEN 1080P USB 摄像头、USB 麦克风、PCA9685 二轴云台、USB 音箱
- 输出：板端 `analysis.json` / `meta.json` / `report.md`，PC 端 `report.html`

关注程度只表示"先处理哪个区域"的优先级，不是事故概率或安全认证；评估范围只限本轮照片可见区域。

## 目录

```text
app/        板端应用：采集、VLM 分析、报告、云台流程、ASR 客户端、买家版报告
configs/    运行配置与 0.8B 提示词、响应 schema
gimbal/     PCA9685 / MPU6050 / 云台控制包
scripts/    板端启动与恢复、PC 部署 / 同步 / 验收
tests/      PC 单测（unittest，不接板）
```

## 硬件与系统

| 项 | 要求 | 说明 |
|---|---|---|
| 板卡 | K1 MUSE Pi Pro（进迭时空），内存 ≥ 8GB | 实测 15–16GiB；0.8B 模型加载需要 ≥7.5GB 可用内存 |
| 系统 | Bianbu 2.3.5 LXQt / riscv64 / Python 3.12.3 / kernel 6.6.63 | 不要换板型镜像，复现前也别升级 Bianbu 大版本 |
| 用户 | `biandu`，家目录 `/home/biandu` | 脚本默认写死这个路径；其他用户用 `new_board_bringup.py --allow-other-home` |
| 摄像头 | 免驱 UVC 1080P（项目用的是 UGREEN） | 板端走 V4L2，`/dev/video*` 要可读写 |
| 云台 | PCA9685 二轴（i2c-4，地址 0x40）：pan=CH15、tilt=CH11 | 初始姿态 CH15 512.7µs / CH11 2055.7µs；旧文档写的 CH0/CH1 会清零 PCA |
| 麦克风 / 音箱 | USB 麦克风 + USB 音箱（Jieli 4c4a:4155） | 音箱缺失时入口自动加 `--no-cue --wait-enter`，按回车后再说"可以开始了" |

云台需要 i2c 权限：`biandu` 加入 `i2c` 组，`/dev/i2c-3`、`/dev/i2c-4` 可读写（`/dev/i2c-3` 接 MPU6050，可选）。

## 依赖与模型权重

PC 侧：

```bash
pip install -r requirements-pc.txt          # paramiko / Pillow / numpy
```

板端：

```bash
pip install -r requirements-board.txt       # numpy / Pillow / PyYAML
sudo apt install python3-spacemit-ort       # onnxruntime，Bianbu 上优先用 apt
```

模型权重不进仓库（`*.gguf`、`*.onnx` 被 `.gitignore` 排除），需要自己下载并按下面的路径放置。
路径在 `configs/room_review_k1.json` 和 `scripts/board/ensure_room_vlm.sh` 里写死，改了要同步改配置。

| 模型 | 下载地址 | 大小 / sha256 | 放置路径 |
|---|---|---|---|
| Qwen3.5-0.8B（文本 GGUF + 224/384/768 视觉 ONNX） | `http://archive.spacemit.com/spacemit-ai/model_zoo/vlm/Qwen3.5-0.8B.tar.gz` | 977,343,463 B（约 977MB）；sha256 `be44a9d2350c0668bbd830cf79d48c6a1ce5d691bb3c6e8e702dcb58a994d2ca` | 解压到 `~/.cache/models/vlm/`，得到 `~/.cache/models/vlm/Qwen3.5-0.8B/` |
| SenseVoice（ASR） | `https://archive.spacemit.com/spacemit-ai/openwebui/sensevoice.tar.gz` | 446,692,718 B；其中 `model_quant_optimized.onnx` 约 240MB；sha256 `3e54c9cb42beef5d37dc38c3cc87248ee6e9615d143112e0fd8afd5b6c73ff35` | 解压到 `~/rv/asr/sensevoice/`，得到 `~/rv/asr/sensevoice/sensevoice/` |

不想手动下载可以直接跑，脚本会下载并校验 sha256：

```bash
cd /home/biandu/rv/room_review
python3 scripts/board/new_board_bringup.py install --only models
# 已有离线包：加 --models-dir <目录>；完全离线：加 --offline
```

另外还需要 llama.cpp 0.1.7 和 spacemit-ort 2.0.6。首次克隆到新板后，执行：

```bash
python3 scripts/board/new_board_bringup.py install --only dirs,runtime,models,cues
```

`runtime` 从官方 GitHub Release 下载并校验 SHA256，解压到
`~/rv/validation_20261103/runtime_archives/`：

| 运行时 | 官方来源 | SHA256 |
|---|---|---|
| llama.cpp 0.1.7 | [下载](https://github.com/spacemit-com/llama.cpp/releases/download/v0.1.7/spacemit-llama.cpp.riscv64.0.1.7.tar.gz) | `2e24e51d77b8bf44d376ce84b388af817f05a924c5ba38698c1d36c4f9d8f8da` |
| ORT 2.0.6 | [下载](https://github.com/spacemit-com/onnxruntime/releases/download/2.0.6/spacemit-ort.riscv64.2.0.6.tar.gz) | `bebcdfb7df6b49eefa3863afcd85a3da2aa83c3ae9252d7d856188c38a70b0e6` |

离线时将下载文件分别命名为 `llama_017.tar.gz`、`ort_206.tar.gz`，使用
`install --only runtime --runtime-dir <目录> --offline`。缺文件或校验失败会报错退出。
`scripts/board/ensure_room_vlm.sh` 只负责拉起常驻 VLM 服务（端口 8099），不负责安装运行时。
权重与运行时按各自的原始许可证使用，仓库不分发。

新板安装的 VNC 仅监听板端回环地址。Windows 先运行
`ssh -N -L 15901:127.0.0.1:5901 biandu@k1.local`，保持终端打开，
再用 TigerVNC 连接 `127.0.0.1::15901`。重新安装 `vnc` 步骤会将旧的局域网直连切换为 SSH 隧道。

## 板端运行

```bash
ssh biandu@k1.local
cd /home/biandu/rv/room_review
bash scripts/board/start_room_review.sh
```

菜单：

1. 单张拍照 → 0.8B → 报告
2. 手动换方向拍四张 → 分析
3. 语音触发 → 单张拍照
4. 处理待分析轮次
5. 语音测试（文字提示 + 回车）
6. 旧云台四角入口，走 `Gimbal/Hardware`，会清零 PCA，已停用
7. 30 秒流程入口：提示语 + 嘀一声 → 四角度拍照与分析（`scripts/board/voice_gimbal30.sh`）
8. 完整检查（无提示音），音箱不可用时用
9. 恢复云台初始姿态，板子重启后 PCA 被清零时先托住相机再跑

也可以直接调用：

```bash
python3 app/room_review.py capture             # 单张
python3 app/room_review.py watch               # 处理 READY 轮次
python3 app/room_review.py audio-check --play  # 音箱自检
python3 app/room_review.py voice               # 语音触发
```

## PC 端生成报告

```bash
# 取回板端轮次
python scripts/pc/sync_room_review.py --host k1.local --out results/room_review_<日期>/board

# 冻结事实，生成 polish/facts.json 与 facts.sha256
python app/polish_packet.py build <round_dir> <round_dir>/polish

# 润色：没有模型时用确定性模板，也可以接 OpenAI 兼容服务
python app/polish_report.py template <round_dir>/polish

# 生成自包含的买家版 HTML
python app/buyer_report.py <round_dir>
```

输出是 `<round_dir>/report.html`。事实锁校验不通过时脚本会直接报错，不要绕过。
`compose`、`template` 和 `run` 都将摘要/建议写到 `polish/model_output.json`，HTML 从同一文件读取。
`run` 的模型原始回复另存为 `polish/model_response.txt`。摘要只能陈述整体等级，
每条建议中的等级只能对应同序重点区域；文本检查仍不能证明模型所述视觉事实正确。

`scripts/pc/watch_report_to_desktop.py` 可以轮询板端轮次，把新生成的 `report.html` 自动复制到 Windows 桌面，
日志在 `%LOCALAPPDATA%\K1ReportWatcher\watcher.log`。

## 测试

```bash
python -m unittest discover -s tests
```

PC 侧 325 项用例，不需要板卡、摄像头、云台或权重。覆盖照片处理与旋转、VLM 输出契约与提示词、
风险等级汇总、报告事实锁与买家版 HTML、云台 30 秒流程的时序计算、轮次状态机、PCA9685 驱动、
提示音与语音模块、离线包与部署脚本的纯函数部分。真实拍照、ASR/VLM 实际耗时、舵机转位、
音箱放音这些必须有板卡才能验。

## 已知限制

- 云台拍照闭环只在 17:36 那一轮人工确认通过。旧入口 `gimbal_scan.py` 用 CH0/CH1 且会清零 PCA，不要用它跑新流程。
- 风险等级只在单个场景上跑过，没有真值，也没有统计准确率，不宣称 ≥90%。
- AI 核与 CPU 占用未测。
- 断电、摄像头断连、舵机异常恢复未验收，只验证过 VNC 重启后的自恢复。
- 四角覆盖约 0–267°，不是全屋覆盖。
- 相机默认 `rotate=0`；最早那个 16:50 的轮次误转了 180°，作为失败记录保留。
- USB 音箱掉线时入口会退回板载输出；板载也不可用就用菜单 8，按回车后再说"可以开始了"。
- 桌面双击 `.desktop` 仍会报"需要文件管理器服务"，用"所有应用程序"或终端入口。

## 许可证

MIT，见 [LICENSE](LICENSE)。
