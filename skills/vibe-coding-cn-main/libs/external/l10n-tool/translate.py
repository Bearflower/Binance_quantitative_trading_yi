#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量级批量翻译工具：将 Markdown/文本从一种语言翻译到另一种语言。
默认使用 deep-translator 封装的 Google Translator，自带简单的代码块保护。
用法示例：
    python translate.py --input ../../i18n/zh/README.md --src-lang zh --tgt-lang en --output ../../i18n/en/README.md
"""

import argparse
import os
import sys
from typing import List

try:
    from deep_translator import GoogleTranslator
except ImportError as exc:  # pragma: no cover - 运行前请安装依赖
    sys.stderr.write("[错误] 缺少依赖 deep-translator，请先 `pip install -r requirements.txt`\n")
    raise


def translate_blocks(lines: List[str], src: str, tgt: str) -> List[str]:
    """逐段翻译，保持代码块原样，减少上下文丢失。"""
    translated: List[str] = []
    in_code = False
    buffer: List[str] = []
    translator = GoogleTranslator(source=src, target=tgt)

    def flush_buffer():
        if not buffer:
            return
        text = "\n".join(buffer)
        try:
            result = translator.translate(text)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"翻译失败: {exc}") from exc
        translated.extend(result.split("\n"))
        buffer.clear()

    for line in lines:
        if line.strip().startswith("```"):
            flush_buffer()
            in_code = not in_code
            translated.append(line)
            continue
        if in_code:
            translated.append(line)
            continue
        # 空行作为段落分割，先刷新再保留空行
        if not line.strip():
            flush_buffer()
            translated.append(line)
            continue
        buffer.append(line)

    flush_buffer()
    return translated


def main() -> int:
    parser = argparse.ArgumentParser(description="批量翻译文本/Markdown，保护代码块")
    parser.add_argument("--input", required=True, help="源文件路径")
    parser.add_argument("--output", required=True, help="目标文件路径")
    parser.add_argument("--src-lang", required=True, help="源语言代码，如 zh")
    parser.add_argument("--tgt-lang", required=True, help="目标语言代码，如 en")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出文件")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.stderr.write(f"[错误] 源文件不存在: {args.input}\n")
        return 1
    if os.path.exists(args.output) and not args.overwrite:
        sys.stderr.write(f"[错误] 目标文件已存在，加 --overwrite 才会覆盖: {args.output}\n")
        return 1

    with open(args.input, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    translated = translate_blocks(lines, args.src_lang, args.tgt_lang)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(translated) + "\n")

    print(f"[完成] {args.input} -> {args.output} ({args.src_lang} -> {args.tgt_lang})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
