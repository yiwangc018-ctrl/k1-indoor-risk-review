"""麦克风取音 + 本地 ASR（可选）+ 语音播报。

为什么语音这一环不能省：
    赛题第 3 条要求"AI 模型与控制系统深度协同"。纯定时扫描只能证明"会转"，
    而"人说话 → 意图解析 → 云台改变位姿 → 影响后续采图与判定"是一条
    能被评委当场复现的闭环，是这条要求最省成本的证据。

依赖策略（全部本地，不联网）：
    录音：alsa-utils 的 arecord（Bianbu 自带）
    识别：sherpa-onnx 离线中文模型（可选，未装则退化为键盘输入）
    播报：espeak-ng / spd-say（可选，未装则打印）

未装 sherpa-onnx 时，本文件不报错：record 仍可用、recognize 返回 None，
cli 会自动退回"打字当说话"，保证演示链路不因缺依赖而断。
"""

import os
import shutil
import subprocess
import time

RATE = 16000


def have(cmd):
    return shutil.which(cmd) is not None


def record_wav(path, seconds=3.0, device="default", rate=RATE):
    if not have("arecord"):
        raise RuntimeError("没有 arecord，先 apt install alsa-utils")
    cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE",
           "-r", str(rate), "-c", "1", "-d", "%.1f" % float(seconds), path]
    subprocess.run(cmd, check=True)
    return path


def list_devices():
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception as e:
        return "无法列出录音设备：%s" % e


# ---------------- ASR ----------------
class SherpaASR:
    """sherpa-onnx 离线识别封装。模型目录自行下载，支持两种常见布局。"""

    def __init__(self, model_dir, num_threads=2, log=print):
        try:
            import sherpa_onnx
        except ImportError:
            raise RuntimeError(
                "未安装 sherpa-onnx。装法见 README「语音」一节；"
                "或用 --asr off 走键盘输入。")
        self.log = log
        self.rec = self._build(sherpa_onnx, model_dir, num_threads)
        log("[ASR] sherpa-onnx 已加载：%s" % model_dir)

    def _find(self, model_dir, *names):
        for n in names:
            p = os.path.join(model_dir, n)
            if os.path.isfile(p):
                return p
        return None

    def _build(self, so, model_dir, nt):
        if not os.path.isdir(model_dir):
            raise RuntimeError("模型目录不存在：%s" % model_dir)
        tokens = self._find(model_dir, "tokens.txt")
        if tokens is None:
            raise RuntimeError("模型目录缺少 tokens.txt：%s" % model_dir)

        # 1) Paraformer（非流式，中文效果好，模型小）
        m = self._find(model_dir, "model.int8.onnx", "model.onnx")
        if m:
            self.log("[ASR] 识别器类型：Paraformer（非流式）")
            return so.OfflineRecognizer.from_paraformer(
                paraformer=m, tokens=tokens, num_threads=nt, sample_rate=RATE)

        # 2) Zipformer transducer（encoder/decoder/joiner 三件套）
        enc = self._find(model_dir, "encoder.int8.onnx", "encoder.onnx")
        dec = self._find(model_dir, "decoder.int8.onnx", "decoder.onnx")
        joi = self._find(model_dir, "joiner.int8.onnx", "joiner.onnx")
        if enc and dec and joi:
            self.log("[ASR] 识别器类型：Zipformer transducer")
            return so.OfflineRecognizer.from_transducer(
                encoder=enc, decoder=dec, joiner=joi, tokens=tokens,
                num_threads=nt, sample_rate=RATE, feature_dim=80)

        raise RuntimeError("模型目录里没找到可识别的 onnx（需要 model.onnx 或 "
                           "encoder/decoder/joiner 三件套）")

    def recognize(self, wav_path):
        import wave
        with wave.open(wav_path, "rb") as w:
            if w.getframerate() != RATE or w.getnchannels() != 1:
                raise RuntimeError("录音格式需为 %d Hz 单声道，实得 %d Hz / %d ch"
                                   % (RATE, w.getframerate(), w.getnchannels()))
            frames = w.readframes(w.getnframes())
        import array
        samples = array.array("h", frames)
        stream = self.rec.create_stream()
        stream.accept_waveform(RATE, samples)
        self.rec.decode_stream(stream)
        return (stream.result.text or "").strip()


# ---------------- TTS ----------------
class Speaker:
    """有 TTS 就念，没有就静默——绝不因为播报失败影响控制流程。"""

    def __init__(self, enabled=True, log=print):
        self.log = log
        self.engine = None
        if not enabled:
            return
        if have("espeak-ng"):
            self.engine = ["espeak-ng", "-v", "cmn", "-s", "165"]
        elif have("spd-say"):
            self.engine = ["spd-say", "-l", "zh", "-r", "0"]

    def say(self, text):
        if not text:
            return
        if not self.engine:
            return
        try:
            subprocess.run(self.engine + [text], timeout=8,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def press_to_talk_loop(dispatcher, asr, speaker, seconds=3.0, device="default",
                       tmp_wav="/tmp/gimbal_utt.wav", log=print):
    """回车开始说话 → 录 N 秒 → 识别 → 执行。最稳的现场交互方式。"""
    from .voice import parse
    log("按回车开始说话（%d 秒），输入 q 回车退出。" % int(seconds))
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line in ("q", "quit", "退出"):
            break
        if line:
            # 允许直接打字，方便没麦克风时演示
            text = line
        else:
            try:
                log("录音中…")
                record_wav(tmp_wav, seconds, device)
                text = asr.recognize(tmp_wav) if asr else ""
            except Exception as e:
                log("录音/识别失败：%s" % e)
                continue
        if not text:
            log("没听清，再说一次")
            continue
        it = parse(text)
        log("识别：%s  →  %r" % (text, it))
        reply = dispatcher.handle(it)
        log("云台：%s" % reply)
        speaker.say(reply)
        time.sleep(0.05)
