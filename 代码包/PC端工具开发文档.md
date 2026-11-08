# PC 端工具开发文档

## 源码归属

本模块 `scripts/pc/`：`k1.py` 提供 SSH/SFTP；`deploy_room_review.py` 部署正式文件；
`sync_room_review.py` 同步轮次与日志；`accept_new_board.py` 独立检查板端；
`make_offline_bundle.py` 生成板端镜像式恢复包；`watch_report_to_desktop.py` 监听报告并复制到桌面；
`find-k1.ps1` 为网络发现辅助工具。
`app/` 中 buyer_report/polish_packet/polish_report 属于报告工具，也会按依赖部署到板端。

## 从仓库执行

先组装：

```powershell
python 代码包/管理代码包.py build
Set-Location dist/运行目录
python -m pip install -r requirements-pc.txt
python scripts/pc/deploy_room_review.py --host k1.local
python scripts/pc/accept_new_board.py --host k1.local --smoke
python scripts/pc/sync_room_review.py --host k1.local --out results/本次同步
python scripts/pc/watch_report_to_desktop.py --host k1.local
```

PC 连接明确使用 --host；不要把历史 DHCP 地址当作当前 IP。
`k1.py` 默认用户 biandu，默认私钥 `~/.ssh/id_k1_agent`；可通过环境变量 K1_USER/K1_KEY 调整。
其他专用脚本有各自参数，运行前用 --help 核对。PC 配置示例随模块 `连接配置示例.md` 提供。

部署输入必须是组装后的运行目录。部署器上传 app/configs/板端脚本、板端依赖和 gimbal 包，
不再把旧扫描、RVV/Whisper 构建、实验验证或训练脚本作为正式依赖。
只上传正式文件，不远程删除旧板文件，也不主动驱动电机；如旧板仍有旧入口，需要按文档停用。
组装清单可用于核对来源与内容，不能证明已部署或实机运行。

## 新板与离线恢复

```bash
python scripts/pc/make_offline_bundle.py --out dist/新板恢复包 --zip
```

恢复包扫描组装目录。当前 VLM 与等级解析在 app 内，不再收 scripts/validation。
仅列当前 0.8B 与 SenseVoice 模型；运行时缺失时使用 bringup 的固定官方下载。
已有 runtime tar 可放入组装目录 `results/k1_validation_20261103/runtime_archives/` 后打离线包；
模型通过 --models-dir 指定。`listen.wav` 作为外部资源准备，缺失时用文字提示入口。
缺少必需资源的包不应称为“完全离线可复现”，查看 manifest 的 missing/warnings 并在板端 verify。

SSH 使用现有账户权限；VNC 的新配置只监听回环，使用 SSH 隧道。
本轮未登录板卡修改现有服务。重新安装 VNC 后，原直连需切换为隧道。

## 报告链路

板端 voice_gimbal30 在新轮次分析完成后调用 buyer_report；PC 可以同步轮次后再重新生成。
watch_report_to_desktop 只复制已生成的 HTML，不代替板端分析。
报告内嵌现场照片，不进入公开 GitHub 或源码 ZIP；发送给他人前人工检查隐私内容。

## 测试

test_new_board_kit、test_offline_bundle 验证离线镜像、hash/目标布局与环境解析；
test_buyer_report/polish_* 验证报告展示和事实锁；test_review_regressions 做跨模块空目录部署与锁/报告回归。
测试中的 SFTP 是临时目录替身，成功不代表当前设备、网络或模型已验收。
