"""K1 双轴云台控制包。

对外只需要记住四个入口：
    Hardware     探测 I2C / PCA9685 / MPU6050
    Gimbal       运动 + 俯仰闭环 + 采图门禁
    run_scan     三环全向扫描（capture_fn 注入）
    parse / Dispatcher   语音指令 → 动作

典型用法：
    from gimbal.hw import Hardware
    from gimbal.control import Gimbal
    from gimbal.scan import run_scan
    import json

    cfg = json.load(open("gimbal/config.json", encoding="utf-8"))
    hw = Hardware(cfg)
    g = Gimbal(cfg, hw.backend, hw.mpu)
    g.start()
    g.goto(pan=90, tilt=0)
    g.tilt_to_pitch(-30.0)
    run_scan(g, capture_fn=my_capture)
    g.stop()
"""

__version__ = "1.0.0"
