"""Round boundary, integrity and partial-failure behavior; no K1 needed."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import room_review as review


class RoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = review.load(review.ROOT / "configs/room_review_k1.json")
        self.config["inbox"] = str(self.root)
        self.config["audio"]["enabled"] = False
        # 产品配置只保留"开始工作"的嘀嘀；测试里放开，才能验证完整的播报逻辑。
        self.config["audio"]["enabled_cues"] = None
        self.config["audio"]["volume_percent"] = None

    def tearDown(self):
        self.temp.cleanup()

    def ready(self, count=2):
        folder = self.root / "round_test"
        folder.mkdir()
        views = []
        for i in range(count):
            image = folder / f"view_{i}.jpg"
            Image.new("RGB", (640, 480), "gray").save(image)
            views.append({"view_id": f"view_{i}", "file": image.name,
                          "sha256": hashlib.sha256(image.read_bytes()).hexdigest()})
        review.write_json(folder / "READY.json", {"views": views})
        return folder

    def test_unfinished_round_not_analyzed(self):
        folder = self.root / "round_incomplete"
        folder.mkdir()
        (folder / "photo.jpg").write_bytes(b"still being written")
        with patch.object(review, "exclusive") as lock, patch.object(review, "analyze_round") as analyze:
            review.watch(self.config, once=True)
            analyze.assert_not_called()

    def test_changed_and_escaping_images_rejected(self):
        folder = self.ready()
        manifest = review.load(folder / "READY.json")
        (folder / "view_0.jpg").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "已改变"):
            review.ready_views(folder)
        manifest["views"][0]["file"] = "../outside.jpg"
        review.write_json(folder / "READY.json", manifest)
        with self.assertRaisesRegex(ValueError, "目录内"):
            review.ready_views(folder)

    def test_partial_results_committed_and_not_repeated(self):
        folder = self.ready()

        def fake_phase(spec, jobs, out, port):
            phase = out / spec["id"]
            phase.mkdir()
            row = {"image_id": jobs[0]["image_id"], "ok": True,
                   "text": json.dumps({"status": "assessed", "level": "medium", "reason": "桌边物品突出"})}
            (phase / "responses.jsonl").write_text(json.dumps(row), encoding="utf-8")

        with patch.object(review, "exclusive"), patch.object(review, "run_phase", side_effect=fake_phase) as run:
            result = review.analyze_round(self.config, folder)
            again = review.analyze_round(self.config, folder)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["overall_level"], "medium")
        self.assertEqual(result["assessed_views"], 1)
        self.assertFalse(result["views"][1]["usable"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(again, result)

    def test_failed_model_never_means_low(self):
        result = review.summarize([{"view_id": "one", "usable": False, "error": "timeout"}], 1, [])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["overall_level"])

    def test_low_memory_does_not_launch_model(self):
        folder = self.ready(1)
        with patch.object(review, "exclusive"), patch.object(review, "available_memory_mb", return_value=300), patch.object(review, "run_phase") as run:
            result = review.analyze_round(self.config, folder)
        run.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["overall_level"])
        self.assertIn("内存", result["views"][0]["error"])

    def test_multiview_requires_manual_direction(self):
        with self.assertRaisesRegex(ValueError, "--manual"):
            review.capture_round(self.config, views=4)

    def test_voice_negation_and_traditional_chinese(self):
        self.assertTrue(review.is_start_command("開始檢查。"))
        self.assertTrue(review.is_start_command("请开始检查"))
        self.assertFalse(review.is_start_command("不要开始检查"))
        self.assertFalse(review.is_start_command("开始检查了吗"))
        self.assertFalse(review.is_start_command("准备好了"))
        self.assertTrue(review.is_start_command("開始建查"))
        self.assertFalse(review.is_start_command("不要開始建查"))

    def test_filler_particles_do_not_block_a_start_command(self):
        # SenseVoice 实测常多吐一个"吧/嗯"，多吐字不该让整条命令作废。
        for text in ("开始检查吧", "帮我开始检查吧", "现在开始检查", "开始检查一下", "检查一下。", "开始检查嗯",
                     "帮我开始检查好", "可以开始了", "可以开始了。", "可以开始", "开始工作"):
            self.assertTrue(review.is_start_command(text), text)
        for text in ("不要开始检查嗯", "今天天气不错", "准备好了，拜拜。", "开始检查了吗", "不可以开始"):
            self.assertFalse(review.is_start_command(text), text)

    def test_sensevoice_engine_goes_through_resident_service(self):
        self.config["asr"].update(engine="sensevoice", model_dir=str(self.root), model_name="m.onnx",
                                  service_socket=str(self.root / "asr.sock"))
        (self.root / "m.onnx").write_bytes(b"x")
        folder = self.root / "voice_sv"

        def no_speaker(config, name, events):
            events.append({"audio": name, "status": "error"})

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"RIFF" + b"0" * 44)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(review, "microphone_device", return_value="plughw:0,0"), \
             patch.object(review, "announce", side_effect=no_speaker), \
             patch.object(review.subprocess, "run", side_effect=fake_run), \
             patch("asr_client.transcribe", return_value={"text": "开始检查吧。", "infer_seconds": 0.93,
                                                          "model": "m.onnx"}) as transcribe:
            self.assertTrue(review.record_and_transcribe(self.config, folder, allow_text_cue=True))
        transcribe.assert_called_once()
        saved = review.load(folder / "command.json")
        self.assertEqual(saved["engine"], "sensevoice")
        self.assertEqual(saved["status"], "recognized")
        self.assertEqual(saved["text"], "开始检查吧。")
        self.assertEqual(saved["infer_seconds"], 0.93)
        self.assertNotIn("command", saved)  # 没有 whisper 命令行参数

    def test_sensevoice_service_failure_blocks_capture(self):
        self.config["asr"].update(engine="sensevoice", model_dir=str(self.root), model_name="m.onnx")
        (self.root / "m.onnx").write_bytes(b"x")
        folder = self.root / "voice_sv_fail"

        def no_speaker(config, name, events):
            events.append({"audio": name, "status": "error"})

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"RIFF" + b"0" * 44)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        import asr_client
        with patch.object(review, "microphone_device", return_value="plughw:0,0"), \
             patch.object(review, "announce", side_effect=no_speaker), \
             patch.object(review.subprocess, "run", side_effect=fake_run), \
             patch("asr_client.transcribe", side_effect=asr_client.AsrUnavailable("模型加载超时")):
            with self.assertRaisesRegex(RuntimeError, "服务不可用"):
                review.record_and_transcribe(self.config, folder, allow_text_cue=True)
        saved = review.load(folder / "command.json")
        self.assertEqual(saved["status"], "failed")
        self.assertFalse(saved["accepted"])
        self.assertIn("模型加载超时", saved["error"])

    def test_missing_sensevoice_model_fails_before_recording(self):
        self.config["asr"].update(engine="sensevoice", model_dir=str(self.root / "nope"), model_name="m.onnx")
        folder = self.root / "voice_no_model"
        with self.assertRaisesRegex(FileNotFoundError, "模型未安装"):
            review.record_and_transcribe(self.config, folder)
        self.assertFalse(folder.exists())

    def test_missing_recording_cue_does_not_record_or_recognize(self):
        binary, model = self.root / "asr", self.root / "model"
        binary.write_bytes(b"test")
        model.write_bytes(b"test")
        self.config["asr"].update(binary=str(binary), model=str(model), engine="whisper")
        self.config["audio"]["enabled"] = True
        folder = self.root / "voice_failed"
        def no_speaker(config, name, events):
            events.append({"audio": name, "status": "error"})
        with patch.object(review, "microphone_device", return_value="plughw:0,0"), patch.object(review, "announce", side_effect=no_speaker), patch.object(review.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "没有开始录音"):
                review.record_and_transcribe(self.config, folder)
        run.assert_not_called()
        self.assertFalse((folder / "command.wav").exists())
        self.assertFalse(review.load(folder / "command.json")["accepted"])

    def test_no_cue_records_with_text_prompt_and_bounded_command(self):
        binary, model = self.root / "asr", self.root / "model"
        binary.write_bytes(b"test")
        model.write_bytes(b"test")
        self.config["asr"].update(binary=str(binary), model=str(model), threads=8, max_tokens=16, engine="whisper")
        self.config["audio"]["enabled"] = True
        folder = self.root / "voice_text_cue"

        def no_speaker(config, name, events):
            events.append({"audio": name, "status": "error"})

        def fake_run(command, **kwargs):
            if command[0] == "arecord":
                Path(command[-1]).write_bytes(b"RIFF" + b"0" * 44)
            else:
                (folder / "transcript.txt").write_text("开始检查", encoding="utf-8")
                review.write_json(folder / "transcript.runtime.json",
                                  {"exit_code": 0, "seconds": 0.1, "text_tokens": 4,
                                   "max_tokens": 16, "near_token_limit": False})
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(review, "microphone_device", return_value="plughw:0,0"), \
             patch.object(review, "announce", side_effect=no_speaker), \
             patch.object(review.subprocess, "run", side_effect=fake_run):
            self.assertTrue(review.record_and_transcribe(self.config, folder, allow_text_cue=True))
        saved = review.load(folder / "command.json")
        self.assertEqual(saved["cue"], "text_prompt")
        self.assertEqual(saved["events"][-1]["status"], "text_prompt")
        self.assertNotIn("-ml", saved["command"])
        self.assertEqual(saved["command"][saved["command"].index("-mt") + 1], "16")
        self.assertEqual(saved["command"][saved["command"].index("-t") + 1], "8")

    def test_recording_starts_after_explicit_human_cue(self):
        binary, model = self.root / "asr", self.root / "model"
        binary.write_bytes(b"test")
        model.write_bytes(b"test")
        self.config["asr"].update(binary=str(binary), model=str(model), engine="whisper")
        self.config["audio"]["enabled"] = True

        def no_speaker(config, name, events):
            events.append({"audio": name, "status": "error"})

        for mode in ("wait_enter", "delay"):
            folder = self.root / f"voice_{mode}"
            order = []

            def fake_run(command, **kwargs):
                if command[0] == "arecord":
                    order.append("record")
                    Path(command[-1]).write_bytes(b"RIFF" + b"0" * 44)
                    return subprocess.CompletedProcess(command, 0, b"", b"")
                (folder / "transcript.txt").write_text("开始检查", encoding="utf-8")
                review.write_json(folder / "transcript.runtime.json", {"near_token_limit": False})
                return subprocess.CompletedProcess(command, 0, b"", b"")

            def fake_sleep(seconds, order=order):
                order.append("sleep")

            with patch.object(review, "microphone_device", return_value="plughw:0,0"), \
                 patch.object(review, "announce", side_effect=no_speaker), \
                 patch.object(review.time, "sleep", side_effect=fake_sleep), \
                 patch("builtins.input", side_effect=lambda prompt="": order.append("enter")), \
                 patch.object(review.subprocess, "run", side_effect=fake_run):
                accepted = review.record_and_transcribe(
                    self.config, folder, allow_text_cue=True,
                    wait_enter=(mode == "wait_enter"), delay_seconds=0 if mode == "wait_enter" else 3)
            self.assertTrue(accepted)
            if mode == "wait_enter":
                self.assertEqual(order, ["enter", "record"])
            else:
                self.assertEqual(order, ["sleep", "sleep", "sleep", "record"])

    def test_capped_transcript_cannot_trigger_even_with_start_prefix(self):
        binary, model, audio = self.root / "asr", self.root / "model", self.root / "input.wav"
        for path in (binary, model, audio):
            path.write_bytes(b"test")
        self.config["asr"].update(binary=str(binary), model=str(model), engine="whisper")
        folder = self.root / "voice_capped"
        def capped(command, **kwargs):
            (folder / "transcript.txt").write_text("开始检查", encoding="utf-8")
            review.write_json(folder / "transcript.runtime.json", {"near_token_limit": True})
            return subprocess.CompletedProcess(command, 4, b"", b"")
        with patch.object(review.subprocess, "run", side_effect=capped):
            self.assertFalse(review.record_and_transcribe(self.config, folder, audio))
        saved = review.load(folder / "command.json")
        self.assertEqual(saved["status"], "rejected")
        self.assertEqual(saved["text"], "开始检查")
        self.assertFalse(saved["accepted"])

    def test_speaker_profile_change_and_no_builtin_fallback(self):
        self.config["audio"].update(enabled=True, directory=str(self.root))
        (self.root / "capture_ok.wav").write_bytes(b"test")
        prefix = self.config["audio"]["target"].rsplit(".", 1)[0]
        builtin = {"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.builtin"}}}
        usb = {"info": {"props": {"media.class": "Audio/Sink", "node.name": prefix + ".stereo-fallback"}}}
        events = []
        with patch.object(review.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, json.dumps([builtin, usb])), subprocess.CompletedProcess([], 0)]) as run:
            review.announce(self.config, "capture_ok", events)
        self.assertEqual(run.call_args.args[0][2], prefix + ".stereo-fallback")
        self.assertEqual(events[0]["status"], "played")
        events = []
        self.config["audio"]["enumerate_attempts"] = 4
        with patch.object(review.time, "sleep"), patch.object(review.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps([builtin]))) as run:
            review.announce(self.config, "capture_ok", events)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(events[0]["status"], "error")

    def test_announce_fails_fast_without_speaker(self):
        self.config["audio"].update(enabled=True, directory=str(self.root))
        (self.root / "capture_ok.wav").write_bytes(b"test")
        builtin = {"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.builtin"}}}
        events = []
        with patch.object(review.time, "sleep") as sleep, patch.object(review.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps([builtin]))) as run:
            review.announce(self.config, "capture_ok", events)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(events[0]["status"], "error")

    def test_speaker_found_by_name_when_the_node_name_changes(self):
        # 换口/重插后节点名会变；写死的 target 失效，名称包含匹配要能救回来。
        self.config["audio"].update(enabled=True, directory=str(self.root))
        (self.root / "capture_ok.wav").write_bytes(b"test")
        self.config["audio"]["target"] = "alsa_output.usb-Old_Device-01.analog-stereo"
        self.config["audio"]["match"] = {"name_contains": ["Jieli"]}
        builtin = {"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.builtin"}}}
        usb = {"info": {"props": {"media.class": "Audio/Sink",
                                  "node.name": "alsa_output.usb-Jieli_Technology_Replugged-01.stereo-fallback"}}}
        events = []
        with patch.object(review.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps([builtin, usb])),
                subprocess.CompletedProcess([], 0)]) as run:
            review.announce(self.config, "capture_ok", events)
        self.assertEqual(events[0]["status"], "played")
        self.assertEqual(run.call_args.args[0][2],
                         "alsa_output.usb-Jieli_Technology_Replugged-01.stereo-fallback")

    def test_two_matching_speakers_are_refused(self):
        self.config["audio"].update(enabled=True, directory=str(self.root), match={"name_contains": ["usb"]})
        (self.root / "capture_ok.wav").write_bytes(b"test")
        sinks = [{"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.usb-A-01.analog-stereo"}}},
                 {"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.usb-B-01.analog-stereo"}}}]
        events = []
        with patch.object(review.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(sinks))):
            review.announce(self.config, "capture_ok", events)
        self.assertEqual(events[0]["status"], "error")
        self.assertIn("多个", events[0]["error"])

    def test_audio_check_writes_state_even_without_speaker(self):
        self.config["inbox"] = str(self.root / "rounds")
        self.config["audio"].update(enabled=True, directory=str(self.root), match={"name_contains": ["Jieli"]})
        builtin = {"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.builtin"}}}
        with patch.object(review.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps([builtin]))):
            state = review.audio_check(self.config)
        self.assertFalse(state["ok"])
        self.assertIn("未找到 USB 音箱", state["error"])
        saved = review.load(self.root / "audio_state.json")
        self.assertEqual(saved["sinks"], ["alsa_output.builtin"])
        self.assertFalse(saved["played"])

    def test_only_the_start_cue_is_audible(self):
        # 用户口径：全程只有"开始工作"的嘀嘀，其它播报点不出声也不算失败。
        self.config["audio"].update(enabled=True, directory=str(self.root), enabled_cues=["start"])
        (self.root / "capture_ok.wav").write_bytes(b"test")
        events = []
        with patch.object(review.subprocess, "run") as run:
            event = review.announce(self.config, "capture_ok", events)
        run.assert_not_called()
        self.assertEqual(event["status"], "skipped")
        self.assertEqual(events, [])

    def test_non_utf8_pw_dump_does_not_crash(self):
        # 板上 pw-dump 里有 GBK 字节；text=True 会直接抛 UnicodeDecodeError。
        self.config["audio"].update(enabled=True, directory=str(self.root), match={"name_contains": ["Jieli"]})
        (self.root / "start.wav").write_bytes(b"test")
        payload = (b'[{"info":{"props":{"media.class":"Audio/Sink",'
                   b'"node.name":"alsa_output.usb-Jieli_X-01.pro-output-0","node.description":"\xb9\xa6\xb7\xc5"}}}]')
        events = []
        with patch.object(review.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, payload),
                subprocess.CompletedProcess([], 0, b"")]):
            event = review.announce(self.config, "start", events)
        self.assertEqual(event["status"], "played")
        self.assertEqual(event["target"], "alsa_output.usb-Jieli_X-01.pro-output-0")

    def test_falls_back_to_alsa_when_pipewire_has_no_sink(self):
        # 这块 USB 音箱在 WirePlumber 里默认只有输入 profile；ALSA 直推是兜底。
        self.config["audio"].update(enabled=True, directory=str(self.root), match={"name_contains": ["Jieli"]})
        (self.root / "start.wav").write_bytes(b"test")
        builtin = {"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.builtin"}}}
        events = []
        with patch.object(review, "alsa_usb_playback", return_value="plughw:3,0"), \
             patch.object(review.subprocess, "run", side_effect=[
                 subprocess.CompletedProcess([], 0, json.dumps([builtin])),
                 subprocess.CompletedProcess([], 0, b"")]) as run:
            event = review.announce(self.config, "start", events)
        self.assertEqual(event["status"], "played")
        self.assertEqual(event["alsa"], "plughw:3,0")
        self.assertEqual(run.call_args.args[0][:3], ["aplay", "-D", "plughw:3,0"])

    def test_no_speaker_and_no_alsa_keeps_the_error(self):
        self.config["audio"].update(enabled=True, directory=str(self.root), match={"name_contains": ["Jieli"]})
        (self.root / "start.wav").write_bytes(b"test")
        builtin = {"info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.builtin"}}}
        events = []
        with patch.object(review, "alsa_usb_playback", return_value=None), \
             patch.object(review.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps([builtin]))):
            event = review.announce(self.config, "start", events)
        self.assertEqual(event["status"], "error")
        self.assertIn("未找到 USB 音箱", event["error"])

    def test_volume_is_raised_before_playing(self):
        # 实测这块卡 PCM 出厂 40%，提示音太轻；播放前要把它拉到配置音量。
        self.config["audio"].update(enabled=True, directory=str(self.root),
                                    match={"name_contains": ["Jieli"]}, volume_percent=100)
        (self.root / "start.wav").write_bytes(b"test")
        usb = {"info": {"props": {"media.class": "Audio/Sink",
                                  "node.name": "alsa_output.usb-Jieli_X-01.pro-output-0"}}}
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[0] == "pw-dump":
                return subprocess.CompletedProcess(command, 0, json.dumps([usb]))
            return subprocess.CompletedProcess(command, 0, b"")

        with patch.object(review, "usb_playback_card", return_value="3"), \
             patch.object(review.subprocess, "run", side_effect=fake_run):
            event = review.announce(self.config, "start", [])
        self.assertEqual(event["status"], "played")
        self.assertEqual(event["volume_card"], "3")
        self.assertIn(["amixer", "-c", "3", "sset", "PCM", "100%"], calls)
        self.assertEqual(calls[-1][0], "pw-play")

    def test_skipped_listen_cue_falls_back_to_text_prompt_instead_of_failing(self):
        binary, model = self.root / "asr", self.root / "model"
        for path in (binary, model):
            path.write_bytes(b"test")
        self.config["asr"].update(binary=str(binary), model=str(model), engine="whisper")
        self.config["audio"].update(enabled=True, directory=str(self.root), enabled_cues=["start"])
        folder = self.root / "voice_skipped"

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"RIFF" + b"0" * 44)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def whisper(command, **kwargs):
            (folder / "transcript.txt").write_text("可以开始了", encoding="utf-8")
            review.write_json(folder / "transcript.runtime.json", {"near_token_limit": False})
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(review, "microphone_device", return_value="plughw:0,0"), \
             patch.object(review.subprocess, "run", side_effect=lambda command, **kw: (
                 fake_run(command, **kw) if command[0] == "arecord" else whisper(command, **kw))):
            self.assertTrue(review.record_and_transcribe(self.config, folder))
        saved = review.load(folder / "command.json")
        self.assertEqual(saved["cue"], "text_prompt")
        self.assertEqual(saved["text"], "可以开始了")
        self.assertTrue(saved["accepted"])

    def test_failed_round_not_retried_in_watcher_loop(self):
        folder = self.ready()
        with patch.object(review, "exclusive"), patch.object(review, "analyze_round", side_effect=ValueError("bad manifest")) as run:
            review.watch(self.config, once=True)
            review.watch(self.config, once=True)
        self.assertEqual(run.call_count, 1)
        self.assertTrue((folder / "analysis_error.json").exists())

    def test_busy_model_keeps_round_queued(self):
        folder = self.ready()
        with patch.object(review, "exclusive"), patch.object(review, "analyze_round", side_effect=[review.ResourceBusyError("busy"), None]) as run:
            review.watch(self.config, once=True)
            self.assertFalse((folder / "analysis_error.json").exists())
            review.watch(self.config, once=True)
        self.assertEqual(run.call_count, 2)

    def test_report_suggestions_are_joined_by_punctuation(self):
        result = review.summarize([{"view_id": "view_01", "usable": True, "level": "medium",
                                    "reason": "台边物品突出",
                                    "focus": [{"kind": "placement", "where": "台边",
                                               "basis": "突出且可能影响日常操作"}]}], 1, [])
        folder = self.root / "round_report"
        folder.mkdir()
        review.report(folder, result)
        text = (folder / "report.md").read_text(encoding="utf-8")
        advice = [line for line in text.splitlines() if line.startswith("- ")][0]
        self.assertIn("影响日常操作。建议", advice)
        self.assertEqual(result["reporting_revision"]["raw_model_output_changed"], False)

    def test_verdict_depends_on_gate(self):
        self.assertEqual(review.decide_verdict("verified", True), "accepted")
        self.assertEqual(review.decide_verdict("verified", False), "rejected")
        self.assertEqual(review.decide_verdict("optimistic", True), "accepted")
        self.assertEqual(review.decide_verdict("optimistic", False), "voided")
        with self.assertRaisesRegex(ValueError, "未知门控"):
            review.decide_verdict("maybe", True)

    def test_held_back_capture_is_not_eligible_until_published(self):
        def fake_run(command, **kwargs):
            if command[0] == "v4l2-ctl":
                target = Path([c for c in command if c.startswith("--stream-to=")][0].split("=", 1)[1])
                Image.new("RGB", (64, 48), "gray").save(target, format="JPEG")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(review, "exclusive"), patch.object(review.time, "sleep"), \
             patch.object(review.subprocess, "run", side_effect=fake_run):
            folder = review.capture_round(self.config, views=1, publish_ready=False)
        self.assertFalse((folder / "READY.json").exists())
        self.assertEqual(review.load(folder / "meta.json")["status"], "captured")
        with patch.object(review, "exclusive"), patch.object(review, "analyze_round") as analyze:
            review.watch(self.config, once=True)
            analyze.assert_not_called()
        review.publish_ready(folder)
        self.assertTrue((folder / "READY.json").exists())
        self.assertEqual(len(review.ready_views(folder)), 1)

    def test_voided_round_keeps_evidence_and_never_reaches_model(self):
        def fake_run(command, **kwargs):
            if command[0] == "v4l2-ctl":
                target = Path([c for c in command if c.startswith("--stream-to=")][0].split("=", 1)[1])
                Image.new("RGB", (64, 48), "gray").save(target, format="JPEG")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        trigger = {"kind": "voice", "gate": "optimistic", "asr": {"text": "开始检查"}}
        with patch.object(review, "exclusive"), patch.object(review.time, "sleep"), \
             patch.object(review.subprocess, "run", side_effect=fake_run):
            folder = review.capture_round(self.config, views=1, trigger=trigger, publish_ready=False)
        meta = review.void_round(folder, "voided", "识别未确认")
        self.assertEqual(meta["status"], "voided")
        self.assertEqual(meta["trigger"]["verdict"], "voided")
        self.assertFalse((folder / "READY.json").exists())
        self.assertTrue((folder / "originals/view_01.jpg").is_file())
        with patch.object(review, "exclusive"), patch.object(review, "analyze_round") as analyze:
            review.watch(self.config, once=True)
            analyze.assert_not_called()
        with self.assertRaisesRegex(ValueError, "captured"):
            review.publish_ready(folder)

    def test_published_round_cannot_be_voided_silently(self):
        folder = self.ready(1)
        with self.assertRaisesRegex(RuntimeError, "已发布 READY"):
            review.void_round(folder)


if __name__ == "__main__":
    unittest.main()
