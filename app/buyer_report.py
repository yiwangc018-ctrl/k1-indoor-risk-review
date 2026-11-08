#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从一轮结果生成买家版 HTML 报告：照片 + 评估 + 问题点。

输入
----
<round>/analysis.json   每视角等级、理由、focus、priority_regions
<round>/meta.json       轮次、照片文件、覆盖说明
<round>/originals/*.jpg 原图（缺失时显示占位）
<round>/polish/         可选：facts.json / facts.sha256 / model_output.json / polish_meta.json
                        有则使用润色后的 summary 与建议句，并核对事实摘要

输出
----
<round>/report.html     自包含（图片内嵌 base64），可直接发给买家或打印。

事实口径
--------
等级、区域、覆盖、照片与视角对应关系全部来自 analysis.json/meta.json；
polish 只提供 summary 与每条重点区域的建议措辞。facts 摘要不一致直接报错，
不会把模型新写的句子当成事实。
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polish_packet

LABELS = {"low": "低", "medium": "中", "high": "高", None: "无法评估"}
CLASSES = {"low": "low", "medium": "medium", "high": "high", None: "unknown"}
STATUS_TEXT = {"complete": "已完成", "partial": "部分完成", "failed": "未完成"}
CST = timezone(timedelta(hours=8))

CSS = """
:root{
  --ink:#242220; --muted:#6f6a60; --line:#e5ded1; --paper:#f6f3ec;
  --card:#ffffff; --accent:#2f5d50; --accent-soft:#e7f0ec;
  --medium:#a86400; --medium-bg:#fff2d6; --low:#2e6b3a; --low-bg:#e3f2e5;
  --high:#b3261e; --high-bg:#fbe3e0;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:16px/1.8 "Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;
}
.page{max-width:980px; margin:0 auto; padding:44px 26px 64px}
.kicker{font-size:13px; letter-spacing:.16em; color:var(--accent); font-weight:700}
h1{font-size:31px; line-height:1.3; margin:8px 0 12px; letter-spacing:.02em}
h2{font-size:21px; margin:46px 0 14px; padding-bottom:8px; border-bottom:1px solid var(--line)}
h3{font-size:18px; margin:0}
h4{font-size:15px; margin:22px 0 8px}
p{margin:0 0 10px}
.meta{display:flex; flex-wrap:wrap; gap:8px 12px; color:var(--muted); font-size:14px}
.chip{background:#fff; border:1px solid var(--line); border-radius:999px; padding:3px 11px}
.hero{
  display:grid; grid-template-columns:150px 1fr; gap:24px; align-items:center;
  background:var(--card); border:1px solid var(--line); border-radius:16px;
  padding:26px 28px; margin-top:26px;
}
.level-card{text-align:center; border-right:1px solid var(--line); padding-right:18px}
.level-word{font-size:54px; font-weight:800; line-height:1.05; color:var(--medium)}
.level-card.low .level-word{color:var(--low)} .level-card.high .level-word{color:var(--high)}
.level-card.unknown .level-word{color:var(--muted)}
.level-caption{font-size:13px; color:var(--muted); letter-spacing:.18em}
.lead{font-size:17px; margin:0 0 8px}
.note{color:var(--muted); font-size:13px; margin:0}
.badge{
  display:inline-block; padding:2px 10px; border-radius:999px;
  font-size:13px; font-weight:700; white-space:nowrap;
}
.badge.medium{color:var(--medium); background:var(--medium-bg)}
.badge.low{color:var(--low); background:var(--low-bg)}
.badge.high{color:var(--high); background:var(--high-bg)}
.badge.unknown{color:var(--muted); background:#eee}
.card{background:var(--card); border:1px solid var(--line); border-radius:14px}
.issue{display:grid; grid-template-columns:320px 1fr; gap:0; overflow:hidden}
.issue + .issue{margin-top:18px}
.issue figure{margin:0; background:#eae4d7; min-height:240px}
.issue figure img{display:block; width:100%; height:100%; object-fit:cover}
.issue figcaption{font-size:12px; color:var(--muted); padding:7px 10px; background:#fff}
.issue-body{padding:22px 24px}
.issue-title{display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px}
.facts{margin:0; display:grid; grid-template-columns:78px 1fr; gap:8px 12px; font-size:15px}
.facts dt{color:var(--muted)}
.facts dd{margin:0}
.empty{color:var(--muted); background:var(--card); border:1px dashed var(--line); border-radius:12px; padding:18px 20px}
.gallery{display:grid; grid-template-columns:1fr 1fr; gap:18px}
.photo-card{overflow:hidden}
.photo-frame{background:#eae4d7; aspect-ratio:4/3; overflow:hidden}
.photo-frame img{display:block; width:100%; height:100%; object-fit:cover}
.photo-card figcaption{padding:13px 15px 15px}
.photo-head{display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:6px}
.photo-no{font-size:14px; font-weight:700}
.view-id{font-size:12px; color:var(--muted); font-weight:400; margin-left:6px}
.reason{font-size:14px; color:#4b4741; margin:0}
.hint{font-size:13px; color:var(--muted); margin-top:10px}
.coverage{margin:0; padding-left:20px}
.coverage li{margin:0 0 7px}
.appendix{margin-top:38px; padding:0 20px}
.appendix summary{cursor:pointer; padding:16px 0; font-weight:700; color:var(--accent)}
.appendix-body{border-top:1px solid var(--line); padding:16px 0 22px; font-size:14px}
.appendix pre{
  white-space:pre-wrap; overflow-wrap:anywhere; background:#f2eee5;
  border:1px solid var(--line); border-radius:8px; padding:10px 12px; font-size:12.5px;
}
.tbl{width:100%; border-collapse:collapse; font-size:14px; margin:4px 0 10px}
.tbl th,.tbl td{text-align:left; border-bottom:1px solid var(--line); padding:6px 8px}
.tbl th{color:var(--muted); font-weight:600}
code{font-family:Consolas,"SFMono-Regular",monospace; font-size:12.5px; overflow-wrap:anywhere}
.foot{margin-top:44px; padding-top:16px; border-top:1px solid var(--line); color:var(--muted); font-size:13px}
.photo-missing{display:flex; align-items:center; justify-content:center; height:100%; min-height:160px; color:var(--muted); font-size:13px; background:#eee8dc}
@media (max-width:760px){
  .page{padding:26px 16px 44px}
  h1{font-size:25px}
  .hero{grid-template-columns:1fr; gap:14px; padding:20px}
  .level-card{border-right:0; border-bottom:1px solid var(--line); padding:0 0 12px}
  .issue{grid-template-columns:1fr}
  .issue figure{min-height:0; aspect-ratio:4/3}
  .gallery{grid-template-columns:1fr}
}
@media print{
  body{background:#fff}
  .page{max-width:100%; padding:0}
  .hero,.card{box-shadow:none}
  .issue,.photo-card{break-inside:avoid}
  .appendix{break-before:page}
  a{color:inherit; text-decoration:none}
}
"""


class ReportError(ValueError):
    pass


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def fmt_time(value):
    if not value:
        return "未记录"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def label(level):
    return LABELS.get(level, "无法评估")


def badge(level):
    return f'<span class="badge {CLASSES.get(level, "unknown")}">{html.escape(label(level))}</span>'


def image_uri(path, max_width, quality, cache):
    key = (str(path), max_width)
    if key in cache:
        return cache[key]
    try:
        from PIL import Image
    except ImportError as exc:
        raise ReportError("需要 Pillow 才能内嵌照片：pip install pillow") from exc
    with Image.open(path) as image:
        image = image.convert("RGB")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        image.thumbnail((max_width, max_width), resample)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
    uri = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    cache[key] = uri
    return uri


def load_polish(round_dir):
    """读润色包并核对摘要；包不完整或不一致时拒绝生成，不回退到旧事实。"""
    round_dir = Path(round_dir)
    packet = round_dir / "polish"
    if not (packet / "facts.json").is_file():
        return None
    facts = load_json(packet / "facts.json")
    stored = (packet / "facts.sha256").read_text(encoding="utf-8").strip()
    current = polish_packet.facts_from_round(round_dir)
    digest = polish_packet.facts_sha256(current)
    if stored != digest:
        raise ReportError("润色包与当前 analysis/meta 不一致：facts 摘要不匹配，请重新 build")
    meta_file = packet / "polish_meta.json"
    if not meta_file.is_file():
        raise ReportError("润色包缺少 polish_meta.json")
    polish_meta = load_json(meta_file)
    if polish_meta.get("facts_sha256") != digest:
        raise ReportError("polish_meta 的事实摘要与 facts 不一致")
    output_file = packet / "model_output.json"
    summary = None
    advice = None
    if output_file.is_file():
        output = load_json(output_file)
        summary = output.get("summary")
        advice = output.get("advice") or []
        if not isinstance(summary, str) or not summary.strip():
            raise ReportError("model_output.json 缺少 summary")
        expected = len(current.get("priority_regions") or [])
        if not isinstance(advice, list) or len(advice) != expected:
            raise ReportError(
                f"model_output.json 的 advice 数量为 {len(advice)}，"
                f"但事实里有 {expected} 个重点区域"
            )
        advice = [str(item).strip() for item in advice]
    return {"facts": current, "summary": summary, "advice": advice,
            "meta": polish_meta, "digest": digest}


def infer_model(round_dir, analysis):
    """从本轮推理元数据里取模型名、分辨率与运行库；没有就留空。"""
    attempt = analysis.get("attempt_directory")
    if not attempt:
        return ""
    path = Path(round_dir) / attempt / "inference/risk_degree/metadata.json"
    if not path.is_file():
        return ""
    spec = (load_json(path) or {}).get("spec") or {}
    parts = []
    source = Path(spec.get("source_config") or "")
    if source.parent.name:
        parts.append(source.parent.name)
    if spec.get("resolution"):
        parts.append(f"{spec['resolution']} 输入")
    binary = str(spec.get("binary") or "")
    if "0.1.7" in binary:
        parts.append("llama.cpp 0.1.7")
    env = str((spec.get("env") or {}).get("LD_LIBRARY_PATH") or "")
    if "2.0.6" in env:
        parts.append("ORT 2.0.6")
    return " · ".join(parts)


def build(round_dir, out_path=None, title="本轮室内环境风险初评"):
    round_dir = Path(round_dir)
    analysis = load_json(round_dir / "analysis.json")
    meta = load_json(round_dir / "meta.json")
    polish = load_polish(round_dir)

    views = analysis.get("views") or []
    meta_views = {v.get("view_id"): v for v in (meta.get("views") or [])}
    overall = analysis.get("overall_level")
    assessed = analysis.get("assessed_views")
    total = analysis.get("input_views")
    status = analysis.get("status")
    scope = analysis.get("scope") or "本轮可见区域的模型初评；等级为整理优先程度，不是事故概率。"
    regions = analysis.get("priority_regions") or []
    index_of = {v.get("view_id"): i + 1 for i, v in enumerate(views)}

    def photo_path(view_id):
        entry = meta_views.get(view_id) or {}
        relative = entry.get("file") or f"originals/{view_id}.jpg"
        path = (round_dir / relative).resolve()
        if path.is_file():
            return path
        fallback = round_dir / "originals" / f"{view_id}.jpg"
        return fallback if fallback.is_file() else None

    cache = {}

    def photo_block(view_id, max_width=560, quality=82):
        path = photo_path(view_id)
        if path is None:
            return '<div class="photo-missing">照片缺失</div>'
        return f'<img src="{image_uri(path, max_width, quality, cache)}" alt="照片 {index_of.get(view_id, "?")}">'

    summary = (polish or {}).get("summary")
    if not summary:
        if overall:
            summary = (f"本次检查共 {total} 张照片，其中 {assessed} 张获得有效初评，"
                       f"整体关注程度为「{label(overall)}」。")
        else:
            summary = "本轮未能形成有效的整体评估，请查看下方逐张记录。"
    advice = (polish or {}).get("advice") or [
        (region.get("suggestion") or "").strip() for region in regions
    ]

    # 事实锁的文本层：润色/模型输出只能改措辞，不得改等级，也不得把部分完成写成完整。
    allowed_levels = {overall} | {v.get("level") for v in views
                                  if v.get("usable") and v.get("level") in LABELS}
    allowed_levels.discard(None)
    claimed = polish_packet._level_claims(summary + "\n" + "\n".join(str(a) for a in advice))
    conflicts = claimed - allowed_levels
    if conflicts:
        names = "、".join(LABELS[level] for level in sorted(conflicts))
        raise ReportError(f"润色/模型文字出现与事实冲突的等级：{names}；事实只允许 {sorted(allowed_levels)}")
    if status in ("partial", "failed") and polish_packet._COMPLETE_CLAIMS.search(
            summary + "\n" + "\n".join(str(a) for a in advice)):
        raise ReportError("本轮是部分完成/失败，报告文字不得写“完整/全部完成/覆盖”")

    issue_html = []
    for i, region in enumerate(regions):
        view_id = region.get("view_id")
        where = html.escape(region.get("where") or "可见区域")
        basis = html.escape(region.get("basis") or "未提供")
        suggestion = html.escape((advice[i] if i < len(advice) else region.get("suggestion")) or "建议优先整理该区域，留出日常活动空间。")
        number = index_of.get(view_id, "?")
        issue_html.append(f"""
<article class="issue card">
  <figure>{photo_block(view_id)}<figcaption>照片 {number} · {html.escape(str(view_id or ""))}</figcaption></figure>
  <div class="issue-body">
    <div class="issue-title"><h3>{where}</h3>{badge(region.get("level"))}</div>
    <dl class="facts">
      <dt>对应照片</dt><dd>照片 {number}</dd>
      <dt>现象</dt><dd>{basis}</dd>
      <dt>建议</dt><dd>{suggestion}</dd>
    </dl>
  </div>
</article>""")
    if not issue_html:
        attention = [(index + 1, view) for index, view in enumerate(views)
                     if view.get("usable") and view.get("level") in ("medium", "high")]
        if attention:
            photos = "、".join("照片 %d" % number for number, _ in attention)
            levels = "、".join(sorted({label(view.get("level")) for _, view in attention}))
            issue_html.append(
                '<p class="empty">本轮模型没有给出可定位的具体区域；%s 被评估为“%s”，'
                '建议优先查看这些照片的整图评估。</p>' % (photos, levels)
            )
        else:
            issue_html.append('<p class="empty">本轮没有可定位的重点问题，请查看下方的逐张评估。</p>')

    gallery_html = []
    for i, view in enumerate(views, 1):
        view_id = view.get("view_id")
        reason = view.get("reason") if view.get("usable") else (view.get("error") or "无法评估")
        gallery_html.append(f"""
<figure class="photo-card card">
  <div class="photo-frame">{photo_block(view_id, 1200, 84)}</div>
  <figcaption>
    <div class="photo-head"><span class="photo-no">照片 {i}<span class="view-id">{html.escape(str(view_id or ""))}</span></span>{badge(view.get("level"))}</div>
    <p class="reason">{html.escape(str(reason or "无法评估"))}</p>
  </figcaption>
</figure>""")

    coverage_note = html.escape(meta.get("coverage") or "仅本轮照片可见区域")
    capture_mode = str(meta.get("capture_mode") or "")
    pose_sources = {(v.get("pose") or {}).get("source")
                    for v in meta_views.values() if (v.get("pose") or {}).get("source")}
    if "gimbal" in capture_mode or "commanded" in pose_sources or "partial_sensor" in pose_sources:
        camera_note = "照片由云台按固定 pan 角采集，角度为命令值（source=commanded），不是实测绝对角"
    elif capture_mode in ("manual_views", "manual"):
        camera_note = "照片由人工逐张拍摄，无云台角度记录"
    elif capture_mode == "fixed_view":
        camera_note = "照片由固定机位拍摄，无云台角度记录"
    else:
        camera_note = "照片来源见上方覆盖说明"
    appendix_rows = []
    raw_blocks = []
    for i, view in enumerate(views, 1):
        view_id = view.get("view_id")
        elapsed = view.get("elapsed_seconds")
        elapsed_text = f"{elapsed:.2f} 秒" if isinstance(elapsed, (int, float)) else "未记录"
        pose = (meta_views.get(view_id) or {}).get("pose") or {}
        commanded = pose.get("commanded") or {}
        if commanded.get("pan") is not None:
            pan = commanded.get("pan")
            tilt = commanded.get("tilt")
            source = pose.get("source") or "commanded"
            rotation = pose.get("rotation_deg")
            rotation_text = f"，旋转 {rotation}°" if rotation not in (None, 0) else ""
            pose_text = f"pan {pan}° / tilt {tilt}°（{source}{rotation_text}）"
        else:
            pose_text = "未记录"
        appendix_rows.append(
            f"<tr><td>照片 {i}</td><td>{html.escape(str(view_id or ''))}</td>"
            f"<td>{html.escape(label(view.get('level')))}</td><td>{elapsed_text}</td>"
            f"<td>{html.escape(pose_text)}</td></tr>"
        )
        raw = view.get("raw_text") or view.get("reason") or ""
        raw_blocks.append(
            f"<p><b>照片 {i}（{html.escape(str(view_id or ''))}）</b></p>"
            f"<pre>{html.escape(str(raw))}</pre>"
        )

    model_text = infer_model(round_dir, analysis) or "K1 本地视觉模型（元数据缺失）"
    polish_meta = (polish or {}).get("meta") or {}
    polish_tool = polish_meta.get("tool") or "未记录"
    polish_time = fmt_time(polish_meta.get("ts"))
    digest = (polish or {}).get("digest") or ""
    created = fmt_time(meta.get("created_at"))
    generated = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    round_id = html.escape(str(meta.get("round_id") or round_dir.name))
    status_text = STATUS_TEXT.get(status, str(status))
    overall_label = html.escape(label(overall))
    overall_class = CLASSES.get(overall, "unknown")

    facts_line = (f'<p><b>事实摘要</b> <code>{html.escape(digest)}</code></p>'
                  if digest else "")
    appendix = f"""
<details class="appendix card">
  <summary>技术附录：模型、耗时与原始回答</summary>
  <div class="appendix-body">
    <p><b>轮次</b> {round_id}　<b>检查时间</b> {html.escape(created)}　<b>生成时间</b> {generated}</p>
    <p><b>视觉模型</b> {html.escape(model_text)}</p>
    <p><b>文字润色</b> {html.escape(polish_time)} · <code>{html.escape(str(polish_tool))}</code></p>
    {facts_line}
    <h4>各视角记录</h4>
    <table class="tbl"><thead><tr><th>照片</th><th>编号</th><th>等级</th><th>耗时</th><th>角度（命令值）</th></tr></thead>
    <tbody>{''.join(appendix_rows)}</tbody></table>
    <h4>模型原始回答（未改写）</h4>
    {''.join(raw_blocks)}
    <h4>方法与限制</h4>
    <p>{total} 张照片逐张调用视觉模型，获得每张的关注等级与理由；整体等级取有效视角中的最高等级，不是平均值。重点区域与建议来自结构化字段。</p>
    <p>PC 端润色只改写总体说明与建议措辞；等级、区域、覆盖、各视角原文由脚本从事实文件原样保留。{html.escape(scope)}</p>
    <p>本报告只评价这 {total} 张照片的可见区域；{html.escape(camera_note)}，不宣称覆盖全部房间，也不是事故概率、安全认证或验收结论。</p>
  </div>
</details>"""

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · {round_id}</title>
<style>{CSS}</style>
</head>
<body>
<main class="page">
  <header>
    <div class="kicker">室内环境风险初评 · 初步评估</div>
    <h1>{html.escape(title)}</h1>
    <div class="meta">
      <span class="chip">{html.escape(created)}</span>
      <span class="chip">{total} 张照片</span>
      <span class="chip">关注程度 {overall_label}</span>
      <span class="chip">状态：{html.escape(status_text)}</span>
    </div>
  </header>

  <section class="hero">
    <div class="level-card {overall_class}">
      <div class="level-word">{overall_label}</div>
      <div class="level-caption">关注程度</div>
    </div>
    <div>
      <p class="lead">{html.escape(summary)}</p>
      <p class="note">等级表示整理优先程度，不是事故概率或安全认证。</p>
    </div>
  </section>

  <h2>优先处理的问题</h2>
  <p class="hint">问题点来自模型明确指出的区域；其余照片的逐张评估见下一节。</p>
  {''.join(issue_html)}

  <h2>全部照片与逐张评估</h2>
  <div class="gallery">
    {''.join(gallery_html)}
  </div>
  <p class="hint">逐张文字为模型评估原文，未改写。多张照片可能拍到同一区域；本轮未做跨照片同一物体确认，问题点以文字方位为准。</p>

  <h2>覆盖与说明</h2>
  <ul class="coverage">
    <li><b>有效照片</b>：{assessed}/{total} 张获得有效初评。</li>
    <li><b>照片来源</b>：{coverage_note}。</li>
    <li><b>覆盖范围</b>：仅这 {total} 张照片的可见区域；不宣称覆盖全部房间。</li>
    <li><b>结果用途</b>：整理优先级参考，不是事故概率、安全认证或验收结论。</li>
  </ul>

  {appendix}

  <footer class="foot">
    <p>本报告由 K1 端视觉模型逐张初评、程序汇总，经 PC 端润色生成；等级、区域、覆盖与各视角原文未改动。</p>
    <p>报告仅用于本轮照片可见区域的整理参考。</p>
  </footer>
</main>
</body>
</html>
"""

    out_path = Path(out_path) if out_path else round_dir / "report.html"
    out_path.write_text(document, encoding="utf-8")
    return out_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成买家版 HTML 报告：照片 + 评估 + 问题点")
    parser.add_argument("round", type=Path, help="轮次目录（含 analysis.json / meta.json / originals）")
    parser.add_argument("--out", type=Path, help="输出路径，默认 <round>/report.html")
    parser.add_argument("--title", default="本轮室内环境风险初评")
    args = parser.parse_args(argv)
    try:
        out = build(args.round, args.out, args.title)
    except (ReportError, OSError, ValueError) as exc:
        print(f"错误：{exc}")
        return 2
    print(f"已生成：{out}（{out.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
