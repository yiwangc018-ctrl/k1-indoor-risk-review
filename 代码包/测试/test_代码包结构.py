"""Exercise the source package as a distributable artifact, without a K1 board."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('code_package', PACKAGE / '管理代码包.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class SourcePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='k1-package-check-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_formal_runtime_has_dependency_closure_and_no_archived_entrypoints(self):
        runtime = package.build(self.root / 'runtime with spaces')
        for relative in ('app/room_review.py', 'app/gimbal_flow30.py', 'app/asr_client.py',
                         'app/asr_sensevoice.py', 'app/vlm_runtime.py', 'app/risk_degree.py',
                         'app/buyer_report.py', 'app/polish_packet.py', 'app/polish_report.py',
                         'scripts/board/voice_gimbal30.sh', 'scripts/board/asr_service.py',
                         'gimbal/config.json', 'requirements-board.txt', 'requirements-pc.txt'):
            self.assertTrue((runtime / relative).is_file(), relative)
        for relative in ('app/main.py', 'app/gimbal_scan.py', 'gimbal/hw.py', 'gimbal/control.py',
                         'scripts/validation', 'scripts/train', 'scripts/board/scan_gimbal.sh',
                         'configs/risk_assessment_v1'):
            self.assertFalse((runtime / relative).exists(), relative)
        result = subprocess.run([sys.executable, '-c',
            'import sys; sys.path.insert(0,"app"); import room_review, gimbal_flow30, buyer_report; '
            'assert gimbal_flow30.PAN_CHANNEL == 15; assert gimbal_flow30.TILT_CHANNEL == 11'],
            cwd=runtime, capture_output=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_cli_help_does_not_require_models_or_touch_hardware(self):
        runtime = package.build(self.root / 'runtime')
        for relative in ('app/room_review.py', 'app/gimbal_flow30.py', 'app/buyer_report.py',
                         'scripts/board/new_board_bringup.py', 'scripts/pc/deploy_room_review.py',
                         'scripts/pc/sync_room_review.py', 'scripts/pc/accept_new_board.py'):
            with self.subTest(relative=relative):
                result = subprocess.run([sys.executable, relative, '--help'], cwd=runtime,
                                        capture_output=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_normalizes_shell_line_endings(self):
        runtime = package.build(self.root / 'runtime')
        for path in (runtime / 'scripts/board').glob('*.sh'):
            self.assertNotIn(b'\r', path.read_bytes(), str(path))

    def test_unmanaged_output_is_preserved(self):
        out = self.root / 'unmanaged'
        out.mkdir()
        keep = out / 'notes.txt'
        keep.write_text('user data', encoding='utf-8')
        with self.assertRaises(ValueError):
            package.build(out)
        self.assertEqual(keep.read_text(encoding='utf-8'), 'user data')

    def test_rebuild_removes_only_obsolete_manifest_files(self):
        out = package.build(self.root / 'runtime')
        legacy = out / 'app/obsolete.py'
        legacy.write_text('old', encoding='utf-8')
        keep = out / 'user-notes.txt'
        keep.write_text('keep', encoding='utf-8')
        marker = out / 'runtime_manifest.json'
        data = json.loads(marker.read_text(encoding='utf-8'))
        data['files'].append({'runtime': 'app/obsolete.py'})
        marker.write_text(json.dumps(data), encoding='utf-8')
        package.build(out)
        self.assertFalse(legacy.exists())
        self.assertTrue(keep.exists())

    def test_zip_rebuilds_after_extraction_without_repository_or_archive(self):
        archive = self.root / 'source.zip'
        result = subprocess.run([sys.executable, str(PACKAGE / '管理代码包.py'), 'zip', '--out', str(archive)],
                                capture_output=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self.root / 'extracted code'
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            self.assertFalse(any('历史归档/' in name or '__pycache__' in name for name in names))
            self.assertFalse(any(name.endswith(('.gguf', '.onnx', '.wav', '.jpg')) for name in names))
            zipped.extractall(extracted)
        result = subprocess.run([sys.executable, str(extracted / '代码包/管理代码包.py'), 'build'],
                                capture_output=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((extracted / 'dist/运行目录/app/room_review.py').is_file())
        for name in ('K1主程序开发文档.md', '云台控制开发文档.md', '视觉评估与报告生成文档.md',
                     'PC端工具开发文档.md', '开源组件与模型说明.md'):
            self.assertTrue((extracted / '代码包' / name).is_file(), name)


if __name__ == '__main__':
    unittest.main()
