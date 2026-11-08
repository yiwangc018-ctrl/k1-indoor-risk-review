#!/usr/bin/env python3
"""把 PCA9685 的 pan/tilt 从 0/卸力状态慢速恢复到交接包初始姿态。

用于板子重启、PCA 被 Hardware(cfg)/菜单6 清零后。只写 CH15/CH11，不构造 Hardware。
运行前请托住相机、确认行程内无遮挡。"""
import sys
import json
from pathlib import Path
import time

sys.path.insert(0, "/home/biandu/rv")
from gimbal.i2c import I2CBus
from gimbal.pca9685 import PCA9685

local_cfg = Path(__file__).resolve().parents[2] / "gimbal/config.json"
config_path = local_cfg if local_cfg.is_file() else Path("/home/biandu/rv/gimbal/config.json")
cfg = json.loads(config_path.read_text(encoding="utf-8"))
PAN_CH = cfg["pan"]["channel"]
TILT_CH = cfg["tilt"]["channel"]
PAN_TARGET = cfg["pan"]["initial_us"]
TILT_TARGET = cfg["tilt"]["initial_us"]

bus = I2CBus(cfg["bus"])
pca = PCA9685(bus, cfg["address"], cfg["pwm_hz"])
print("init: pan=%.1f tilt=%.1f" % (pca.read_pulse_us(PAN_CH), pca.read_pulse_us(TILT_CH)), flush=True)

steps = 200
duration = 8.0
for i in range(1, steps + 1):
    pca.set_pulse_us(PAN_CH, PAN_TARGET * i / steps)
    pca.set_pulse_us(TILT_CH, TILT_TARGET * i / steps)
    time.sleep(duration / steps)
time.sleep(0.5)
print("final: pan=%.1f tilt=%.1f" % (pca.read_pulse_us(PAN_CH), pca.read_pulse_us(TILT_CH)), flush=True)
bus.close()
