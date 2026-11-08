#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小指标层：事件与延迟追加到 CSV / JSONL（只用标准库）。

用法
----
    m = Metrics(csv_path="results/mvp_events.csv",
                jsonl_path="results/mvp_events.jsonl")
    m.event("command", command="向左30度", state="THINK")
    m.latency("first_response", 3.2, state="ACT", command="向左30度")
    m.close()

设计
----
* 两个输出都可选；都不传时只保留内存记录，适合单测。
* CSV 用固定表头，未知字段序列化进 detail 列，避免每行表头漂移。
* JSONL 一行一个对象，字段更自由。
* 追加写；首次写 CSV 时自动补表头；线程锁保护多线程写入。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
from datetime import datetime
from typing import Optional

FIELDS = (
    "ts", "kind", "event", "stage", "ms", "state", "command",
    "action", "axis", "mode", "value", "applied", "clamped",
    "ok", "error", "detail",
)

_NUMERIC = ("ms", "value", "applied")


class Metrics:
    """事件/延迟记录器；csv_path 和 jsonl_path 可单独或同时配置。"""

    def __init__(self, csv_path: Optional[str] = None,
                 jsonl_path: Optional[str] = None,
                 clock=None):
        self.csv_path = os.fspath(csv_path) if csv_path else None
        self.jsonl_path = os.fspath(jsonl_path) if jsonl_path else None
        self.records: list = []
        self.errors: list = []
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._csv_file = None
        self._csv_writer = None
        self._jsonl_file = None
        self._closed = False

    # --- 写入 ---------------------------------------------------------------
    def event(self, name: str, **fields) -> dict:
        """记录一个具名事件，例如 command / action / reject / error。"""
        return self.record(kind="event", event=name, **fields)

    def latency(self, stage: str, ms: float, **fields) -> dict:
        """记录一段耗时（毫秒），例如 parse / first_response / act / total。"""
        return self.record(kind="latency", stage=stage, ms=float(ms), **fields)

    def record(self, kind: str, **fields) -> dict:
        if self._closed:
            raise RuntimeError("Metrics 已关闭")
        row = {name: None for name in FIELDS}
        row["ts"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        row["kind"] = str(kind)
        detail = {}
        explicit_detail = fields.pop("detail", None)
        if isinstance(explicit_detail, dict):
            detail.update(explicit_detail)
        elif explicit_detail:
            detail["detail"] = explicit_detail

        for key, value in fields.items():
            if key in _NUMERIC and value is not None:
                try:
                    value = round(float(value), 3)
                except (TypeError, ValueError):
                    pass
            if key in row and key not in ("ts", "kind", "detail"):
                row[key] = value
            else:
                detail[key] = value

        row["detail"] = json.dumps(detail, ensure_ascii=False) if detail else ""
        with self._lock:
            self.records.append(row)
            try:
                self._write_csv(row)
                self._write_jsonl(row)
            except OSError as exc:
                # 指标写失败不能反过来阻断控制链路；记下来并在 stderr 提示一次。
                message = "%s: %s" % (type(exc).__name__, exc)
                if message not in self.errors:
                    self.errors.append(message)
                    print("[metrics] 写入失败：%s" % exc, file=sys.stderr)
        return dict(row)

    def _write_csv(self, row: dict) -> None:
        if not self.csv_path:
            return
        if self._csv_file is None:
            directory = os.path.dirname(os.path.abspath(self.csv_path))
            os.makedirs(directory, exist_ok=True)
            is_new = not (os.path.exists(self.csv_path)
                          and os.path.getsize(self.csv_path) > 0)
            self._csv_file = open(self.csv_path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(
                self._csv_file, fieldnames=FIELDS, extrasaction="ignore")
            if is_new:
                self._csv_writer.writeheader()
        self._csv_writer.writerow(row)
        self._csv_file.flush()

    def _write_jsonl(self, row: dict) -> None:
        if not self.jsonl_path:
            return
        if self._jsonl_file is None:
            directory = os.path.dirname(os.path.abspath(self.jsonl_path))
            os.makedirs(directory, exist_ok=True)
            self._jsonl_file = open(self.jsonl_path, "a", encoding="utf-8")
        self._jsonl_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._jsonl_file.flush()

    # --- 生命周期 -----------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            if self._csv_file:
                self._csv_file.flush()
            if self._jsonl_file:
                self._jsonl_file.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for handle in (self._csv_file, self._jsonl_file):
                if handle:
                    try:
                        handle.flush()
                    finally:
                        handle.close()
            self._csv_file = None
            self._jsonl_file = None
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def summary(self) -> dict:
        counts = {}
        for row in self.records:
            key = row.get("kind") or "?"
            counts[key] = counts.get(key, 0) + 1
        return {"total": len(self.records), "by_kind": counts}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


__all__ = ["Metrics", "FIELDS"]
