#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推理层 + 契约层。

为什么要分两层
--------------
模型是概率采样器：同一句"输出数字123"，它可能回 `123`、`数字是123。`、`1 2 3`。
屏幕要的是**确定的一帧像素**。中间这一层就是把"不确定"收窄成"确定"：

    1) 约束解码（grammar）—— 最强的闸：只允许吐数字，概率上就出不了别的
    2) 提示词      —— 最弱的闸：0.5B 大概七成遵守
    3) 抽取/校验   —— 兜底的闸：正则捞第一个数字，捞不到就承认失败

失败必须可见：返回 (None, "none")，由调用方显示 `?` 并返回非零退出码，
而不是显示一片空白让人以为程序卡死。

零第三方依赖：只用标准库 urllib（板子上不用额外 pip install requests）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULTS = {
    "display": {"driver": "auto", "panel": "ssd1306", "buses": [4, 3], "bus": 4,
                "address": "0x3C", "rotate": 0, "contrast": 207,
                "font": "", "sizes": [24, 20, 16, 12]},
    "llm": {
        "backend": "llamacpp",
        "url": "http://127.0.0.1:8080",
        "model": "local",
        "temperature": 0.0,
        "max_tokens": 8,
        "timeout": 3.0,
        "use_grammar": True,
        "grammar": "root ::= [0-9]{1,5}",
        "system": "你是数字提取器。只输出阿拉伯数字，不要解释、不要标点、不要单位、不要换行。",
        "stop": ["<|im_end|>", "\n"],
    },
    "app": {"mode": "digits", "max_chars": 32, "log": "results/llm_display.jsonl"},
}

CONFIG_NAMES = ("config.yaml", "app/config.yaml")


class ConfigError(RuntimeError):
    """配置文件缺失、依赖缺失或内容不合法。"""


def _deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _find_config(path=None):
    if path is not None:
        candidate = os.fspath(path)
        if not os.path.exists(candidate):
            raise ConfigError("配置文件不存在: %s" % candidate)
        if not os.path.isfile(candidate):
            raise ConfigError("配置路径不是文件: %s" % candidate)
        return candidate
    here = os.path.dirname(os.path.abspath(__file__))
    for name in CONFIG_NAMES:
        for cand in (os.path.join(here, name), os.path.join(here, "..", name)):
            cand = os.path.normpath(cand)
            if os.path.exists(cand):
                return cand
    return None


def load_config(path=None):
    """读 config.yaml，缺的键用 DEFAULTS 补齐。

    显式传入路径时，路径不存在、缺 PyYAML、YAML 解析失败或根节点不是映射都会
    抛 ConfigError，不再静默回退默认值。自动查找配置时，缺 PyYAML 仍保持
    "打印警告 + 使用内置默认值" 的降级行为。
    """
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    explicit = path is not None
    real = _find_config(path)
    if not real:
        return cfg
    try:
        import yaml
    except ImportError as exc:
        if explicit:
            raise ConfigError(
                "显式配置需要 PyYAML: %s (%s)" % (real, exc)) from None
        print("[config] 未装 pyyaml，忽略 %s（用内置默认值）" % real, flush=True)
        return cfg
    try:
        with open(real, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, UnicodeError) as exc:
        raise ConfigError("读取配置失败: %s: %s" % (real, exc)) from None
    except yaml.YAMLError as exc:
        raise ConfigError("YAML 解析失败: %s: %s" % (real, exc)) from None
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是映射: %s" % real)
    return _deep_merge(cfg, data)


# ---------------------------------------------------------------------------
# 契约层：原始文本 → 数字（或承认失败）
# ---------------------------------------------------------------------------
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")
_NUM = re.compile(r"\d{1,5}")


def normalize(raw):
    return (raw or "").translate(_FULLWIDTH).strip()


# ---------------------------------------------------------------------------
# 中文数字
#
# 为什么需要：数字模式靠"话里有没有数字"做规则判定，原来的正则只认 [0-9]，
# 于是"输出数字一百二十三"被判成"没有数字"，屏幕直接显示 ?，用起来像"中文输入没反应"。
# 这里把中文数字统一翻成阿拉伯数字再判定，也让模型万一吐中文数字时能兜住。
# ---------------------------------------------------------------------------
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_RUN = re.compile(r"[〇零一二两三四五六七八九十百千]+")


def cn_to_number(text):
    """中文数字 -> int。支持两种写法：

        逐位：  一二三 -> 123、二零二六 -> 2026
        带单位：十五 -> 15、二十三 -> 23、一百二十三 -> 123、一千零八 -> 1008
    """
    if not text:
        return None
    if not any(ch in _CN_UNITS for ch in text):
        digits = [_CN_DIGITS[ch] for ch in text if ch in _CN_DIGITS]
        return int("".join(str(d) for d in digits)) if digits else None

    total, num = 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
            continue
        unit = _CN_UNITS.get(ch)
        if unit is None:
            continue
        if num == 0:
            num = 1                     # "十五" 的 "十" 前面省略了 1
        if unit == 10:
            total += num * 10
        else:
            total = (total + num) * unit
        num = 0
    return total + num


def extract_number(raw):
    """返回 (值|None, 来源)。来源用于报告里统计"约束生效了多少次"：

    clean —— 模型直接吐了纯数字（grammar 生效）
    regex —— 混在句子里，靠正则捞出来
    none  —— 一个字都没捞到，调用方必须显示失败
    """
    text = normalize(raw)
    if not text:
        return None, "none"
    if re.fullmatch(r"\d{1,5}", text):
        return text, "clean"
    m = _NUM.search(text)
    if m:
        return m.group(0), "regex"
    # 模型也可能吐中文数字（"一百二十三"），一样兜住
    cn = _CN_RUN.search(text)
    if cn:
        value = cn_to_number(cn.group(0))
        if value is not None and 0 <= value <= 99999:
            return str(value), "cn"
    return None, "none"


def numbers_in(text):
    """话里出现的数字（阿拉伯 + 中文），用来做规则判定与"抄错"校验。"""
    t = normalize(text)
    found = _NUM.findall(t)
    for run in _CN_RUN.findall(t):
        value = cn_to_number(run)
        if value is not None:
            found.append(str(value))
    return found


# ---------------------------------------------------------------------------
# 契约层（文本模式）：模型回话 → 屏幕上那几个字
# ---------------------------------------------------------------------------
_QUOTES = "\"'“”‘’`「」『』"
_PREAMBLE = re.compile(r"^(好的|当然|没问题|以下是|这是|显示内容)\s*[：:，,、]?\s*")


def extract_text(raw, max_chars=32):
    """从模型回话里取出"要显示的那几个字"。

    0.5B 的典型毛病：加开场白（"好的，以下是要显示的内容："）、加引号、答完再解释一句。
    这里按"去开场白 → 只取第一行 → 去引号 → 去句末标点 → 超长截断"处理，
    处理不了就返回 None 让调用方显示 ?（失败要看得见）。

    返回 (文字|None, 来源)：clean 原样可用 / clipped 截断过 / empty 没内容。
    """
    t = normalize(raw)
    t = t.strip(_QUOTES + " \t")
    t = _PREAMBLE.sub("", t)
    t = re.split(r"[\r\n]+", t.strip())[0].strip()
    t = t.strip(_QUOTES + " \t")
    t = t.rstrip("。．，,.!！?？~～、")
    t = t.strip(_QUOTES + " \t")
    if not t:
        return None, "empty"
    if len(t) > max_chars:
        return t[:max_chars], "clipped"
    return t, "clean"


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------
QWEN_TEMPLATE = (
    "<|im_start|>system\n{system}<|im_end|>\n"
    "<|im_start|>user\n{user}<|im_end|>\n"
    "<|im_start|>assistant\n"
)


class LLMError(RuntimeError):
    """网络/协议层失败。刻意带一个短原因，方便直接打到屏上或日志里。"""


class Reply:
    __slots__ = ("parsed", "raw", "ms", "backend", "source", "error")

    def __init__(self, parsed=None, raw="", ms=0.0, backend="", source="none", error=None):
        self.parsed = parsed
        self.raw = raw
        self.ms = ms
        self.backend = backend
        self.source = source
        self.error = error

    @property
    def ok(self):
        return self.parsed is not None

    def __repr__(self):
        return "Reply(parsed=%r, source=%r, ms=%.0f, backend=%r, err=%r)" % (
            self.parsed, self.source, self.ms, self.backend, self.error)


class LLM:
    def __init__(self, cfg=None):
        cfg = load_config() if cfg is None else cfg
        self.cfg = cfg
        self.llm = cfg.get("llm", {})
        self.backend = str(self.llm.get("backend", "llamacpp")).lower()
        self.url = str(self.llm.get("url", "")).rstrip("/")
        self.model = self.llm.get("model", "local")
        self.timeout = float(self.llm.get("timeout", 3.0))
        self.system = (self.llm.get("system") or "").strip()
        self.max_tokens = int(self.llm.get("max_tokens", 8))
        self.temperature = float(self.llm.get("temperature", 0.0))
        self.use_grammar = bool(self.llm.get("use_grammar", True))
        self.grammar = str(self.llm.get("grammar") or "").strip()
        self.stop = list(self.llm.get("stop") or [])

    # --- HTTP ---------------------------------------------------------------
    def _post(self, path, payload):
        url = self.url + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise LLMError("HTTP %s @ %s %s" % (exc.code, path, body)) from None
        except urllib.error.URLError as exc:
            raise LLMError("连不上 %s：%s" % (self.url, getattr(exc, "reason", exc))) from None
        except (TimeoutError, OSError) as exc:
            raise LLMError("超时/IO 错误（%.1fs）：%s" % (self.timeout, exc)) from None
        except json.JSONDecodeError:
            raise LLMError("返回不是 JSON（%s）" % path) from None

    def _get(self, path):
        """GET 一个只读接口，返回 (ok, detail)，不把网络异常抛给调用方。"""
        url = self.url + path
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return True, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return False, "HTTP %s @ %s %s" % (exc.code, path, body)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                return False, "超时（%.1fs）：%s" % (self.timeout, reason)
            return False, "连不上 %s：%s" % (self.url, reason)
        except TimeoutError as exc:
            return False, "超时（%.1fs）：%s" % (self.timeout, exc)
        except OSError as exc:
            return False, "IO 错误（%s）：%s" % (type(exc).__name__, exc)
        except Exception as exc:
            # 包括 http.client 的协议异常、解码异常等，诊断信息保留在返回值里。
            return False, "请求失败（%s）：%s" % (type(exc).__name__, exc)

    # --- 各后端 -------------------------------------------------------------
    def _ask_llamacpp(self, user_text):
        payload = {
            "prompt": QWEN_TEMPLATE.format(system=self.system, user=user_text),
            "temperature": self.temperature,
            "n_predict": self.max_tokens,
            "stream": False,
            "cache_prompt": False,
        }
        if self.stop:
            payload["stop"] = self.stop
        if self.use_grammar and self.grammar:
            payload["grammar"] = self.grammar
        data = self._post("/completion", payload)
        return str(data.get("content", ""))

    def _ask_ollama(self, user_text):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }
        data = self._post("/api/chat", payload)
        return str((data.get("message") or {}).get("content", ""))

    def _ask_openai(self, user_text):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": user_text},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        data = self._post("/v1/chat/completions", payload)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise LLMError("返回结构不认识：%s" % str(data)[:200]) from None

    def _ask_mock(self, user_text):
        """本地假回答：把用户话里的数字照抄回来，还要故意加噪音，用来验证正则。"""
        found = numbers_in(user_text)
        if not found:
            return "42"
        n = found[-1]
        style = os.environ.get("MOCK_STYLE", "clean")
        if style == "noisy":
            return "当然可以，数字是%s。" % n
        if style == "garbage":
            return "我不太确定你问的是什么。"
        return n

    # --- 对外 ---------------------------------------------------------------
    def ask(self, user_text):
        """一次完整调用：模型 → 契约层 → Reply。不抛异常，错误关在 Reply.error 里。"""
        t0 = time.perf_counter()
        raw = ""
        try:
            if self.backend == "llamacpp":
                raw = self._ask_llamacpp(user_text)
            elif self.backend == "ollama":
                raw = self._ask_ollama(user_text)
            elif self.backend == "openai":
                raw = self._ask_openai(user_text)
            elif self.backend == "mock":
                raw = self._ask_mock(user_text)
            else:
                raise LLMError("未知 backend=%r（llamacpp/ollama/openai/mock）" % self.backend)
        except LLMError as exc:
            return Reply(ms=(time.perf_counter() - t0) * 1000.0,
                         backend=self.backend, error=str(exc))
        ms = (time.perf_counter() - t0) * 1000.0
        parsed, source = extract_number(raw)
        return Reply(parsed, raw, ms, self.backend, source)

    def check(self):
        """服务可达性 + 已加载模型。给 --check 用，不触发推理。

        网络/超时/HTTP 错误都收敛成 (False, 人可读原因)，绝不向 CLI 抛 traceback。
        """
        try:
            if self.backend == "llamacpp":
                ok, body = self._get("/v1/models")
                return (True, body[:400]) if ok else (False, body)
            if self.backend == "ollama":
                ok, body = self._get("/api/tags")
                return (True, body[:400]) if ok else (False, body)
            if self.backend == "openai":
                ok, body = self._get("/v1/models")
                return (True, body[:400]) if ok else (False, body)
            if self.backend == "mock":
                return True, "mock 后端，不需要服务"
            return False, "未知 backend=%r" % self.backend
        except LLMError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, "检查失败（%s）：%s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# 手测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cfg = load_config()
    print("config:", json.dumps(cfg, ensure_ascii=False, indent=2))
    cli = LLM(cfg)
    ok, detail = cli.check()
    print("check:", ok, detail)
    for probe in ("输出数字123", "随便说点什么"):
        r = cli.ask(probe)
        print(probe, "->", r)
