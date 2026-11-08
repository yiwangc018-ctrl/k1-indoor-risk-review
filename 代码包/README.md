# K1 风险初评代码包

按主程序、云台、PC 工具分模块保存唯一源码；目录名用于交接，不另行确定比赛正式项目名称。

```text
代码包/
├─ README.md
├─ 管理代码包.py                 # 校验、组装、测试、源码 ZIP
├─ 文件归属.json                 # 源码 → 运行目录映射及历史文件归档索引
├─ K1风险初评主程序/             # app/、configs/、scripts/board/、tests/、依赖
├─ 云台控制模块/                 # gimbal/、四角采集编排、恢复脚本、tests/
├─ PC端部署与报告工具/           # HTML/润色、SSH部署/同步/验收、tests/、依赖
├─ K1主程序开发文档.md
├─ 云台控制开发文档.md
├─ 视觉评估与报告生成文档.md
├─ PC端工具开发文档.md
└─ 开源组件与模型说明.md
```

## 从源码到运行目录

在仓库根目录执行（路径含空格时保持引号）：

```bash
python 代码包/管理代码包.py check
python 代码包/管理代码包.py build
python 代码包/管理代码包.py test
python 代码包/管理代码包.py zip
```

默认组装到 `dist/运行目录/`；源码压缩包写到 `dist/K1风险初评代码包-20261108.zip`。
ZIP 内含本目录与根目录 LICENSE，不含历史归档、权重、私钥、录音和现场照片。
也可把 ZIP 解压后，在解压根目录执行同一组命令。

三个模块的 `app/`、`scripts/` 是源码归属目录，不能分别当作独立完整应用启动。
组装器按清单合并到熟悉的 `app/configs/scripts/gimbal/tests` 相对路径，跨模块导入不再依赖中文目录名。
每个运行文件的来源和 SHA256 写入 `runtime_manifest.json`。Python/shell 等文本统一 LF。
不要修改生成目录里的代码；改模块源码后重新 build。

## 当前正式入口

板端运行目录仍为 `/home/biandu/rv/room_review`，低层云台包另部署到 `/home/biandu/rv/gimbal`。

```bash
cd /home/biandu/rv/room_review
bash scripts/board/start_room_review.sh
# 菜单 7：语音→四角度采集→视觉分析→HTML
# 菜单 8：缺提示语音文件/无音箱时，文字提示+回车后说出口令
```

直接运行完整链路：`bash scripts/board/voice_gimbal30.sh`。
交互命令为“可以开始了”，SenseVoice 接受后才移动；先拍完四张并返回，再分析。
“30 秒”指既有记录中的采集与云台返回段，不是包含模型冷启动、语音和报告的总耗时承诺。

## 配置与资源

主配置在主程序 `configs/room_review_k1.json`；云台有效配置在云台模块 `gimbal/config.json`。
PC 侧连接通过 `--host`，公钥认证默认读取 `~/.ssh/id_k1_agent`，私钥不进包。
模型和运行时的地址、校验与安装方法见开发文档。`listen.wav` 是外部提示语资源，本代码包不带既有录音；
在板端 `audio/` 放入该资源后使用正常语音提示，否则用菜单 8。
`make_cues.py` 只生成识别通过后的 `start.wav`。

## 测试和边界

正式测试使用临时组装目录，不连接 K1、不驱动舵机。按模块运行示例：

```bash
python 代码包/管理代码包.py test --module 云台控制模块
```

历史实验的测试随历史源码保存，不计入当前正式测试数量。
本轮工作验证源码整理和离线行为，未在板端重新验收。等级只表示可见区域整理优先程度，
不是事故概率、测距结果、全屋覆盖或风险准确率。
