"""After the 2B/384 baseline, defer high-res repeats to runtime-selected tests."""
import json
import os
from pathlib import Path
import signal
import time

root = Path('/home/biandu/rv/validation_20261103')
deadline = time.monotonic() + 7200
while time.monotonic() < deadline:
    metadata = root / 'presence/q2_384/metadata.json'
    if metadata.exists() and json.loads(metadata.read_text()).get('status') == 'complete':
        break
    time.sleep(2)
else:
    raise TimeoutError('2B/384 baseline did not finish; no process was interrupted')
pid = int((root / 'presence_runner.pid').read_text())
cmdpath = Path(f'/proc/{pid}/cmdline')
cmd = cmdpath.read_bytes().replace(b'\0', b' ') if cmdpath.exists() else b''
if cmd:
    if b'/home/biandu/rv/validation_20261103/run_board_study.py' not in cmd:
        raise RuntimeError('Refusing to signal a process outside this study')
    os.kill(pid, signal.SIGINT)
    for _ in range(40):
        if not cmdpath.exists() or not cmdpath.read_bytes():
            break
        time.sleep(1)
    else:
        raise RuntimeError('Runner did not exit; not releasing followup barrier')
report = {'status': 'schedule_finished', 'baseline_complete': ['q08_384','q2_384'],
          'deferred_phases': ['q2_768','q08_768'],
          'reason': 'High-resolution controls are rescheduled after isolated runtime comparison; avoid redundant slow repeats.'}
(root / 'presence_transition.json').write_text(json.dumps(report, indent=2))
temp = root / 'presence/progress.transition'
temp.write_text(json.dumps(report, indent=2))
temp.replace(root / 'presence/progress.json')
print(json.dumps(report), flush=True)
