# 小屏 × 本地大模型（M0–M3 已跑通）

终端问一句 → 本地 `qwen2.5:0.5b`（ollama）→ 屏幕显示数字。

```
ask.py ──► 规则快路径 ──► ollama:11434 ──► 原始文本 ──► 契约层 ──► display.show() ──► i2c-4 ──► 屏
             │无数字→直接 ?
```

**历史记录（2026-10-26，本板；当前环境未复测）**：5/5 正确，全部 `clean`（模型直接吐纯数字），热态 1.0–2.0 s，冷启 ~5 s。该记录不能作为当前板端性能证明。

## 1. 上板 / 更新代码（PC 上执行）

```powershell
python scripts\pc\k1.py putdir app /home/biandu/rv/app
```

> ⚠️ 远端路径必须写绝对路径。PowerShell 7 会把参数里的 `~/rv` 展开成本地
> `C:\Users\<you>/rv` 再交给 exe，SFTP 收到 Windows 路径就 ENOENT（工具里已加护栏）。

## 2. 四条命令

```bash
cd /home/biandu/rv

bash 30_oled_check.sh                        # M0：0x3C 在不在？→ 应在 i2c-4
bash 32_screen_walk.sh 6                     # 全亮→边框→四角→123，每步 6 秒
python3 app/display.py --text 123            # 写死一个数
python3 app/ask.py "输出数字9527"             # 完整闭环
```

依赖（只装过一次）：

```bash
pip3 install --break-system-packages --user smbus2
```

> `~/hwenv` 这个 venv 在本板是坏的（没有 pip / activate），不要用。系统 Python 3.12 自带 pyyaml。

## 3. 配置只有一个文件：`app/config.yaml`

| 想改什么 | 改哪里 |
|---|---|
| **显示模式** | `app.mode: digits`（当前）／`text`（任意中文短句，代码已就绪） |
| 屏型号 | `display.panel: ssd1306` / `sh1106`（auto 命中后用哪套初始化） |
| 总线 / 地址 | `display.buses: [4, 3]`（auto 依次尝试）、`display.address` |
| 屏装反了 | `display.rotate: 180` |
| 换推理后端 | `llm.backend: ollama` / `llamacpp` / `openai` / `mock` |
| 换模型 | `llm.model`（ollama 里 `ollama list` 看有哪些） |
| 超时 | `llm.timeout`（ollama 冷启动要 20 s，别设 3 s） |

`display.driver: auto` 会自己扫 i2c-4/i2c-3 找 0x3C/0x3D，扫不到就降级成 mock 并打印接线提示。

## 4. 三层闸，为什么这样设计

模型是概率采样器，`"输出数字123"` 的合法回答有无限多个；屏幕要的是一帧确定的像素。

| 闸 | 在哪 | 作用 | 实测 |
|---|---|---|---|
| 规则快路径 | `ask.py` 的 `numbers_in(text)` | 话里没数字 → 不调用模型，直接显示 `?` | 省 1.2 s，且防止"你好→1"的瞎编 |
| 解码约束（可选） | `llm.grammar`（仅 llamacpp） | 只允许吐 `[0-9]{1,5}` | 未启用（ollama 不支持） |
| 抽取/校验 | `llm.extract_number()` | 纯数字 → `clean`；句子里捞 → `regex`；捞不到 → `none` | 5/5 都是 `clean` |

**踩过的坑，值得写进报告**：给 0.5B 加"只输出数字"的系统提示后，它对"你好""讲个笑话"
也会硬编一个数字（实测分别输出 `1` 和 `1. 两个和尚在打坐`）。只靠正则拦不住，
必须在调用模型**之前**做规则判定——这正好印证参赛指南 §5.2「规则走快路径，LLM 只做兜底」。

## 5. 脚本清单

| 脚本 | 作用 | 要 sudo？ |
|---|---|---|
| `scripts/pc/k1.py` | PC→板子：`run` / `put` / `putdir` / `get` / `script` | 否 |
| `30_oled_check.sh` | 总线自检 + 失败排查清单 | 否* |
| `31_py_deps.sh` | 装 smbus2（venv 失败则退 `~/.local`） | 否 |
| `32_screen_walk.sh` | 点屏体检（全亮/边框/四角/文字） | 否 |
| `33_init_probe.sh` | SSD1306 vs SH1106 初始化 A/B 试探 | 否 |
| `34_llm_bench.sh` | 5 条指令 + 成功率/延迟统计 | 否 |
| `35_contract_and_latency.sh` | 契约层失败路径 + 长短提示词延迟对比 | 否 |
| `40_fix_i2c_perm.sh` | 装 i2c-tools + `/dev/i2c-*` 改 0666 | **是** |

\* 扫总线本身要能读设备节点；`40_fix_i2c_perm.sh` 跑过一次后就不用 sudo 了。

## 6. 故障对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `i2cdetect` 全 `--` | SCL/SDA 不在同一组 | 都接 I2C-4（`AP_I2C4_SCL/SDA_3V3`）或都接 I2C-3 |
| 有 `3c` 但全黑 | 屏是 SH1106 或对比度低 | `panel: sh1106`，或 `contrast: 255` |
| 整体偏 2 像素 / 右边缺一条 | 1.3″ SH1106 | `panel: sh1106` |
| 文字反了 | 屏装反 | `display.rotate: 180` |
| 屏幕显示 `?` | 话里没数字（规则快路径拦截），或模型没吐数字 | 看终端 `[ask] 判定` 那行 |
| 屏幕显示 `ERR` | 连不上 ollama | `python3 app/ask.py --check` |
| 第一次 5 秒 | 模型冷加载 | 正常；之后 1–2 秒 |

## 7. 接进主工程

```python
from display import open_display
disp = open_display(cfg["display"])   # 硬件有问题自动降级 mock，不会崩
disp.show("90")                       # 角度 / 目标类别 / 状态
```

屏幕是第一个执行器；换成舵机、继电器、TTS，只改最后一层。

---

## 8. 阶段 1 MVP：PC 可验证的指令 → 云台链路

> 只跑 `MockServo`，**不接 GPIO、不接 ASR/CV、不调用 LLM**。真实 `GpioServo`
> 是阶段 2 占位，调用会抛 `NotImplementedError`，避免把“占位”当成“已实现”。

```bash
python app/main.py --mock --once "向左30度"
python app/main.py --mock --once "停"
python app/main.py --mock --once "回中"
python app/main.py --mock --once "打开灯"      # 未知命令：ERROR，不动机，exit 2
python app/main.py --mock                      # 交互模式
python tests/test_mvp.py                       # 阶段 1 单测
```

- 角度约定：`pan` 正=右、负=左；`tilt` 正=上、负=下；软限位默认 pan ±75°、tilt -30°..+60°。
- 可选指标落盘：`--csv results/mvp_events.csv --jsonl results/mvp_events.jsonl`。
- 可导入入口：`from app.main import run_command`（在 `app/` 目录内也可
  `from main import run_command`），签名
  `run_command(text, servo, feedback, metrics)`。
- 状态机：`IDLE → THINK → ACT → FEEDBACK`；未知命令/舵机拒绝进入 `ERROR`。
- 规则命令在 `THINK` 阶段只做字符串解析，不经过 LLM；首响应只含解析 + 一次执行器调用。

---

## 9. 照片处理最小链路（PC，Pillow + numpy）

> 只处理本地图片：读取 → 画质门禁（pass/retry/reject）→ 预处理（旋转/缩放/
> JPEG/PNG）→ average hash 去重 → 边界裁剪 → JSON。**不实现 YOLO，不依赖
> cv2/ultralytics，不读 YAML，不联网，不写 `results/`。**

依赖只有 Pillow 和 numpy（当前环境已装；缺了会给 `DependencyError` 而不是崩溃）：

```powershell
python -c "import PIL, numpy; print(PIL.__version__, numpy.__version__)"
```

### 9.1 命令行

```powershell
# 指定输出目录，图片落到可预期位置；原图不会被覆盖
python app/photo_pipeline.py .\photo1.jpg .\photo2.png --out .\tmp_photos

# 旋转 90°、最长边 1280、裁一块、输出 PNG
python app/photo_pipeline.py .\photo1.jpg --out .\tmp_photos `
  --rotate 90 --max-side 1280 --format PNG --crop 0,0,800,600

# 阈值覆盖：JSON 对象，字段名见 9.3
python app/photo_pipeline.py .\photo1.jpg --out .\tmp_photos `
  --thresholds '{"max_overexposed_ratio": 0.2, "min_gradient_variance": 50}'
```

- `--out` 省略时输出到系统临时目录（结果 JSON 里能看到真实 `output_path`）。
- 输出文件名固定为 `<原名>_processed.jpg|png`；同名已存在会追加 `_1`、`_2`。
- 退出码：所有条目 `ok` 为 0；任一条 `error` 为 1。`reject/retry/duplicate`
  是**可预期的业务结果**，不算崩溃。
- 不传 `--out` 也不传 `--crop` 是最小调用；要留证据就显式指定 `--out`。

### 9.2 Python API

```python
from app.photo_pipeline import (
    run_file_pipeline, results_to_json,
    load_image, quality_check, preprocess, average_hash, crop_box,
)

results = run_file_pipeline(
    ["photo1.jpg", "photo2.png"],
    output_dir="tmp_photos",          # 不覆盖原图；输出到此目录
    image_format="JPEG",              # 或 "PNG"
    rotate=0, max_side=1280,          # 旋转后再限制最长边
    crop_boxes=[(0, 0, 800, 600)],    # 可为每张图记录同一组裁剪框
)
print(results_to_json(results))       # 中文不转义，直接 json.loads 可解析
```

单步：

```python
loaded = load_image("photo.jpg")                 # -> LoadedImage(image, FrameMeta)
q = quality_check(loaded)                        # -> QualityResult(status=pass/retry/reject)
meta = preprocess(loaded, "tmp_photos", image_format="PNG", rotate=90, max_side=1280)
print(meta.original_sha256, meta.processed_sha256, meta.size, meta.rotation, meta.scale)
print(average_hash(loaded.image))                 # 16 位十六进制
cropped, record = crop_box("photo.jpg", (-20, -10, 800, 600))  # 越界自动裁剪
print(record.source_box, record.crop_box, record.clamped)
```

主要数据类：`FrameMeta`（来源/输出尺寸、格式、原始/处理后 sha256、旋转、缩放、
输出路径）、`QualityResult`（状态、metrics、原因、阈值快照）、`CropRecord`
（请求框 `source_box`、实际框 `crop_box`、边界裁剪标记）、`PipelineResult`
（单张图的 status/frame/quality/crops/hash/duplicate_of/error）。`LoadedImage`
只是“解码后的图 + FrameMeta”。

可替换点：`ImageLoader`、`QualityChecker`、`Preprocessor`、`AverageHasher`
四个 Protocol；`run_file_pipeline(..., loader=..., checker=..., preprocessor=...,
hasher=...)` 可直接注入假实现，不修改主流程。

### 9.3 画质阈值与状态

默认值在 `QualityThresholds` 中，全部可用 dict 覆盖（未知字段会报错，避免拼写
错误静默生效）：

| 指标 | 默认阈值 | reject 条件（硬边界） | retry 条件（目标边界） |
|---|---|---|---|
| 尺寸 | 最小 320×240；建议 640×480 | 小于最小尺寸 | 小于建议尺寸 |
| 平均亮度 | 40–220 | < 20 或 > 240（余量 20） | 目标范围外但未到硬边界 |
| 过曝比例（像素 ≥250） | ≤ 0.15 | > 0.25（余量 0.10） | 0.15–0.25 |
| 梯度方差 | ≥ 80 | < 40（余量 40） | 40–80 |

- 尺寸、亮度、过曝、清晰度任一触发 reject → `reject`；否则任一触发目标边界 →
  `retry`；全部通过 → `pass`。
- 清晰度是 **numpy 梯度方差** `var(dI/dx) + var(dI/dy)`，不是 Laplacian；
  它只说明“有没有纹理”，不能替代对焦/模型指标。
- `run_file_pipeline` 对 `reject/retry` 的图只记录画质结果，不做预处理；
  `duplicate` 只记录 `duplicate_of` 和 hash，不重复写文件；单张坏图/缺文件只让
  该条 `status=error`，其它图继续处理。
- `PipelineResult.ok` 表示“本次处理没有异常”，所以 `reject/retry/duplicate`
  也为 `True`；要判断可用性看 `status` / `quality.status`。

### 9.4 测试与边界

```powershell
python tests/test_photo_pipeline.py      # 53 个测试，临时目录现场生成 PIL 图片
python tests/test_mvp.py                 # 38 个测试
python tests/test_llm_regressions.py     # 17 个测试
python tests/test_pipeline.py            # 58 项检查
```

测试覆盖：亮度/过曝/尺寸/梯度清晰度、阈值覆盖与非法阈值、JPEG/PNG、旋转+缩放、
原始/处理后 sha256、不覆盖原图、average hash 同图/不同布局/缩放不变性、汉明距离、
边界裁剪/完全越界/反向框、坏图/空文件/目录/缺文件、缺 Pillow/缺 numpy、多图批处理
继续执行、JSON 序列化、可替换 loader/checker。

边界（不要外推）：

1. **不做** YOLO/检测/分割/风险识别；不加载模型，不访问网络或开发板。
2. 清晰度只是纹理代理，阈值需用真实照片标定；不能宣称“对焦合格”。
3. average hash 只抓“构图近似”的重复，不是语义去重；不同裁剪/大角度旋转可能漏判。
4. 不自动应用 EXIF 方向，也不保留 EXIF；需要时显式 `rotate`。输出尺寸以实际
   解码像素为准。
5. 输出目录省略时落在系统临时目录，不会写 `results/`；长期保存请显式传
   `output_dir` 并自行清理。

---

## 10. 视觉结构契约（`app/vision_contracts.py`）

> 只定义照片链路与 YOLO / 文本 LLM 之间的数据结构、序列化和校验。
> **不实现 YOLO、不加载模型、不下载权重、不调用网络、不读取图片。**
> 它不替代 §9 的照片处理链路，也不把“空检测”包装成“没有风险”。

### 10.1 四个对象

| 对象 | 字段 | 用途 |
|---|---|---|
| `Detection` | `box`（xyxy 原图坐标）、`class_name`、`confidence`、`source`、`image_id` | 单个检测框；confidence 是检测置信度，不是风险概率 |
| `DetectionSet` | `image_id`、`model`、`provider`、`detections`、`latency_ms`、`errors` | 一张图的检测信封；`errors` 非空即降级 |
| `SceneFact` | `category`、`status`、`score`、`evidence_image`、`evidence_box`、`needs_review` | 一条可追溯的场景事实；status 只允许 `present/absent/unknown` |
| `Decision` | `level`、`items`、`degraded`、`provenance` | 规则仲裁结论；level 只允许 `low/medium/high/unknown` |

四个对象都是 frozen dataclass，均提供 `to_dict()` / `from_dict()` /
`to_json()` / `from_json()`。`from_dict()` 遇到未知字段直接报
`VisionContractError`，不会静默丢弃；嵌套的 Detection / SceneFact 也逐个
校验。box 必须正好 4 个有限数值且 `0 <= x1 < x2`、`0 <= y1 < y2`；
confidence / score 必须落在 0..1；`bool` 不会被当成数字；NaN/Infinity
一律拒绝。

```python
from app.vision_contracts import Detection, DetectionSet, SceneFact, Decision

det = Detection(
    box=(804, 590, 1154, 832),   # x1, y1, x2, y2（原图坐标）
    class_name="clutter",
    confidence=0.62,
    source="detector",
    image_id="f-bathroom-p2-b1",
)
detections = DetectionSet(
    image_id="f-bathroom-p2-b1",
    model="yolov8n",
    provider="spacemit-ort",
    detections=[det],
    latency_ms=38.5,
    errors=[],
)
print(detections.to_json())
```

### 10.2 可替换检测器与空实现

`Detector` 是一个只有 `detect(image, image_id, metadata=None)` 的
`Protocol`；真实 YOLO、CPU 兜底、测试 fake detector 都只需满足这个签名。
本文件只提供 `NullDetector`：

```python
from app.vision_contracts import NullDetector, DETECTOR_NOT_CONFIGURED

result = NullDetector().detect(image, "img-1")
assert result.detections == ()
assert result.errors == (DETECTOR_NOT_CONFIGURED,)
```

`NullDetector` 返回空检测 + `DETECTOR_NOT_CONFIGURED`，不读取图片、不伪造
类别/框/置信度，`model` / `provider` 都写 `"null"`。下游看到 `errors` 或
`degraded=True` 时必须按“检测未完成”处理，**不能把空列表解释成“照片里
没有风险物”**。

### 10.3 给文本 Qwen 的纯文本上下文

`build_text_context(detections, metadata)` 返回 JSON **字符串**，其中：

- `modality` 固定 `"text"`、`contains_image` 固定 `False`；
- 只包含 `image_id`、模型/provider、耗时、errors/degraded、检测框和
  metadata，**不含图片、图片路径、base64 或像素数组**；
- metadata 里出现 `image` / `image_path` / `image_base64` / `pixels` /
  `data:image/` 等图片载荷时直接报错，不做静默丢弃；
- `errors` 非空或检测为空时附带 warning，明确“未看到 ≠ 不存在”。

```python
context = build_text_context(detections, {"room": "bathroom", "pose": {"pan_deg": -60}})
# context 是纯文本 JSON；可直接放进文本 Qwen 的 prompt
```

Qwen2.5 是纯文本模型，不接受图像输入；图像事实只能来自 YOLO/CV 检测结果
或独立 VLM。这里传的是检测文本，不是图片。

### 10.4 测试

```powershell
python tests/test_vision_contracts.py   # 60 个测试
```

覆盖：四个对象的 to_dict/from_dict/JSON round-trip、box/confidence/score/
枚举/未知字段/缺失字段/嵌套字段非法输入、`NullDetector` 协议与错误码、
文本上下文无图片字段、metadata 注入图片字段拒绝、空检测 warning。

