#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PolishPacket：把一轮报告的"事实"和"措辞"分开。

为什么要这个模块
---------------
报告要在 PC 上润色（文本模型或人工），但润色绝不能改事实。这个模块只做三件事：

    1) build：从 analysis.json / meta.json 抽出冻结字段，写成 facts.json
    2) 生成 facts.sha256，作为"润色前后事实未变"的凭证
    3) validate：检查润色包——摘要、标注、等级表述冲突

边界
----
* 不联网、不调用模型、不改板上任何文件。
* 只认 analysis.json 里的结构化字段；views[].reason 与 raw_text 是模型原话，永不进入润色对象。
* 校验失败一律返回问题列表，由调用方拒绝该稿并保留 draft.md。

口径见 docs/语音触发与报告润色决策-20261106.md §3。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

SCHEMA = "polish.facts.v1"
PACKET_SCHEMA = "polish.packet.v1"

# 机器初评的等级表述；润色稿里的等级词必须落在这个集合内。
LEVEL_LABELS = {"low": "低", "medium": "中", "high": "高"}

# 只允许改措辞；这些名字写进 polish_meta.edited_fields，方便复盘。
EDITABLE_FIELDS = {"narrative", "bullets", "summary", "tone", "grammar", "order"}

HUMAN_LABEL = "人工后处理"

_LEVEL_CLAIMS = (
    re.compile(r"关注程度\s*[:：]?\s*\**\s*(低|中|高)"),
    re.compile(r"(?<![降])(低|中|高)\s*风险"),
    re.compile(r"风险\s*[:：]?\s*\**\s*(低|中|高)"),
    re.compile(r"等级\s*[:：]?\s*\**\s*(低|中|高)"),
)

# 部分完成 / 失败时，不许把"部分"写成"完整"。
_COMPLETE_CLAIMS = re.compile(r"(完整|全部|全屋|全面)\s*(完成|评估|覆盖|检查)")


class PolishPacketError(ValueError):
    pass


def canonical_json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def facts_sha256(facts):
    return hashlib.sha256(canonical_json(facts).encode("utf-8")).hexdigest()


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def facts_from_round(round_dir):
    """冻结字段：等级、区域、覆盖、视角数与角度来源。措辞不在这里。"""
    round_dir = Path(round_dir)
    analysis = _load(round_dir / "analysis.json")
    meta_file = round_dir / "meta.json"
    meta = _load(meta_file) if meta_file.is_file() else {}

    views = []
    pose_sources = set()
    for view in analysis.get("views", []):
        focus = [{"kind": f.get("kind"), "where": f.get("where"), "basis": f.get("basis")}
                 for f in view.get("focus") or []]
        views.append({"view_id": view.get("view_id"), "usable": bool(view.get("usable")),
                      "level": view.get("level"), "focus": focus})
    for view in meta.get("views") or []:
        pose = (view or {}).get("pose") or {}
        source = pose.get("source") or "none"
        pose_sources.add(source)

    return {
        "schema": SCHEMA,
        "round_id": meta.get("round_id") or round_dir.name,
        "status": analysis.get("status"),
        "overall_level": analysis.get("overall_level"),
        "assessed_views": analysis.get("assessed_views"),
        "input_views": analysis.get("input_views"),
        "coverage_complete": analysis.get("coverage_complete"),
        "coverage": meta.get("coverage"),
        "pose_source": sorted(pose_sources) or None,
        "views": views,
        "priority_regions": [{"view_id": p.get("view_id"), "where": p.get("where"),
                              "basis": p.get("basis"), "level": p.get("level")}
                             for p in analysis.get("priority_regions") or []],
    }


def build_packet(round_dir, packet_dir):
    """生成润色包：facts.json + facts.sha256 + draft.md（机器稿副本，保留原样）。"""
    round_dir, packet_dir = Path(round_dir), Path(packet_dir)
    for name in ("analysis.json", "report.md"):
        if not (round_dir / name).is_file():
            raise PolishPacketError(f"轮次缺少 {name}，不能生成润色包：{round_dir}")
    packet_dir.mkdir(parents=True, exist_ok=True)
    facts = facts_from_round(round_dir)
    digest = facts_sha256(facts)
    _write_json(packet_dir / "facts.json", facts)
    (packet_dir / "facts.sha256").write_text(digest + "\n", encoding="utf-8")
    (packet_dir / "draft.md").write_text((round_dir / "report.md").read_text(encoding="utf-8"),
                                         encoding="utf-8")
    return {"packet": packet_dir, "facts": facts, "sha256": digest}


def _allowed_levels(facts):
    levels = {facts.get("overall_level")}
    levels.update(v.get("level") for v in facts.get("views") or [])
    return {level for level in levels if level in LEVEL_LABELS}


def _level_claims(text):
    claimed = set()
    for pattern in _LEVEL_CLAIMS:
        for match in pattern.finditer(text):
            label = match.group(1)
            for level, name in LEVEL_LABELS.items():
                if name == label:
                    claimed.add(level)
    return claimed


def validate_packet(packet_dir):
    """返回问题列表；空列表表示这稿可以用作报告。不做任何"修复"。"""
    packet_dir = Path(packet_dir)
    problems = []
    for name in ("facts.json", "facts.sha256", "draft.md", "polished.md", "polish_meta.json"):
        if not (packet_dir / name).is_file():
            problems.append(f"缺少 {name}")
    if problems:
        return problems

    facts = _load(packet_dir / "facts.json")
    if facts.get("schema") != SCHEMA:
        problems.append(f"facts.json 的 schema 不是 {SCHEMA}")
    stored = (packet_dir / "facts.sha256").read_text(encoding="utf-8").strip()
    digest = facts_sha256(facts)
    if stored != digest:
        problems.append("facts.json 与 facts.sha256 不一致：事实文件被改过")

    meta = _load(packet_dir / "polish_meta.json")
    if meta.get("facts_sha256") != digest:
        problems.append("polish_meta.facts_sha256 与事实摘要不一致：润色基于别的事实")
    editor = meta.get("editor")
    if editor not in ("human", "tool"):
        problems.append("polish_meta.editor 只能是 human 或 tool")
    if editor == "tool" and not meta.get("tool"):
        problems.append("polish_meta.tool 不能为空（要能追溯到模型与版本）")
    edited = set(meta.get("edited_fields") or [])
    extra = edited - EDITABLE_FIELDS
    if extra:
        problems.append(f"edited_fields 含不可润色的字段：{sorted(extra)}")

    polished = (packet_dir / "polished.md").read_text(encoding="utf-8")
    if not polished.strip():
        problems.append("polished.md 是空的")
    if editor == "human" and HUMAN_LABEL not in polished:
        problems.append(f"人工润色必须在稿中标注“{HUMAN_LABEL}”")

    allowed = _allowed_levels(facts)
    conflicting = _level_claims(polished) - allowed
    if conflicting:
        names = "、".join(LEVEL_LABELS[level] for level in sorted(conflicting))
        problems.append(f"润色稿出现与事实冲突的等级：{names}（事实只允许 {sorted(allowed) or '无等级'}）")

    if facts.get("status") in ("partial", "failed") and _COMPLETE_CLAIMS.search(polished):
        problems.append("本轮是部分完成/失败，润色稿不得出现“完整/全部完成/覆盖”这类表述")

    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="润色包：生成与校验")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="从一轮结果生成润色包")
    build.add_argument("round", type=Path)
    build.add_argument("packet", type=Path)
    check = sub.add_parser("validate", help="校验润色包")
    check.add_argument("packet", type=Path)
    args = parser.parse_args(argv)

    if args.command == "build":
        built = build_packet(args.round, args.packet)
        print(f"润色包：{built['packet']}")
        print(f"事实摘要：{built['sha256']}")
        print("下一步：写 polished.md 与 polish_meta.json（facts_sha256 用上面这个值）")
        return 0

    problems = validate_packet(args.packet)
    if problems:
        for problem in problems:
            print(f"不通过：{problem}")
        return 2
    print("通过：事实未变、标注齐全、等级表述一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
