"""Build an offline index of exact test inputs and saved 2B answers."""
from html import escape
import json
import os
from pathlib import Path
from urllib.parse import quote


ROOT = Path("results/k1_validation_20261103")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def card(title, image, row, reference="", review=""):
    assert image.is_file(), image
    url = quote(Path(os.path.relpath(image, ROOT)).as_posix())
    result = (f'<article class="card"><h3>{escape(title)}</h3>'
        f'<a href="{url}" target="_blank" rel="noopener"><img src="{url}" loading="lazy" alt="{escape(title)}"></a>'
        '<p class="hint">点击图片打开原尺寸文件</p>')
    if reference:
        result += f'<p class="reference">{escape(reference)}</p>'
    result += '<h4>2B 原始回答</h4><pre>' + escape(row["text"]) + '</pre>'
    result += '<details><summary>本次完整提问</summary><p>' + escape(row["prompt"]) + '</p></details>'
    if review:
        result += '<details><summary>实验复核备注（非独立专家真值）</summary><p>' + escape(review) + '</p></details>'
    return result + '</article>'


def main():
    labels = {x["qid"]: x for x in read(Path("results/vlm_eval/labels.json"))}
    basic = {x["image_id"]: x for x in records(ROOT / "presence/q2_384/responses.jsonl") if x["variant"] == "legacy_json"}
    domain = {x["job_id"]: x for x in records(ROOT / "domain/q2_selected_relations/responses.jsonl")}
    mapping = read(ROOT / "domain_blind_map.json")
    reviews = {x["job_id"]: x["review"] for x in read(ROOT / "domain_semantic_review.json")["items"]}
    html = ['''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>测试图片与2B原始回答</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#1c2839;font:16px/1.65 system-ui,"Microsoft YaHei",sans-serif}
header{background:#172a45;color:white;padding:30px max(24px,calc((100vw - 1240px)/2))}h1{margin:0 0 12px;font-size:30px}
header p{margin:8px 0;color:#dce6f6}main{max-width:1290px;margin:auto;padding:20px 24px 60px}nav{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}
nav a{background:white;border:1px solid #d1d9e6;padding:8px 15px;border-radius:7px;color:#204c85;text-decoration:none}
section{margin:35px 0}h2{font-size:24px;margin-bottom:5px}h3{font-size:18px;margin:0 0 14px}h4{font-size:15px;margin:14px 0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}.pair{margin:22px 0}.pairgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.card{background:white;padding:18px;border:1px solid #d9e0e9;border-radius:10px;min-width:0}.card img{width:100%;height:300px;object-fit:contain;background:#e9edf2;border-radius:5px}
.hint{font-size:12px;color:#64748b;margin:5px 0}.reference{font-size:13px;color:#52627a}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f4f8;padding:12px;border-radius:5px;font:14px/1.7 ui-monospace,Consolas,"Microsoft YaHei",monospace}
details{margin-top:12px;font-size:14px}summary{cursor:pointer;color:#234c81}.note{background:#e9eff8;padding:14px 18px;border-left:4px solid #4b6d9b;border-radius:4px}
@media(max-width:720px){.pairgrid{grid-template-columns:1fr}main{padding:15px}.grid{grid-template-columns:1fr}h1{font-size:25px}}
</style><header><h1>测试图片与 2B 原始回答</h1>
<p>14张基础图 + 16张摆放对照图 + 4张局部裁图；桌角检查复用其中6张基础图。</p>
<p>后续选用 Qwen3.5-2B · 384输入。此页保留实验当时的实际回答，不代表所有回答正确。</p></header><main>
<nav><a href="#basic">基础判断 · 14图</a><a href="#placement">摆放对照 · 16图</a><a href="#crops">裁图 · 4图</a><a href="#corners">桌角提问 · 复用6图</a></nav>
<p class="note">可离线浏览，点击照片查看原尺寸。摆放对照来自公开 HomeSafe 编辑数据；作者的 safe/unsafe 标签不是实测物理稳定性，也不代表真实房间风险结论。所有回答均为2B的保存原文，格式与错误没有在此页修正。</p>''']
    html += ['<section id="basic"><h2>基础判断：14张</h2><p>对应2B/384、系统运行时、旧JSON提示的同题基线；不是风险判断题。</p><div class="grid">']
    for qid, row in sorted(basic.items()):
        ref = ("域外控制图：按本次提示应对室内检查填写 unknown。" if qid in ("q13", "q14")
            else f"基础物体参考标注：toilet={labels[qid]['toilet']}，table={labels[qid]['table']}。不是风险等级。")
        html.append(card(qid, Path("results/vlm_eval/images") / (qid + ".jpg"), row, ref))
    html += ['</div></section><section id="placement"><h2>摆放与空间关系：8对、16张</h2><p>对应2B/384、runtime017关系实验。每对来自同一来源场景，不能当作两间独立住宅。</p>']
    titles = {"trip_01": "沙发前地面线缆", "trip_02": "扶手椅前地面线缆", "route_01": "沙发与纸箱遮挡门口",
              "route_02": "床垫与门口通路", "stack_01": "台面厚板与可见突出", "stack_02": "置物架上新增容器",
              "stack_03": "架子边缘工具容器", "stack_04": "柜顶行李堆叠"}
    for prefix, title in titles.items():
        html += [f'<div class="pair"><h3>{escape(title)}</h3><div class="pairgrid">']
        for safe in (True, False):
            ident = next(k for k, v in mapping.items() if v["eval_id"] == prefix + ("_safe" if safe else "_unsafe"))
            label = "safe" if safe else "unsafe"
            job = "relation_" + ident
            html.append(card(f"{ident} · 版本{'A' if safe else 'B'}", ROOT / "eval_images" / (ident + ".jpg"), domain[job],
                f"原数据作者标签：{label}。该标签可能依赖动作或编辑设定，不是本项目物理真值。", reviews[job]))
        html += ['</div></div>']
    html += ['</section><section id="crops"><h2>同一张图的局部裁图：4张</h2><p>用于检查裁图是否改善可见关系描述，不是新增独立场景。</p><div class="grid">']
    for ident in ("d06", "d07", "d11", "d12"):
        job = "crop_" + ident
        html.append(card(ident + " · 局部裁图", ROOT / "eval_images" / (ident + "_crop.jpg"), domain[job],
            "对应完整画面编号：" + ident, reviews[job]))
    html += ['</div></section><section id="corners"><h2>桌沿/桌角提问：复用6张</h2><p>这里复用上面的基础图片。没有已安装护角的正例，不能据此验收护角识别能力。</p><div class="grid">']
    for qid in ("q05", "q06", "q07", "q08", "q13", "q14"):
        job = "corner_" + qid
        html.append(card(qid + " · 桌角提问", Path("results/vlm_eval/images") / (qid + ".jpg"), domain[job], review=reviews[job]))
    html += ['</div></section><p>原始文件位置：eval_images/、../vlm_eval/images/。本页重复展示部分图；共有34个输入图文件，来源场景更少。</p></main></html>']
    out = ROOT / "测试图片与2B回答.html"
    out.write_text("\n".join(html), encoding="utf-8")
    print(str(out.resolve()))


if __name__ == "__main__":
    main()
