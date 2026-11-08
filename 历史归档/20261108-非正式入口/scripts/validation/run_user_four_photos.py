"""Run four user photos on the existing isolated K1 2B runtime; save evidence."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc'))
import k1


def main():
    args = argparse.Namespace(host='192.168.137.240', port=22,
        user=k1.DEFAULT_USER, key=k1.DEFAULT_KEY)
    project = Path(__file__).resolve().parents[2]
    ident = datetime.now().strftime('user_four_photos_%Y%m%d_%H%M%S')
    local = project / 'results' / ident
    local.mkdir(parents=True)
    remote = '/home/biandu/rv/' + ident
    prompt = ('请根据这张照片做粗略的环境整理与摆放检查。'
        'floor_items：描述可见地面上放置的物品及集中或散放情况，正常桌椅和设备架本身不算杂物；'
        'elevated_items：描述柜顶或架顶可见物品的摆放情况；'
        'advice：根据以上可见情况给出简短整理或防掉落建议，没有依据不要编造危险。'
        '看不清或没拍到就直接说明。无需精确数量、坐标、距离或掉落概率。'
        '不要检查电缆和门口遮挡，不评价人物。每个字段用一两句简短中文，总共不超过180字。')
    schema = {'type': 'object', 'properties': {key: {'type': 'string'}
        for key in ('floor_items', 'elevated_items', 'advice')},
        'required': ['floor_items', 'elevated_items', 'advice'], 'additionalProperties': False}
    meta = json.loads((project / 'results/k1_validation_20261103/domain/q2_selected_relations/metadata.json').read_text(encoding='utf-8'))
    old = meta['spec']
    spec = {key: old[key] for key in ('binary', 'env', 'discover_dir')}
    spec.update(id='q2_originals', resolution=384, variants=['rough_observation'])
    jobs, manifest = [], []
    client = k1.connect(args)
    try:
        with client.open_sftp() as sftp:
            sftp.mkdir(remote)
            sftp.mkdir(remote + '/inputs')
            for number in (4471, 4472, 4473, 4474):
                source = Path.home() / 'Downloads' / f'IMG_{number}.jpeg'
                raw = source.read_bytes()
                target = remote + '/inputs/' + source.name
                sftp.put(str(source), target)
                from PIL import Image
                with Image.open(source) as im:
                    size = list(im.size)
                manifest.append({'source': str(source), 'remote': target,
                    'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'size': size})
                jobs.append({'job_id': source.stem, 'image_id': source.stem,
                    'image': target, 'variant': 'rough_observation', 'prompt': prompt,
                    'max_tokens': 320, 'timeout_seconds': 240, 'cache_prompt': False,
                    'response_format': {'type': 'json_object', 'schema': schema}})
            for name, data in [('jobs.json', jobs), ('schedule.json', [spec]), ('inputs_manifest.json', manifest)]:
                path = local / name
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                sftp.put(str(path), remote + '/' + name)
            sftp.put(str(project / 'scripts/validation/run_board_study.py'), remote + '/run_board_study.py')
        (local / 'connection.json').write_text(json.dumps({'host': args.host, 'remote': remote}, indent=2), encoding='utf-8')
        cmd = ['/home/biandu/hwenv/bin/python', '-u', remote + '/run_board_study.py',
            '--schedule', remote + '/schedule.json', '--jobs', remote + '/jobs.json',
            '--out', remote + '/outputs', '--port', '8098']
        print('EVIDENCE_DIR=' + str(local), flush=True)
        _, stdout, stderr = client.exec_command(shlex.join(cmd))
        with (local / 'console.log').open('w', encoding='utf-8') as log:
            for line in stdout:
                log.write(line)
                log.flush()
                print(line.rstrip(), flush=True)
        code = stdout.channel.recv_exit_status()
        errors = stderr.read().decode('utf-8', 'replace')
        (local / 'stderr.log').write_text(errors, encoding='utf-8')
        print('EXIT_CODE=' + str(code), flush=True)
        with client.open_sftp() as sftp:
            for name in ('progress.json', 'q2_originals/config.json', 'q2_originals/metadata.json',
                         'q2_originals/responses.jsonl', 'q2_originals/server.log'):
                path = local / 'outputs' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    sftp.get(remote + '/outputs/' + name, str(path))
                except FileNotFoundError:
                    print('MISSING=' + name, flush=True)
            _, out, _ = client.exec_command('pgrep -af ' + shlex.quote(remote) + ' || true')
            (local / 'final_process_check.txt').write_text(out.read().decode(), encoding='utf-8')
    finally:
        client.close()


if __name__ == '__main__':
    main()
