#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显示层：文本 → 位图 → SSD1306 显存 → I2C。

设计要点（第一性原理）
--------------------
1. 屏幕上没有"文字"，只有 128x64 个像素 = 1024 字节的显存（8 页 x 128 列）。
   "显示 123" 的实质是：把 3 个字符按 5x7 字模展开成像素，写进那 1024 字节。
   这一步完全确定，不含任何 AI。
2. 因此本文件对上层只暴露一个契约：show(text) —— 给字符串，出像素。
   上层（ask.py）不需要知道 I2C、地址、页、列。
3. 零第三方依赖：真机只用 smbus2（板子上已有）；没有硬件时用 mock 后端，
   把帧缓冲打成字符画，于是 PC 上也能验证"字形对不对、有没有居中"。

用法
----
    python3 display.py --selftest              # 按 config.yaml 点亮 + 画边框自检
    python3 display.py --mock --text 123       # 不碰硬件，终端看字符画
    python3 display.py --text 123 --bus 4      # 直接指定总线和地址
    python3 display.py --list-i2c              # 列出本机 I2C 总线与权限
"""
from __future__ import annotations

import argparse
import os
import sys

W, H = 128, 64
PAGES = H // 8

# ---------------------------------------------------------------------------
# 5x7 字模：每个字符 7 行、每行 5 列。用"字符串点阵"而不是十六进制表，
# 是为了让字形可以肉眼校对——这也是 mock 渲染能直接验证的前提。
# 只覆盖数字 + 大写字母 + 少量标点；小写自动转大写；中文需要 TTF（见文末）。
# ---------------------------------------------------------------------------
FONT_5x7 = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "%": ("11001", "11010", "00010", "00100", "01000", "01011", "10011"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
}


def _glyph_columns(rows):
    """把 7x5 的行点阵转成 5 个列字节（SSD1306 的页格式：bit0 = 页内最上面一行）。"""
    cols = []
    for c in range(5):
        b = 0
        for r in range(7):
            if rows[r][c] == "1":
                b |= 1 << r
        cols.append(b)
    return tuple(cols)


GLYPHS = {ch: _glyph_columns(rows) for ch, rows in FONT_5x7.items()}
_SPACE = GLYPHS[" "]


def glyph(ch):
    return GLYPHS.get(ch.upper(), _SPACE)


def text_width(text, scale=1):
    """等宽 5x7 + 1 列间距，放大 scale 倍后一行占多少像素。"""
    if not text:
        return 0
    return len(text) * 6 * scale - scale


def text_height(scale=1):
    return 7 * scale


# ---------------------------------------------------------------------------
# 帧缓冲
# ---------------------------------------------------------------------------
class Frame:
    """1024 字节显存的一层薄封装。坐标系：x 0..127 左→右，y 0..63 上→下。"""

    __slots__ = ("buf",)

    def __init__(self):
        self.buf = bytearray(W * PAGES)

    def clear(self):
        for i in range(len(self.buf)):
            self.buf[i] = 0

    def pixel(self, x, y, on=True):
        if 0 <= x < W and 0 <= y < H:
            idx = (y >> 3) * W + x
            if on:
                self.buf[idx] |= 1 << (y & 7)
            else:
                self.buf[idx] &= ~(1 << (y & 7)) & 0xFF

    def rect(self, x, y, w, h, on=True):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.pixel(xx, yy, on)

    def hline(self, x0, x1, y, on=True):
        for x in range(x0, x1 + 1):
            self.pixel(x, y, on)

    def vline(self, y0, y1, x, on=True):
        for y in range(y0, y1 + 1):
            self.pixel(x, y, on)

    def border(self):
        self.hline(0, W - 1, 0)
        self.hline(0, W - 1, H - 1)
        self.vline(0, H - 1, 0)
        self.vline(0, H - 1, W - 1)

    def text(self, text, x, y, scale=1):
        """在 (x, y) 左上角起画一行；越界自动裁掉，不抛异常。"""
        cx = x
        for ch in text:
            for ci, col in enumerate(glyph(ch)):
                if not col:
                    continue
                for bit in range(7):
                    if col & (1 << bit):
                        self.rect(cx + ci * scale, y + bit * scale, scale, scale)
            cx += 6 * scale

    # ----- 便于在没有屏幕时检查 -------------------------------------------------
    def ascii_art(self, down=2):
        """按 down 倍降采样成字符画（'#' = 亮）。128 宽直接打印会被终端折行，故默认 2:1。"""
        lines = []
        for y in range(0, H, down):
            row = []
            for x in range(0, W, down):
                lit = False
                for dy in range(down):
                    for dx in range(down):
                        if 0 <= y + dy < H and 0 <= x + dx < W:
                            idx = (y + dy >> 3) * W + (x + dx)
                            if self.buf[idx] & (1 << ((y + dy) & 7)):
                                lit = True
                                break
                    if lit:
                        break
                row.append("#" if lit else ".")
            lines.append("".join(row))
        return lines

    def lit_count(self):
        return sum(bin(b).count("1") for b in self.buf)

    def rotate180(self):
        out = bytearray(len(self.buf))
        for page in range(PAGES):
            for col in range(W):
                b = self.buf[page * W + col]
                if not b:
                    continue
                nb = 0
                for bit in range(8):
                    if b & (1 << bit):
                        nb |= 1 << (7 - bit)
                out[(PAGES - 1 - page) * W + (W - 1 - col)] = nb
        return out


# ---------------------------------------------------------------------------
# 渲染：把业务语义（一行结果）变成帧
# ---------------------------------------------------------------------------
MAX_SCALE = 6


def render_big(text, scale=None):
    """一行大字、水平垂直居中，自动选能放下的最大档位。

    "123" → scale 6，占 102 x 42 像素，是这块屏上最醒目的画法。
    """
    text = (text or "?").strip() or "?"
    if len(text) > 21:
        text = text[:21]
    if scale is None or scale <= 0:
        scale = 1
        for cand in range(1, MAX_SCALE + 1):
            if text_width(text, cand) <= W - 4 and text_height(cand) <= H - 4:
                scale = cand
    tw, th = text_width(text, scale), text_height(scale)
    f = Frame()
    f.text(text, max(0, (W - tw) // 2), max(0, (H - th) // 2), scale)
    return f, scale


def render_lines(lines, scale=1, top=2, left=2):
    """多行小字（ASCII 模式），用于一边显示指令一边显示结果。"""
    f = Frame()
    y = top
    per_line = max(1, (W - left) // (6 * scale))
    for line in lines:
        f.text(str(line)[:per_line], left, y, scale)
        y += text_height(scale) + 2
        if y >= H:
            break
    return f


# ---------------------------------------------------------------------------
# 文本渲染（中文）：Pillow + TTF
#
# 5x7 字模只能画 ASCII；中文必须走字形引擎。Pillow 在本板（Bianbu 2.3.5）已自带，
# 系统里也有 Noto CJK / Droid Sans Fallback，所以这里不需要额外安装。
# 做法：把文字画进一张 128x64 的灰度图，再按阈值二值化成 1024 字节显存——
# 顺带白捡了抗锯齿：阈值化之前先"柔化"边缘，小字号反而更清晰。
# ---------------------------------------------------------------------------
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)

_FONT_CACHE = {}
DEFAULT_SIZES = (24, 20, 16, 12)
THRESHOLD = 100


def load_font(path=None, size=16):
    """按路径加载 TTF；path 为空时依次试常见字体。找不到就抛 RuntimeError。"""
    from PIL import ImageFont

    key = (path or "", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = ([path] if path else []) + list(FONT_CANDIDATES)
    for cand in candidates:
        if not cand or not os.path.exists(cand):
            continue
        try:
            font = ImageFont.truetype(cand, size)
        except OSError:
            continue
        _FONT_CACHE[key] = font
        return font
    raise RuntimeError(
        "找不到可用的 TTF 字体（试过 %d 个路径）。装一个：sudo apt install fonts-wqy-microhei"
        % len(candidates)
    )


def wrap_text(text, font, max_width):
    """按像素宽度逐字折行。中文没有空格，所以按字符折；英文单词会被硬拆，小屏可接受。"""
    lines = []
    for raw in (text or "").splitlines() or [""]:
        cur = ""
        for ch in raw:
            if font.getlength(cur + ch) <= max_width:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        lines.append(cur)
    return [ln for ln in lines if ln.strip()] or [""]


def render_text(text, font_path=None, sizes=DEFAULT_SIZES, margin=2, line_gap=2,
                threshold=THRESHOLD):
    """一段文字 → 帧。自动选"能放下且最大"的字号，水平垂直居中。

    返回 (frame, size, lines)。
    """
    from PIL import Image, ImageDraw

    text = (text or "").strip() or "?"
    w_max = W - margin * 2
    h_max = H - margin * 2

    chosen = None
    for size in sizes:
        font = load_font(font_path, size)
        lines = wrap_text(text, font, w_max)
        line_h = int(size * 1.15)
        total_h = len(lines) * line_h + (len(lines) - 1) * line_gap
        if total_h <= h_max and all(font.getlength(ln) <= w_max for ln in lines):
            chosen = (font, lines, line_h, total_h, size)
            break

    if chosen is None:
        # 最小字号也放不下：截断最后一行，别让文字跑到屏外
        size = sizes[-1]
        font = load_font(font_path, size)
        line_h = int(size * 1.15)
        max_lines = max(1, (h_max + line_gap) // (line_h + line_gap))
        lines = wrap_text(text, font, w_max)[:max_lines]
        total_h = len(lines) * line_h + (len(lines) - 1) * line_gap
        chosen = (font, lines, line_h, total_h, size)

    font, lines, line_h, total_h, size = chosen

    img = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(img)
    y = max(margin, (H - total_h) // 2)
    for line in lines:
        x = max(0, int((W - font.getlength(line)) / 2))
        draw.text((x, y), line, font=font, fill=255)
        y += line_h + line_gap

    frame = Frame()
    px = img.load()
    for yy in range(H):
        for xx in range(W):
            if px[xx, yy] >= threshold:
                frame.pixel(xx, yy)
    return frame, size, lines


# ---------------------------------------------------------------------------
# 后端：mock / none / i2c
# ---------------------------------------------------------------------------
class Display:
    name = "base"

    def show(self, text, scale=None):
        frame, used = render_big(text, scale)
        self.show_frame(frame)
        return used

    def show_text(self, text, font=None, sizes=DEFAULT_SIZES):
        """任意文本（含中文）。Pillow/字体缺失时退回 5x7 ASCII。"""
        try:
            frame, size, lines = render_text(text, font, sizes)
        except Exception as exc:                    # 没 Pillow / 没字体
            print("[display] 文本渲染不可用（%s），退回 5x7 ASCII" % exc, file=sys.stderr)
            used = self.show(text)
            return used, [text]
        self.show_frame(frame)
        return size, lines

    def show_frame(self, frame):
        raise NotImplementedError

    def show_lines(self, lines):
        self.show_frame(render_lines(lines))

    def close(self):
        pass


class NullDisplay(Display):
    """driver: none —— 只做契约校验，不输出。"""

    name = "none"

    def show_frame(self, frame):
        pass


class MockDisplay(Display):
    """driver: mock —— 把屏幕画在终端里。没有板子也能验收 M1。"""

    name = "mock"

    def __init__(self, down=2, out=None):
        self.down = down
        self.out = out          # None = 写当前 sys.stdout（这样 redirect_stdout 也能抓到）

    @property
    def stream(self):
        return self.out or sys.stdout

    def show_frame(self, frame):
        w = self.stream
        w.write("\n+%s+\n" % ("-" * (W // self.down)))
        for line in frame.ascii_art(self.down):
            w.write("|%s|\n" % line)
        w.write("+%s+\n" % ("-" * (W // self.down)))
        w.flush()

    def show(self, text, scale=None):
        frame, used = render_big(text, scale)
        self.show_frame(frame)
        return used


class I2CDisplay(Display):
    """真机后端。SSD1306 用水平寻址整页刷；SH1106 只有页寻址，逐页刷。"""

    name = "i2c"
    PANEL_COLS = 128
    DRIVER = "ssd1306"

    def __init__(self, bus, address=0x3C, rotate=0, contrast=207, chunk=32):
        from smbus2 import SMBus, i2c_msg  # 只有真机才需要

        self._msg = i2c_msg
        self._bus = SMBus(bus)
        self.addr = address
        self.rotate = 180 if int(rotate) == 180 else 0
        self.chunk = chunk
        self._cmd_seq(self._init_sequence(contrast))

    # --- 底层 -----------------------------------------------------------------
    def _write(self, payload):
        self._bus.i2c_rdwr(self._msg.write(self.addr, list(payload)))

    def _cmd_seq(self, cmds):
        """一次性下发命令流：控制字节 0x00 表示"后面全是命令"。"""
        self._write(bytes([0x00]) + bytes(cmds))

    def _data(self, raw):
        for off in range(0, len(raw), self.chunk):
            self._write(bytes([0x40]) + bytes(raw[off : off + self.chunk]))

    def _init_sequence(self, contrast):
        raise NotImplementedError

    def show_frame(self, frame):
        raise NotImplementedError

    def close(self):
        try:
            self._cmd_seq([0xAE])  # 关显示（省电），下次 show 前会重新打开
        except Exception:
            pass
        try:
            self._bus.close()
        except Exception:
            pass


class SSD1306Display(I2CDisplay):
    DRIVER = "ssd1306"

    def _init_sequence(self, contrast):
        return [
            0xAE,             # display off
            0xD5, 0x80,       # 时钟分频
            0xA8, 0x3F,       # multiplex = 64
            0xD3, 0x00,       # 显示偏移
            0x40,             # 起始行 = 0
            0x8D, 0x14,       # 电荷泵开（4 脚模块必须开）
            0x20, 0x00,       # 水平寻址
            0xA1,             # 列重映射（左右不反）
            0xC8,             # 行扫描方向（上下不反）
            0xDA, 0x12,       # COM 引脚配置
            0x81, contrast & 0xFF,
            0xD9, 0xF1,       # 预充电
            0xDB, 0x40,       # VCOMH
            0xA4,             # 恢复显示 RAM 内容
            0xA6,             # 正常（非反色）
            0x2E,             # 关滚动
            0xAF,             # display on
        ]

    def show_frame(self, frame):
        raw = frame.buf
        if self.rotate == 180:
            raw = frame.rotate180()
        # 列 0..127、页 0..7，然后 1024 字节顺序写入
        self._cmd_seq([0x21, 0x00, 0x7F, 0x22, 0x00, 0x07])
        self._data(raw)


class SH1106Display(I2CDisplay):
    """1.3 寸常见型号：132 列、只有页寻址，且显存起点有 2 列偏移。"""

    DRIVER = "sh1106"
    PANEL_COLS = 132
    COL_OFFSET = 2

    def _init_sequence(self, contrast):
        return [
            0xAE,
            0xD5, 0x80,
            0xA8, 0x3F,
            0xD3, 0x00,
            0x40,
            0xAD, 0x8B,       # SH1106 的 DC-DC 开
            0xA1,
            0xC8,
            0xDA, 0x12,
            0x81, contrast & 0xFF,
            0xD9, 0x22,
            0xDB, 0x35,
            0xA4,
            0xA6,
            0xAF,
        ]

    def show_frame(self, frame):
        raw = frame.buf
        if self.rotate == 180:
            raw = frame.rotate180()
        for page in range(PAGES):
            col = self.COL_OFFSET
            self._cmd_seq([0xB0 | page, col & 0x0F, 0x10 | (col >> 4)])
            self._data(raw[page * W : (page + 1) * W])


DRIVERS = {
    "ssd1306": SSD1306Display,
    "sh1106": SH1106Display,
}


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def scan_i2c(buses, addresses):
    """在若干总线上探测若干地址，返回 [(bus, addr), ...]。

    探测方式就是 i2cdetect 的做法：发一次读字节，设备存在则 ACK，不存在则 NAK/报错。
    只看这些地址，不搞 0x03-0x77 全扫，避免碰到别的从机。
    """
    from smbus2 import SMBus

    hits = []
    for bus in buses:
        try:
            dev = SMBus(int(bus))
        except (FileNotFoundError, PermissionError, OSError) as exc:
            print("[display] i2c-%s 打不开（%s）" % (bus, exc), file=sys.stderr)
            continue
        try:
            for a in addresses:
                try:
                    dev.read_byte(int(a))
                    hits.append((int(bus), int(a)))
                except OSError:
                    pass
        finally:
            dev.close()
    return hits


def open_display(cfg=None):
    """按配置返回显示对象。任何硬件问题都降级成 mock，不让主流程崩。"""
    cfg = cfg or {}
    driver = str(cfg.get("driver", "auto")).lower()
    if driver in ("none", "off", "null"):
        return NullDisplay()
    if driver == "mock":
        return MockDisplay()
    if driver not in DRIVERS and driver != "auto":
        print("[display] 未知 driver=%r，按 auto 处理" % driver, file=sys.stderr)
        driver = "auto"

    rotate = int(cfg.get("rotate", 0))
    contrast = int(cfg.get("contrast", 207))

    try:
        if driver == "auto":
            buses = cfg.get("buses") or [cfg.get("bus", 4), 3]
            addresses = cfg.get("addresses") or [0x3C, 0x3D]
            hits = scan_i2c(buses, addresses)
            if not hits:
                print("[display] auto：i2c-%s 上都没扫到 0x3C/0x3D，降级为 mock"
                      % ",".join(str(b) for b in buses), file=sys.stderr)
                print("[display]   接线检查：VCC=3.3V、SCL/SDA 必须同属一组（都 I2C-4 或都 I2C-3）",
                      file=sys.stderr)
                return MockDisplay()
            bus, addr = hits[0]
            print("[display] auto：在 /dev/i2c-%d 上发现 0x%02X" % (bus, addr))
            cls = DRIVERS[str(cfg.get("panel", "ssd1306")).lower()]
            return cls(bus, addr, rotate=rotate, contrast=contrast)

        bus = int(cfg.get("bus", 4))
        addr = int(str(cfg.get("address", "0x3C")), 0)
        return DRIVERS[driver](bus, addr, rotate=rotate, contrast=contrast)
    except ImportError:
        print("[display] 缺 smbus2（pip install smbus2），降级为 mock", file=sys.stderr)
        return MockDisplay()
    except FileNotFoundError:
        print("[display] /dev/i2c 不存在，降级为 mock", file=sys.stderr)
        return MockDisplay()
    except PermissionError:
        print("[display] 无权限访问 /dev/i2c（加 i2c 组或 udev 0666），降级为 mock", file=sys.stderr)
        return MockDisplay()
    except OSError as exc:
        print("[display] I2C 打开失败（%s），降级为 mock" % exc, file=sys.stderr)
        return MockDisplay()


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------
def _load_cfg(path="config.yaml"):
    try:
        import yaml
    except ImportError:
        print("[display] 未装 pyyaml，用内置默认值", file=sys.stderr)
        return {}
    for candidate in (path, os.path.join(os.path.dirname(os.path.abspath(__file__)), path)):
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8") as fh:
                return (yaml.safe_load(fh) or {}).get("display", {}) or {}
    return {}


def _selftest(disp):
    """三段式自检：边框（验证整屏寻址）→ 四角（验证方向）→ 文字（验证字模）。"""
    print("[selftest] 1/3 画边框：应是完整的一圈，缺边说明列/页寻址不对")
    f = Frame()
    f.border()
    disp.show_frame(f)
    input("          看到完整边框后按回车继续...") if sys.stdin.isatty() else None

    print("[selftest] 2/3 画四个角：应只有四个 4x4 方块，位置不对改 rotate")
    f = Frame()
    for x, y in ((0, 0), (W - 4, 0), (0, H - 4), (W - 4, H - 4)):
        f.rect(x, y, 4, 4)
    disp.show_frame(f)
    input("          看到四个角后按回车继续...") if sys.stdin.isatty() else None

    print("[selftest] 3/3 画文字：GOOD 应居中且可读")
    used = disp.show("GOOD")
    print("[selftest] 完成，字号 scale=%s，总线=%s" % (used, getattr(disp, "addr", "-")))


def main(argv=None):
    ap = argparse.ArgumentParser(description="SSD1306/SH1106 小屏驱动与自检")
    ap.add_argument("--text", default=None, help="要显示的文本")
    ap.add_argument("--scale", type=int, default=0, help="字号倍数，0=自动")
    ap.add_argument("--mock", action="store_true", help="不碰硬件，终端字符画")
    ap.add_argument("--selftest", action="store_true", help="边框/四角/文字三段自检")
    ap.add_argument("--bus", type=int, default=None)
    ap.add_argument("--addr", default=None)
    ap.add_argument("--driver", default=None, choices=["ssd1306", "sh1106", "mock", "none"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--list-i2c", action="store_true", help="列出 /dev/i2c-* 与权限")
    args = ap.parse_args(argv)

    if args.list_i2c:
        import glob
        import stat

        found = sorted(glob.glob("/dev/i2c-*"))
        if not found:
            print("没有 /dev/i2c-*：加载了 i2c 模块吗？sudo modprobe i2c-dev")
            return 1
        for p in found:
            st = os.stat(p)
            print("%-14s %s  %s" % (p, stat.filemode(st.st_mode),
                                    "可读写" if os.access(p, os.R_OK | os.W_OK) else "需要权限"))
        return 0

    cfg = _load_cfg(args.config)
    if args.mock:
        cfg["driver"] = "mock"
    if args.driver:
        cfg["driver"] = args.driver
    if args.bus is not None:
        cfg["bus"] = args.bus
    if args.addr is not None:
        cfg["address"] = args.addr

    disp = open_display(cfg)
    print("[display] 后端=%s bus=%s addr=%s" % (disp.name, cfg.get("bus"), cfg.get("address")))
    try:
        if args.selftest:
            _selftest(disp)
        elif args.text is not None:
            used = disp.show(args.text, args.scale or None)
            print("[display] 已显示 %r（scale=%s）" % (args.text, used))
        else:
            ap.print_help()
    finally:
        disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
