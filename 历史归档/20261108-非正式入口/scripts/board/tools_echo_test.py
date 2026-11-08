#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文输入自检：验证键盘打进去的中文，到达 Python 时是否完好。

用法（在 Xshell 里手动运行，别用管道）：
    python3 /home/biandu/rv/tools/echo_test.py
"""
import sys

CJK = lambda ch: "\u4e00" <= ch <= "\u9fff"


def main():
    print("stdin 编码 =", sys.stdin.encoding, " stdout 编码 =", sys.stdout.encoding)
    print("输入一句中文（例如：输出数字一百二十三），回车：", end=" ", flush=True)
    try:
        s = input()
    except EOFError:
        print("\n[EOF] 没有读到输入")
        return 1

    print("\n收到 %r，长度 %d" % (s, len(s)))
    print("十六进制：", s.encode("utf-8", "backslashreplace"))

    n_cjk = sum(1 for ch in s if CJK(ch))
    n_bad = sum(1 for ch in s if not (CJK(ch) or ch.isascii()))
    if n_cjk and n_bad == 0:
        print("判定：中文传输正常（%d 个汉字完整到达）" % n_cjk)
        return 0
    if n_cjk:
        print("判定：有 %d 个汉字到达，但混入了异常字符（%d 个）" % (n_cjk, n_bad))
        return 0
    print("判定：一个汉字都没收到 —— 大概率是 Xshell 编码不是 UTF-8")
    print("      改法：会话属性 → 终端 → 编码 → 选 'Unicode (UTF-8)'，然后重连")
    return 0


if __name__ == "__main__":
    sys.exit(main())
