#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC 端报告润色执行器：提示词 → 模型 → 按事实模板合成 → 事实锁校验。

与 polish_packet.py 的分工
--------------------------
* polish_packet.build：把 analysis.json / meta.json 的冻结字段抽成 facts.json + sha256；
* polish_packet.validate：检查润色包有没有改事实、有没有漏标注、等级表述是否冲突；
* 本文件补上中间那段：
    1) prompt：生成提示词，模型只能看到冻结事实和机器草稿；
    2) compose：模型只写两个"措辞槽位"（summary、advice[]），
       等级／状态／覆盖／区域／basis 全部由脚本从 facts.json 原样渲染；
    3) template：不调模型的确定性降级稿；
    4) run：调用 OpenAI 兼容服务或 Ollama，把模型输出落盘再合成。

为什么不让模型直接写整篇
------------------------
让模型写整篇报告，事实锁只能靠正则事后拦截，拦不住的表述会漏过去。
这里改成"结构锁"：模型碰不到等级、覆盖、区域这几个字段，
它写的句子只出现在 summary 和第 i 条建议里，位置由脚本固定。

口径见 docs/语音触发与报告润色决策-20261106.md §3。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polish_packet

LEVEL_LABELS = polish_packet.LEVEL_LABELS
SCOPE = "本轮可见区域的模型初评；等级为整理优先程度，不是事故概率。"
DEFAULT_ADVICE = "建议先整理该处，留出日常活动空间。"
SUMMARY_MAX = 400
ADVICE_MAX = 120
FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.S)


class PolishReportError(ValueError):
    """输入不合法、模型输出不合契约、服务不可用。"""


# ---------------------------------------------------------------------------
# 读包与渲染
# ---------------------------------------------------------------------------
def load_packet(packet_dir):
    """读 facts.json，并当场核对它和 facts.sha256 是否一致。"""
    packet_dir = Path(packet_dir)
    facts_file = packet_dir / "facts.json"
    digest_file = packet_dir / "facts.sha256"
    for path in (facts_file, digest_file):
        if not path.is_file():
            raise PolishReportError(f"润色包缺少 {path.name}：先运行 polish_packet.py build")
    facts = json.loads(facts_file.read_text(encoding="utf-8-sig"))
    stored = digest_file.read_text(encoding="utf-8").strip()
    digest = polish_packet.facts_sha256(facts)
    if stored != digest:
        raise PolishReportError("facts.json 与 facts.sha256 不一致：事实文件被改过")
    return facts, digest


def _label(level):
    return LEVEL_LABELS.get(level, "无法评估")


def _sentence(text):
    text = (text or "").strip()
    if not text:
        return ""
    return text if text[-1] in "。！？…；;." else text + "。"


def _one_line(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def render_header(facts):
    """等级、状态、口径由脚本原样渲染，模型输出不参与。"""
    return [
        "# 可见区域风险程度初评（PC 润色稿）",
        "",
        f"状态：{facts.get('status')}；有效照片 {facts.get('assessed_views')}/{facts.get('input_views')}。",
        f"关注程度：**{_label(facts.get('overall_level'))}**。",
        SCOPE,
        "",
    ]


def render_coverage(facts):
    sources = [str(x) for x in (facts.get("pose_source") or []) if x]
    return [
        "## 覆盖",
        "",
        f"- 有效照片：{facts.get('assessed_views')}/{facts.get('input_views')}",
        f"- 覆盖说明：{facts.get('coverage') or '仅本轮照片可见区域'}；不宣称覆盖全部房间。",
        f"- 角度来源：{'、'.join(sources) if sources else '未记录'}",
        f"- 覆盖是否完整：{'是' if facts.get('coverage_complete') else '否'}",
        "",
    ]


def render_regions(facts, advice):
    """重点区域：view_id / where / 等级 / basis 来自事实；只有建议句可被润色。"""
    regions = facts.get("priority_regions") or []
    lines = ["## 重点区域（按关注程度排序）", ""]
    if not regions:
        lines += ["本轮没有可定位的重点区域。", ""]
        return lines
    for index, region in enumerate(regions):
        where = (region.get("where") or "可见区域").strip()
        basis = _sentence(region.get("basis"))
        sentence = _sentence(advice[index] if index < len(advice) else "")
        if not sentence:
            sentence = DEFAULT_ADVICE
        lines.append(
            f"- {region.get('view_id')} · {where}（{_label(region.get('level'))}）："
            f"{basis}{sentence}"
        )
    lines.append("")
    return lines


def validate_slots(facts, model_output):
    """只接受 summary + advice[] 两个槽位；数量必须与重点区域一一对应。"""
    if not isinstance(model_output, dict):
        raise PolishReportError("模型输出必须是 JSON 对象")
    summary = _one_line(model_output.get("summary"))
    if not summary:
        raise PolishReportError("模型输出缺少 summary")
    if len(summary) > SUMMARY_MAX:
        raise PolishReportError(f"summary 超过 {SUMMARY_MAX} 字")
    advice = model_output.get("advice")
    if advice is None:
        advice = []
    if not isinstance(advice, list) or any(not isinstance(item, str) for item in advice):
        raise PolishReportError("advice 必须是字符串数组")
    advice = [_one_line(item) for item in advice]
    expected = len(facts.get("priority_regions") or [])
    if len(advice) != expected:
        raise PolishReportError(
            f"advice 数量为 {len(advice)}，但事实里有 {expected} 个重点区域"
        )
    for index, item in enumerate(advice):
        if not item:
            raise PolishReportError(f"advice[{index}] 是空的")
        if len(item) > ADVICE_MAX:
            raise PolishReportError(f"advice[{index}] 超过 {ADVICE_MAX} 字")
    return summary, advice


def compose_markdown(facts, summary, advice):
    lines = render_header(facts)
    lines += [summary, ""]
    lines += render_coverage(facts)
    lines += render_regions(facts, advice)
    return "\n".join(lines).rstrip() + "\n"


def write_polished(packet_dir, facts, digest, summary, advice, tool, by, source):
    packet_dir = Path(packet_dir)
    (packet_dir / "polished.md").write_text(
        compose_markdown(facts, summary, advice), encoding="utf-8"
    )
    meta = {
        "editor": "tool",
        "by": by,
        "tool": tool,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "facts_sha256": digest,
        "edited_fields": ["narrative", "bullets", "summary", "tone", "grammar", "order"],
        "source": source,
    }
    polish_packet._write_json(packet_dir / "polish_meta.json", meta)
    return meta


# ---------------------------------------------------------------------------
# 提示词与模型调用
# ---------------------------------------------------------------------------
def build_prompt(facts, draft):
    regions = facts.get("priority_regions") or []
    region_lines = []
    for index, region in enumerate(regions):
        region_lines.append(
            f"{index}. {region.get('view_id')} · {region.get('where') or '可见区域'}"
            f"（{_label(region.get('level'))}）：{region.get('basis') or ''}"
        )
    if not region_lines:
        region_lines = ["（本轮没有重点区域，advice 必须是空数组）"]
    facts_text = json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True)
    return f"""你是一个中文报告润色器。只需把措辞改顺，不得改变任何事实。

【硬约束】
1. 只输出一个 JSON 对象，不要输出解释或代码围栏。
2. 不得改等级、区域、覆盖、视角数、角度来源；不得新增或删除区域。
3. 不得写"检测结论""已确认安全""全屋覆盖""完整完成""全部完成"。
4. 不得把"模型初评"写成"检测结论"。
5. summary 用 2–4 句中文，客观概括本轮可见区域的关注程度和优先方向；
   可以提到总体关注程度，但必须与事实一致。
6. advice 数组长度必须等于 {len(regions)}（当前重点区域数），第 i 句对应第 i 个区域；
   只改建议措辞，不得出现新的区域名或与事实不同的等级词。
7. 不要建议补拍、加拍或改变拍摄流程；建议只针对可见区域的整理。
8. 不要自行推断未拍摄的方向或房间；覆盖情况只按事实写。

【重点区域（顺序固定）】
{chr(10).join(region_lines)}

【冻结事实】
{facts_text}

【机器草稿，仅供理解，不要照抄其中的覆盖表述】
{draft}

【输出格式】
{{"summary": "……", "advice": ["……"]}}
"""


def parse_model_output(text):
    """容忍 ```json 围栏；其余情况按错误处理。"""
    stripped = (text or "").strip()
    match = FENCE.match(stripped)
    if match:
        stripped = match.group(1).strip()
    try:
        return json.loads(stripped)
    except ValueError:
        raise PolishReportError("模型输出不是合法 JSON") from None


def _post(url, payload, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise PolishReportError(f"HTTP {exc.code} @ {url} {body}") from None
    except urllib.error.URLError as exc:
        raise PolishReportError(f"连不上 {url}：{getattr(exc, 'reason', exc)}") from None
    except (TimeoutError, OSError) as exc:
        raise PolishReportError(f"请求超时/IO 错误：{exc}") from None
    except json.JSONDecodeError:
        raise PolishReportError(f"返回不是 JSON：{url}") from None


def chat(backend, host, model, prompt, timeout):
    host = host.rstrip("/")
    if backend == "openai":
        data = _post(
            host + "/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 800,
                "stream": False,
            },
            timeout,
        )
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise PolishReportError(f"返回结构不认识：{str(data)[:200]}") from None
    if backend == "ollama":
        data = _post(
            host + "/api/chat",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 800},
            },
            timeout,
        )
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError):
            raise PolishReportError(f"返回结构不认识：{str(data)[:200]}") from None
    raise PolishReportError(f"未知 backend：{backend}")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_prompt(args):
    facts, _ = load_packet(args.packet)
    draft = (Path(args.packet) / "draft.md").read_text(encoding="utf-8")
    prompt = build_prompt(facts, draft)
    target = args.out or Path(args.packet) / "prompt.md"
    target.write_text(prompt, encoding="utf-8")
    print(f"提示词：{target}")
    return 0


def cmd_compose(args):
    facts, digest = load_packet(args.packet)
    raw = Path(args.model_output).read_text(encoding="utf-8-sig")
    model_output = parse_model_output(raw)
    summary, advice = validate_slots(facts, model_output)
    write_polished(args.packet, facts, digest, summary, advice,
                   args.tool, args.by, source=Path(args.model_output).name)
    problems = polish_packet.validate_packet(args.packet)
    if problems:
        for problem in problems:
            print(f"不通过：{problem}")
        print("这稿不得用作报告；draft.md 与 facts.json 保留。")
        return 2
    print(f"通过：{Path(args.packet) / 'polished.md'}")
    return 0


def cmd_template(args):
    facts, digest = load_packet(args.packet)
    label = _label(facts.get("overall_level"))
    summary = (
        f"本轮已分析 {facts.get('assessed_views')}/{facts.get('input_views')} 张照片，"
        f"关注程度为「{label}」。以下为按事实生成的报告，未做语言润色。"
    )
    advice = [DEFAULT_ADVICE] * len(facts.get("priority_regions") or [])
    write_polished(args.packet, facts, digest, summary, advice,
                   args.tool, args.by, source="template")
    problems = polish_packet.validate_packet(args.packet)
    if problems:
        for problem in problems:
            print(f"不通过：{problem}")
        return 2
    print(f"通过：{Path(args.packet) / 'polished.md'}")
    return 0


def cmd_run(args):
    facts, digest = load_packet(args.packet)
    draft = (Path(args.packet) / "draft.md").read_text(encoding="utf-8")
    prompt = build_prompt(facts, draft)
    (Path(args.packet) / "prompt.md").write_text(prompt, encoding="utf-8")
    raw = chat(args.backend, args.host, args.model, prompt, args.timeout)
    output_file = Path(args.packet) / "model_output.json"
    output_file.write_text(raw, encoding="utf-8")
    model_output = parse_model_output(raw)
    summary, advice = validate_slots(facts, model_output)
    write_polished(args.packet, facts, digest, summary, advice,
                   args.tool or f"{args.model}@{args.backend}", args.by,
                   source=output_file.name)
    problems = polish_packet.validate_packet(args.packet)
    if problems:
        for problem in problems:
            print(f"不通过：{problem}")
        print("这稿不得用作报告；draft.md 与 facts.json 保留。")
        return 2
    print(f"通过：{Path(args.packet) / 'polished.md'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="PC 端报告润色：只改措辞，不改事实")
    sub = parser.add_subparsers(dest="command", required=True)

    prompt = sub.add_parser("prompt", help="生成提示词，交给任意模型")
    prompt.add_argument("packet", type=Path)
    prompt.add_argument("--out", type=Path)

    compose = sub.add_parser("compose", help="用模型输出合成 polished.md 并校验")
    compose.add_argument("packet", type=Path)
    compose.add_argument("--model-output", type=Path, required=True)
    compose.add_argument("--tool", default="model", help="模型与版本，写进 polish_meta")
    compose.add_argument("--by", default="pc")

    template = sub.add_parser("template", help="不调模型，生成确定性降级稿")
    template.add_argument("packet", type=Path)
    template.add_argument("--tool", default="template/v1")
    template.add_argument("--by", default="pc")

    run = sub.add_parser("run", help="调用 OpenAI 兼容服务或 Ollama，然后合成")
    run.add_argument("packet", type=Path)
    run.add_argument("--backend", choices=("openai", "ollama"), required=True)
    run.add_argument("--host", default="http://127.0.0.1:11434")
    run.add_argument("--model", required=True)
    run.add_argument("--tool", default="")
    run.add_argument("--by", default="pc")
    run.add_argument("--timeout", type=float, default=180.0)

    args = parser.parse_args(argv)
    handlers = {"prompt": cmd_prompt, "compose": cmd_compose,
                "template": cmd_template, "run": cmd_run}
    try:
        return handlers[args.command](args)
    except PolishReportError as exc:
        print(f"错误：{exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
