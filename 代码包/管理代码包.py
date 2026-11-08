"""组装正式运行目录、校验归属、执行测试；只依赖 Python 标准库。"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

PACKAGE = Path(__file__).resolve().parent
REPOSITORY = PACKAGE.parent
MANIFEST = PACKAGE / '文件归属.json'


def entries():
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    targets = set()
    listed = set()
    for item in data['files']:
        source = (PACKAGE / item['source']).resolve()
        target = Path(item['runtime'])
        if not source.is_relative_to(PACKAGE.resolve()) or source.is_symlink() or not source.is_file():
            raise ValueError(f'源文件缺失或越界：{source}')
        if target.is_absolute() or '..' in target.parts or target.as_posix() in targets:
            raise ValueError(f'重复或非法运行路径：{target}')
        targets.add(target.as_posix())
        listed.add(source)
    # 新增源文件必须登记，不能静默漏进包/漏部署。
    for group in ('K1风险初评主程序', '云台控制模块', 'PC端部署与报告工具'):
        for path in (PACKAGE / group).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in (
                    '.py', '.sh', '.json', '.yaml', '.desktop', '.ps1') and path.resolve() not in listed:
                raise ValueError(f'源文件未登记：{path.relative_to(PACKAGE)}')
    return data['files']


def build(out):
    rows = entries()
    out = Path(out).resolve()
    # 输出不能覆盖源码、历史归档或原始项目。允许仓库 dist/ 下的生成物。
    if (out == REPOSITORY.resolve() or out.is_relative_to(PACKAGE.resolve()) or
            out.is_relative_to((REPOSITORY / '历史归档').resolve()) or
            PACKAGE.resolve().is_relative_to(out)):
        raise ValueError('输出目录不能覆盖源码目录')
    marker = out / 'runtime_manifest.json'
    if out.exists() and any(out.iterdir()):
        if not marker.is_file():
            raise ValueError('输出目录非空且没有组装标记，请指定新的空目录')
        previous = json.loads(marker.read_text(encoding='utf-8'))
        expected = {item['runtime'] for item in rows} | {'LICENSE'}
        for item in previous.get('files', []):
            target = (out / item['runtime']).resolve()
            if not target.is_relative_to(out):
                raise ValueError('旧清单包含越界路径')
            if item['runtime'] not in expected and target.is_file():
                target.unlink()
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for row in rows:
        source = PACKAGE / row['source']
        content = source.read_bytes()
        # 发布文本统一 LF，避免 Windows 检出后板端 bash 解释失败。
        if source.suffix in ('.py', '.sh', '.json', '.txt', '.desktop', '.ps1'):
            content = content.replace(b'\r\n', b'\n')
        target = out / row['runtime']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if source.suffix in ('.sh', '.desktop'):
            target.chmod(0o755)
        records.append({**row, 'sha256': hashlib.sha256(content).hexdigest()})
    (out / 'LICENSE').write_bytes((REPOSITORY / 'LICENSE').read_bytes())
    marker.write_text(json.dumps({'version': 1, 'files': records}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return out


def test(module=None):
    with tempfile.TemporaryDirectory(prefix='k1-formal-test-') as temp:
        runtime = build(Path(temp) / 'runtime')
        if module:
            names = [Path(row['runtime']).stem for row in entries()
                     if row['source'].startswith(module + '/tests/')]
            if not names:
                raise ValueError('该模块没有测试')
            code = subprocess.call([sys.executable, '-m', 'unittest', *names], cwd=runtime / 'tests')
        else:
            code = subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=runtime)
        if code:
            return code
        if not module:
            code = subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", str(PACKAGE / "测试")], cwd=REPOSITORY)
            if code: return code
        # 旧 PCA 测试是独立脚本，不会被 unittest 自动收集，显式执行。
        if not module or module == '云台控制模块':
            code = subprocess.call([sys.executable, 'tests/test_pca_driver.py'], cwd=runtime)
            if code: return code
            code = subprocess.call([sys.executable, '-m', 'gimbal', 'selftest'], cwd=runtime)
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check', help='检查源码归属及目标路径冲突')
    build_parser = sub.add_parser('build', help='组装板端/PC共用运行目录')
    build_parser.add_argument('--out', type=Path, default=REPOSITORY / 'dist/运行目录')
    test_parser = sub.add_parser('test', help='在临时组装目录运行正式测试，不连接开发板')
    test_parser.add_argument('--module', choices=('K1风险初评主程序','云台控制模块','PC端部署与报告工具'))
    zip_parser = sub.add_parser('zip', help='打包源码、测试和开发文档，不含历史归档/模型/现场照片')
    zip_parser.add_argument('--out', type=Path, default=REPOSITORY / 'dist/K1风险初评代码包-20261108.zip')
    args = parser.parse_args(argv)
    if args.command == 'check':
        print(f'归属校验通过：{len(entries())} 个运行文件')
    elif args.command == 'build':
        print(build(args.out))
    elif args.command == 'test':
        return test(args.module)
    else:
        entries()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.out, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(PACKAGE.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    archive.write(path, '代码包/' + path.relative_to(PACKAGE).as_posix())
            archive.write(REPOSITORY / 'LICENSE', 'LICENSE')
        print(args.out.resolve())
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f'代码包操作失败：{exc}', file=sys.stderr)
        raise SystemExit(2)
