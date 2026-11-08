"""Evaluate local text extraction after VLM studies release their resources."""
import argparse
import json
from pathlib import Path
import time
import urllib.request


def save(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    out = root / "text"
    out.mkdir(exist_ok=True)
    save(out / "progress.json", {"status": "waiting_for_format_study"})
    deadline = time.monotonic() + 18000
    while time.monotonic() < deadline:
        progress = root / "format/progress.json"
        if progress.exists() and json.loads(progress.read_text()).get("status") == "schedule_finished":
            break
        time.sleep(5)
    else:
        save(out / "progress.json", {"status": "blocked", "error": "prior study did not finish"})
        return
    fixtures = [
        ("马桶可见，桌子未见。", "present", "absent"),
        ("未见马桶。可见一张书桌。", "absent", "present"),
        ("看不清是否有马桶；桌子被挡住，无法确认。", "unknown", "unknown"),
        ("这张户外照片不适用于室内检查，两项均无法确认。", "unknown", "unknown"),
        ("照片里有柜子和箱子。观察记录没有提供马桶或桌子的信息。", "unknown", "unknown"),
        ("马桶没有看清，不能说不存在。桌子可见。", "unknown", "present"),
        ("先说有马桶，随后又说没有马桶，两句相互矛盾。桌子没有出现。", "unknown", "absent"),
        ("马桶和桌子都没有在画面中出现。", "absent", "absent"),
        ("马桶可见；至于桌子，观察记录没有提供信息。", "present", "unknown"),
        ("文中只有样例字段present|absent|unknown，没有实际观察结论。", "unknown", "unknown"),
    ]
    tasks = [{"id": f"fixture_{i:02d}", "source": "controlled_text_fixture", "text": text,
        "expected": {"toilet": toilet, "table": table}} for i, (text, toilet, table) in enumerate(fixtures, 1)]
    for phase in ("q08_384", "q2_384"):
        file = root / "presence" / phase / "responses.jsonl"
        if file.exists():
            for line in file.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row["variant"] == "prose" and row.get("ok"):
                    tasks.append({"id": phase + "_" + row["job_id"], "source": "actual_vlm_prose",
                        "image_id": row["image_id"], "text": row["text"], "expected": None,
                        "review_note": "Textual extraction fidelity requires manual source-text review, not image labels."})
    save(out / "inputs.json", tasks)
    schema = {"type": "object", "properties": {key: {"type": "string", "enum": ["present", "absent", "unknown"]}
              for key in ("toilet", "table")}, "required": ["toilet", "table"], "additionalProperties": False}
    system = ("你是观察记录的字段提取器，没有图片。仅提取观察文本的明确结论，禁止新增视觉事实。"
        "马桶可见填present，明确未见填absent，看不清、未提及或前后矛盾填unknown；桌子同理。"
        "不是室内检查且记录无法确认时，两项填unknown。只输出含toilet和table两个字段的JSON对象。")
    for mode in ("prompt_only", "schema_constrained"):
        for index, task in enumerate(tasks):
            save(out / "progress.json", {"status": "running", "mode": mode, "task": task["id"],
                                       "remaining": len(tasks) - index})
            payload = {"model": "qwen2.5:0.5b", "stream": False, "keep_alive": 0,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": "<观察文本>\n" + task["text"] + "\n</观察文本>"}],
                "options": {"temperature": 0, "seed": 42, "num_predict": 96, "num_ctx": 2048, "num_thread": 4}}
            if mode == "schema_constrained":
                payload["format"] = schema
            request = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
            start = time.perf_counter()
            result = {"id": task["id"], "mode": mode, "source": task["source"], "expected": task["expected"]}
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    data = json.load(response)
                result.update(ok=True, response=data, text=data.get("message", {}).get("content", ""))
            except Exception as exc:
                result.update(ok=False, error=type(exc).__name__ + ": " + str(exc))
            result["elapsed_seconds"] = round(time.perf_counter() - start, 3)
            with (out / "responses.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(json.dumps({"mode": mode, "id": task["id"], "seconds": result["elapsed_seconds"],
                              "ok": result["ok"], "text": result.get("text", "")}, ensure_ascii=False), flush=True)
    save(out / "progress.json", {"status": "schedule_finished", "tasks_per_mode": len(tasks)})


if __name__ == "__main__":
    main()
