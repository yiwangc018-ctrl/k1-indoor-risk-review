"""扫描协议：默认"快速四张"（4 方位 × 俯角 30°），可选"三环全向"。

与主流程的接口（很重要，答辩第 3 条就看这里）：
    capture_fn(pose) -> 文件路径或任意结果
这个回调由主程序注入，里面做"取图 + 质量门禁 + 检测"。
扫描器只负责"把相机准确搬到某个位姿并确认可以拍"，不关心图像怎么处理。

两种模式（配置在 config.json -> scan.modes）：
    quick  默认。1 个俯仰 × 4 个方位 = 4 张，tilt = -30°。
           不用语音报角度，上电后说"拍照"就走这一套。
    full   3 环 × 4 方位 = 12 张，tilt = -35 / 0 / +25，用于完整评估。

水平覆盖算术（写进报告，用来回答"为什么 4 个方位就够"）：
    单张水平视场 HFOV。方位角在 [0,270]，4 个位置步进 90°。
    每张覆盖 [az-HFOV/2, az+HFOV/2]（模 360）。
    只要 HFOV > 90°，四个区间的并集就闭合，无盲区。
    OV5647 130°(对角)/≈110°(水平)、GC2093 118°(对角)/≈110°(水平) 都满足：
        覆盖 = 270 + 110 = 380° > 360°，重叠 20°。
    但"有效视场"（去边缘畸变）约 0.7×标称 = 77°，此时四方位之间只剩
    约 13° 的过渡带。结论：四方位用于交互与演示成立；做检测级无盲缝，
    要么步进降到 45°（8 方位），要么在报告里把过渡带标成"低置信区"。

垂直覆盖（quick 模式必须知道，否则报告会写错）：
    tilt=-30°、HFOV=110°、16:9 → 垂直视场约 78°，画面覆盖"仰角 9° ~ 俯角 69°"。
    相机高 1.4 m 时：地面从 0.54 m 开始可见，一直看到墙脚与地平线；
    墙面可见到约 1.6 m 高，天花板与吊灯看不到。
    → quick 模式对"地面/墙脚类"风险（门槛、线缆、水渍、杂物、地毯翘边）够用，
      对"高处"风险不足，说明书里要写清边界。
"""

import json
import math
import os
import time


# ==================== 覆盖算术 ====================
def intervals(azimuths, hfov):
    """把各方位视场展开成 0~360 的并集区间列表。"""
    half = hfov / 2.0
    iv = []
    for a in azimuths:
        lo, hi = a - half, a + half
        iv.append((lo, hi))
    # 按起点排序后合并（允许跨 0 的处理交给调用方看原值）
    iv.sort()
    merged = []
    for lo, hi in iv:
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return [tuple(x) for x in merged]


def coverage_gaps(azimuths, hfov):
    """返回 360° 内未被覆盖的区间，用 [0,360) 表示（自动处理跨越 0 点）。"""
    # 把每个区间拆成落在 [0,360) 内的段
    segs = []
    for lo, hi in intervals(azimuths, hfov):
        a, b = lo % 360.0, hi % 360.0
        if lo < 0:
            segs.append((0.0, b))
            segs.append((a, 360.0))
        elif hi > 360.0:
            segs.append((a, 360.0))
            segs.append((0.0, b))
        else:
            segs.append((a, b))
    segs.sort()
    merged = []
    for lo, hi in segs:
        if not merged or lo > merged[-1][1] + 1e-9:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    gaps = []
    cur = 0.0
    for lo, hi in merged:
        if lo > cur + 1e-9:
            gaps.append((cur, lo))
        cur = max(cur, hi)
    if cur < 360.0 - 1e-9:
        gaps.append((cur, 360.0))
    return gaps


def vfov_from_hfov(hfov_deg, aspect=16.0 / 9.0):
    """水平视场 → 垂直视场（矩形靶面、小孔近似，忽略畸变）。"""
    return 2.0 * math.degrees(
        math.atan(math.tan(math.radians(hfov_deg / 2.0)) / aspect))


def ground_span(cam_height_m, tilt_deg, vfov_deg):
    """给定相机高度、俯仰指令、垂直视场，算画面能看到的范围。

    俯角（向下为正）= -tilt。返回：
        pitch_top / pitch_bottom  画面上/下边缘对应的俯角（正=向下，负=向上）
        near_m / far_m            地面最近/最远可视距离，far_m=None 表示见到地平线
        sees_horizon              地平线是否入画
        wall_top_m                正前方墙面上沿的相对高度（以相机为原点，可为负）
        wall_top_abs_m            该上沿的离地高度；画面里没有墙时为 None
    """
    down = -float(tilt_deg)
    top = down - vfov_deg / 2.0          # 上边缘：俯角较小（可能为负=仰角）
    bottom = down + vfov_deg / 2.0       # 下边缘：俯角较大

    def dist(pitch):
        return cam_height_m / math.tan(math.radians(pitch)) if pitch > 0.5 else None

    near_m = dist(bottom)
    far_m = dist(top)
    if top < 0.0:                        # 上边缘是仰角，画面里有墙
        wall_rel = cam_height_m * math.tan(math.radians(-top))
        wall_abs = cam_height_m + wall_rel
    else:
        wall_rel, wall_abs = 0.0, None   # 从相机往下看，画面里没有墙
    return {
        "pitch_top": round(top, 1),
        "pitch_bottom": round(bottom, 1),
        "near_m": None if near_m is None else round(near_m, 2),
        "far_m": None if far_m is None else round(far_m, 1),
        "sees_horizon": top <= 0.0,
        "wall_top_m": round(wall_rel, 2),
        "wall_top_abs_m": None if wall_abs is None else round(wall_abs, 2),
    }


# ==================== 计划 ====================
def _azimuths(cfg, n, use_presets=True):
    """方位序列。优先取 presets.pan（0/90/180/270），再夹到软限位。"""
    pan = cfg["servos"]["pan"]
    lo = float(pan.get("min_servo_deg", 2.0))
    hi = float(pan.get("max_servo_deg", 267.0))
    presets = [float(x) for x in cfg.get("presets", {}).get("pan", [])]

    if use_presets and len(presets) == n and n > 0:
        return [round(min(max(p, lo), hi), 1) for p in presets], True

    span = hi - lo
    if n <= 1:
        return [round(lo, 1)], False
    return [round(lo + span * i / (n - 1), 1) for i in range(n)], False


def default_plan(cfg, mode=None):
    """生成扫描计划。mode 取 quick / full，缺省用 scan.default_mode（quick）。"""
    sc = cfg.get("scan", {})
    mode = (mode or sc.get("default_mode") or "quick").lower()
    modes = sc.get("modes", {})
    if mode not in modes:
        raise ValueError("未知扫描模式 %r，可选：%s" % (mode, list(modes) or "quick/full"))
    m = modes[mode]

    if "rings_tilt" in m:
        rings = [float(x) for x in m["rings_tilt"]]
    else:
        rings = [float(m.get("tilt", -30.0))]

    n = int(m.get("azimuths", sc.get("azimuths", 4)))
    azimuths, from_presets = _azimuths(cfg, n, bool(m.get("use_presets", True)))

    return {
        "mode": mode,
        "label": m.get("label", mode),
        "rings": rings,
        "azimuths": azimuths,
        "azimuths_from_presets": from_presets,
        "count": len(rings) * len(azimuths),
    }


def plan_summary(cfg, plan):
    """把计划 + 覆盖情况整理成一段可打印、可进报告的文本。"""
    sc = cfg.get("scan", {})
    hfov = float(sc.get("hfov_deg", 110.0))
    aspect = float(sc.get("sensor_aspect", 16.0 / 9.0))
    vfov = vfov_from_hfov(hfov, aspect)
    gaps = coverage_gaps(plan["azimuths"], hfov)

    lines = ["模式=%s（%s）  位姿 %d 个 = %d 环 × %d 方位"
             % (plan["mode"], plan["label"], plan["count"],
                len(plan["rings"]), len(plan["azimuths"])),
             "方位：%s%s" % (plan["azimuths"],
                             "（预设 0/90/180/270，已夹到软限位）"
                             if plan["azimuths_from_presets"] else "（软限位内均分）"),
             "俯仰：%s" % ["%.0f°" % r for r in plan["rings"]]]
    if gaps:
        total = sum(b - a for a, b in gaps)
        lines.append("水平覆盖：有 %.1f° 缺口 %s"
                     % (total, [(round(a, 1), round(b, 1)) for a, b in gaps]))
    else:
        lines.append("水平覆盖：闭合，%d 方位 × %.0f° 视场 = 无盲区"
                     % (len(plan["azimuths"]), hfov))

    # 垂直覆盖只在单环时有意义（多环要取并集，这里只报各环）
    h = float(sc.get("cam_height_m", 1.4))
    for r in plan["rings"]:
        g = ground_span(h, r, vfov)
        if g["near_m"] is None:
            ground = "画面不含地面"
        elif g["far_m"] is None:
            ground = "地面 %.2f m 起到地平线" % g["near_m"]
        else:
            ground = "地面 %.2f~%.1f m" % (g["near_m"], g["far_m"])
        lines.append("  俯仰 %.0f°（垂视场 %.0f°）：仰角 %.0f° ~ 俯角 %.0f°，%s，墙面看到 %s"
                     % (r, vfov, -g["pitch_top"], g["pitch_bottom"], ground,
                        "无墙" if g["wall_top_abs_m"] is None
                        else "%.2f m 高" % g["wall_top_abs_m"]))
    return "\n".join(lines)


# ==================== 执行 ====================
def run_scan(g, capture_fn=None, out_json=None, cfg=None, log=print,
             use_closed_loop=None, max_retry=2, dry_run=False, mode=None,
             tilt=None, azimuths=None):
    """跑一轮扫描。

    mode       quick / full，缺省走 config 里的 default_mode（quick）
    tilt       覆盖模式的俯仰设定（只在单环时有效，多环忽略并提示）
    azimuths   覆盖方位数（例如想拍 8 张就用 8）
    """
    cfg = cfg or g.cfg
    plan = default_plan(cfg, mode)
    if azimuths:
        a, fp = _azimuths(cfg, int(azimuths), True)
        plan["azimuths"], plan["azimuths_from_presets"] = a, fp
        plan["count"] = len(plan["rings"]) * len(a)
    if tilt is not None:
        if len(plan["rings"]) > 1:
            log("[Scan] 多环模式下忽略 tilt=%.0f（每环角度由模式决定）" % tilt)
        else:
            plan["rings"] = [float(tilt)]

    rings = plan["rings"]
    azimuths = plan["azimuths"]
    sc = cfg.get("scan", {})
    extra_settle = float(sc.get("settle_ms", 300)) / 1000.0

    if use_closed_loop is None:
        use_closed_loop = bool(sc.get("tilt_closed_loop", True)) and (g.mpu is not None)

    log("[Scan] " + plan_summary(cfg, plan).replace("\n", "\n[Scan] "))

    lg = g.level_gate()
    if not lg["ok"]:
        log("[Scan] 拒绝开始：%s" % lg["reason"])
        return {"ok": False, "reason": "level_gate", "detail": lg,
                "poses": [], "mode": plan["mode"], "plan": plan}

    manifest = {"ok": True, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": plan["mode"], "label": plan["label"], "plan": plan,
                "closed_loop": use_closed_loop,
                "hfov_deg": sc.get("hfov_deg", 110.0),
                "vfov_deg": round(vfov_from_hfov(
                    float(sc.get("hfov_deg", 110.0)),
                    float(sc.get("sensor_aspect", 16.0 / 9.0))), 1),
                "poses": []}

    try:
        for ri, ring in enumerate(rings):
            log("[Scan] 第 %d/%d 环 tilt=%.0f°" % (ri + 1, len(rings), ring))
            g.goto(tilt=float(ring), blocking=True, timeout=20.0)
            ring_closed = None
            if use_closed_loop:
                r = g.tilt_to_pitch(float(ring), verbose=False)
                ring_closed = {k: r[k] for k in ("ok", "achieved", "error") if k in r}
                if not r["ok"]:
                    log("[Scan] 环 %d 俯仰未收敛：%s" % (ri + 1, r.get("reason")))

            for az in azimuths:
                g.goto(pan=float(az), blocking=True, timeout=20.0)
                pose = {"ring": ri, "tilt_cmd": float(ring), "pan": float(az)}
                if ring_closed is not None:
                    pose["tilt_closed"] = ring_closed

                ok, reasons, tries = False, [], 0
                while tries <= max_retry:
                    time.sleep(extra_settle)
                    ok, reasons = g.capture_gate()
                    if ok:
                        break
                    tries += 1
                    if "水平" in "".join(reasons):
                        break            # 水平问题重拍没用，直接放弃这一环
                    log("[Scan] 位姿 pan=%.0f tilt=%.0f 未通过门禁（%s），重试 %d/%d"
                        % (az, ring, "、".join(reasons), tries, max_retry))

                pose["gate_ok"] = ok
                pose["gate_reasons"] = reasons
                pose["retries"] = tries
                if g.mpu is not None:
                    try:
                        p, r = g.mpu.read_pitch_roll()
                        pose["pitch_meas"] = round(p, 2)
                        pose["roll_meas"] = round(r, 2)
                    except Exception:
                        pass

                if ok and capture_fn is not None and not dry_run:
                    try:
                        pose["result"] = capture_fn(pose)
                    except Exception as e:
                        pose["error"] = str(e)
                        log("[Scan] 采集回调异常：%s" % e)
                manifest["poses"].append(pose)

        hfov = float(sc.get("hfov_deg", 110.0))
        gaps = coverage_gaps(azimuths, hfov)
        manifest["coverage_gaps_deg"] = gaps
        if gaps:
            total = sum(b - a for a, b in gaps)
            log("[Scan] 警告：水平覆盖存在 %.1f° 缺口 %s（步进过大的话把 azimuths 改成 8）"
                % (total, [(round(a, 1), round(b, 1)) for a, b in gaps]))
        else:
            log("[Scan] 水平覆盖闭合：%d 方位 × %.0f° 视场 = 无盲区"
                % (len(azimuths), hfov))

        h = float(sc.get("cam_height_m", 1.4))
        vfov = vfov_from_hfov(hfov, float(sc.get("sensor_aspect", 16.0 / 9.0)))
        manifest["vertical"] = [dict(ground_span(h, r, vfov), tilt=float(r))
                                for r in rings]
    finally:
        if out_json:
            d = os.path.dirname(os.path.abspath(out_json))
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            log("[Scan] 清单已写入 %s（%d 个位姿）" % (out_json, len(manifest["poses"])))
    return manifest
