"""Regression tests for repository review fixes; no board, network or motor access."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'scripts/pc')]
import buyer_report
import gimbal_flow30 as flow
import polish_packet
import polish_report
import room_review as rr
import test_buyer_report as buyer_fixture
import test_gimbal_flow30 as flow_fixture

spec = importlib.util.spec_from_file_location('review_bringup', ROOT / 'scripts/board/new_board_bringup.py')
bringup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bringup)


class LockTests(unittest.TestCase):
    def run_flow(self, tmp, pulse, lock, capture=flow_fixture.fake_capture):
        return flow.run_flow30(
            {'inbox': tmp, 'gimbal': {}}, pulse=pulse, capture=capture, analyze=False,
            timing=flow.Timing(move_s=0, settle_s=0, hold_s=0, return_s=0, steps=2),
            sleep=lambda _: None, log=lambda *_: None, lock=lock)

    def test_busy_lock_never_reads_or_writes_motor(self):
        @contextmanager
        def busy(_):
            raise rr.ResourceBusyError('busy')
            yield
        pulse = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rr.ResourceBusyError):
                self.run_flow(tmp, pulse, busy)
        pulse.read_us.assert_not_called()
        pulse.write_us.assert_not_called()

    def test_every_motor_access_including_homing_is_locked(self):
        active = False
        @contextmanager
        def held(_):
            nonlocal active
            active = True
            try:
                yield
            finally:
                active = False
        parent = self
        class GuardedPulse(flow_fixture.FakePulse):
            def read_us(self, channel):
                parent.assertTrue(active)
                return super().read_us(channel)
            def write_us(self, channel, value):
                parent.assertTrue(active)
                return super().write_us(channel, value)
        pulse = GuardedPulse()
        with tempfile.TemporaryDirectory() as tmp:
            self.run_flow(tmp, pulse, held)
        self.assertFalse(active)
        self.assertAlmostEqual(pulse.values[flow.PAN_CHANNEL], flow.START_PAN_US)

    def test_capture_failure_homes_before_unlock(self):
        active = False
        @contextmanager
        def held(_):
            nonlocal active
            active = True
            try:
                yield
            finally:
                self.assertAlmostEqual(pulse.values[flow.PAN_CHANNEL], flow.START_PAN_US)
                self.assertAlmostEqual(pulse.values[flow.TILT_CHANNEL], flow.START_TILT_US)
                active = False
        pulse = flow_fixture.FakePulse()
        def fail(*_):
            self.assertTrue(active)
            raise RuntimeError('camera disconnected')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'camera disconnected'):
                self.run_flow(tmp, pulse, held, capture=fail)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = buyer_fixture.make_round(self.root)
        buyer_fixture.add_polish(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def test_summary_cannot_borrow_lower_grade_from_another_view(self):
        for text in ('本轮整体为低风险。', '整体关注程度为「低」。', '整体等级是“低”。'):
            with self.subTest(text=text):
                path = self.folder / 'polish/model_output.json'
                path.write_text(json.dumps({'summary': text, 'advice': ['建议整理。']}, ensure_ascii=False), encoding='utf-8')
                with self.assertRaisesRegex(buyer_report.ReportError, '冲突的等级'):
                    buyer_report.build(self.folder)

    def test_advice_grade_must_match_its_region(self):
        output = self.folder / 'polish/model_output.json'
        output.write_text(json.dumps({'summary': '可见区域初评。', 'advice': ['此处为低风险。']}, ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesRegex(buyer_report.ReportError, r'advice\[0\]'):
            buyer_report.build(self.folder)

    def test_external_compose_is_used_by_html(self):
        source = self.root / 'external.json'
        source.write_text(json.dumps({'summary': '外部润色摘要。', 'advice': ['外部润色建议。']}, ensure_ascii=False), encoding='utf-8')
        code = polish_report.cmd_compose(argparse.Namespace(
            packet=self.folder / 'polish', model_output=source, tool='test', by='review'))
        self.assertEqual(code, 0)
        html = buyer_report.build(self.folder).read_text(encoding='utf-8')
        self.assertIn('外部润色摘要', html)
        self.assertIn('外部润色建议', html)

    def test_template_replaces_stale_model_slots(self):
        code = polish_report.cmd_template(argparse.Namespace(packet=self.folder / 'polish', tool='template', by='review'))
        self.assertEqual(code, 0)
        output = json.loads((self.folder / 'polish/model_output.json').read_text(encoding='utf-8'))
        self.assertIn('未做语言润色', output['summary'])
        html = buyer_report.build(self.folder).read_text(encoding='utf-8')
        self.assertIn('未做语言润色', html)
        self.assertNotIn('润色后的总体说明', html)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = mock.patch.multiple(bringup, RUNTIME=self.root, LLAMA_DIR=self.root / 'llama',
                                           ORT_DIR=self.root / 'ort', DRY=False, RESULTS=[])
        self.patches.start()
        self.args = argparse.Namespace(offline=False, runtime_dir=None)

    def tearDown(self):
        self.patches.stop()
        self.temp.cleanup()

    def test_download_is_pinned_and_extraction_must_create_runtime(self):
        def extract(cmd, **_):
            if 'llama_017' in cmd[2]:
                path = bringup.LLAMA_DIR / 'bin/llama-server'
                path.parent.mkdir(parents=True)
                path.touch()
            else:
                (bringup.ORT_DIR / 'lib').mkdir(parents=True)
            return 0, '', ''
        with mock.patch.object(bringup, 'ensure_tar', return_value=True) as download, mock.patch.object(bringup, 'run', side_effect=extract):
            self.assertTrue(bringup.step_runtime(self.args))
        self.assertEqual(download.call_count, 2)
        for call in download.call_args_list:
            self.assertTrue(call.args[1].startswith('https://github.com/spacemit-com/'))
            self.assertEqual(len(call.kwargs['sha256']), 64)

    def test_offline_missing_runtime_fails_without_network(self):
        self.args.offline = True
        with mock.patch.object(bringup, 'run') as run:
            self.assertFalse(bringup.step_runtime(self.args))
        run.assert_not_called()

    def test_download_failure_does_not_extract(self):
        with mock.patch.object(bringup, 'ensure_tar', return_value=False), mock.patch.object(bringup, 'run') as run:
            self.assertFalse(bringup.step_runtime(self.args))
        run.assert_not_called()

    def test_extraction_without_expected_files_fails(self):
        with mock.patch.object(bringup, 'ensure_tar', return_value=True), mock.patch.object(bringup, 'run', return_value=(0, '', '')):
            self.assertFalse(bringup.step_runtime(self.args))

    def test_local_runtime_archive_is_checked_before_extraction(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'llama_017.tar.gz').write_bytes(b'corrupt')
        self.args.runtime_dir = str(source)
        self.args.offline = True
        with mock.patch.object(bringup, 'run') as run:
            self.assertFalse(bringup.step_runtime(self.args))
        run.assert_not_called()
        self.assertTrue(any(r['status'] == bringup.FAIL for r in bringup.RESULTS))

    def test_vnc_is_loopback_only(self):
        self.assertIn('-localhost yes', bringup.VNC_UNIT)
        self.assertNotIn('-localhost no', bringup.VNC_UNIT)
        self.assertIn('ssh -N -L', bringup.VNC_NOTE)


class DeploymentTests(unittest.TestCase):
    def test_deployment_to_empty_directory_includes_importable_asr_chain(self):
        import deploy_room_review as deploy
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            class Sftp:
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def local(self, remote): return destination / remote.lstrip('/')
                def stat(self, remote): return self.local(remote).stat()
                def mkdir(self, remote): self.local(remote).mkdir()
                def open(self, remote, mode): return self.local(remote).open(mode, encoding='utf-8')
                def chmod(self, *_): pass
                def put(self, source, remote): shutil.copyfile(source, self.local(remote))
            client = mock.Mock()
            client.open_sftp.return_value = Sftp()
            with mock.patch.object(deploy.k1, 'connect', return_value=client), mock.patch.object(sys, 'argv', ['deploy_room_review.py', '--host', 'test']):
                deploy.main()
            room = destination / 'home/biandu/rv/room_review'
            self.assertTrue((room / 'scripts/board/asr_service.py').is_file())
            result = subprocess.run([sys.executable, '-c',
                'import sys; sys.path.insert(0,"app"); import room_review, asr_client, asr_sensevoice; assert asr_client.service_script().is_file()'],
                cwd=room, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
