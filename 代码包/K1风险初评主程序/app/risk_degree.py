"""Summarize actual risk-degree responses without treating failures as low."""
import json
import re

LEVELS = {"low": 0, "medium": 1, "high": 2}
LABELS = {"low": "低", "medium": "中", "high": "高", None: "无法评估"}


def parse_coarse(text):
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        match = re.fullmatch(r"\s*```(?:json)?\s*\n?(.*?)\n?```\s*", text or "", re.S)
        if not match:
            return {"usable": False, "error": "invalid JSON"}
        try:
            obj = json.loads(match.group(1))
        except ValueError:
            return {"usable": False, "error": "invalid JSON"}
    if not isinstance(obj, dict) or not isinstance(obj.get("reason"), str) or not obj["reason"].strip():
        return {"usable": False, "error": "missing core fields"}
    if obj.get("status") == "unassessable" and obj.get("level") == "not_assessed":
        return {"usable": False, "error": "model unassessable", "reason": obj["reason"]}
    if obj.get("status") != "assessed" or obj.get("level") not in LEVELS:
        return {"usable": False, "error": "invalid status/level"}
    focus = obj.get("focus", [])
    valid = [x for x in focus if isinstance(x, dict) and x.get("kind") in ("movement", "placement", "collision")
             and all(isinstance(x.get(k), str) and x[k].strip() for k in ("where", "basis"))] if isinstance(focus, list) else []
    return {"usable": True, "level": obj["level"], "reason": obj["reason"], "focus": valid[:1]}


def overall_level(views):
    return max((x["level"] for x in views if x.get("usable")), key=LEVELS.get, default=None)
