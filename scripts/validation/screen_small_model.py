"""Avoid a long domain replay if both small-model resolutions fail screening."""
import json
import os
from pathlib import Path
import signal
import time
from score_presence import evaluate

root = Path('/home/biandu/rv/validation_20261103')
labels = {x['qid']: x for x in json.loads((root/'presence_labels.json').read_text())}
deadline = time.monotonic() + 14400
while time.monotonic() < deadline:
    p = root/'domain/progress.json'
    state = json.loads(p.read_text()) if p.exists() else {}
    if state.get('status') == 'schedule_finished':
        break
    if state.get('phase') == 'q08_selected_relations':
        scores = []
        for name in ['q08_runtime017_control','q08_runtime023_control','q08_selected_768']:
            file = root/'domain'/name/'responses.jsonl'
            rows = [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []
            score = evaluate(rows, labels, indoor_only=True)
            scores.append({'phase':name,**score})
        best = max((x['accuracy'] or 0) for x in scores)
        if best >= 0.75:
            break
        pid = int((root/'domain_runner.pid').read_text())
        cmdpath = Path(f'/proc/{pid}/cmdline')
        if not cmdpath.exists() or b'run_board_study_followup.py' not in cmdpath.read_bytes():
            raise RuntimeError('Not the owned followup runner')
        os.kill(pid, signal.SIGINT)
        for _ in range(40):
            if not cmdpath.exists() or not cmdpath.read_bytes():
                break
            time.sleep(1)
        else:
            raise RuntimeError('Runner did not exit; barrier kept closed')
        report = {'status':'schedule_finished','early_stop':'q08_selected_relations',
            'reason':'Small-model fact screening remained below 0.75; avoid redundant long domain replay under time constraint.',
            'screening_not_business_acceptance':True,'controls':scores}
        save = root/'domain/early_stop.json';save.write_text(json.dumps(report,indent=2))
        metadata = root/'domain/q08_selected_relations/metadata.json'
        if metadata.exists():
            data=json.loads(metadata.read_text());data.update(status='screened_out',screening=report)
            metadata.write_text(json.dumps(data,ensure_ascii=False,indent=2))
        temp=root/'domain/progress.screen';temp.write_text(json.dumps(report,indent=2));temp.replace(p)
        print(json.dumps(report),flush=True)
        break
    time.sleep(2)
