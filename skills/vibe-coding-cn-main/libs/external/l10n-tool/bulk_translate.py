#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量翻译 i18n/zh 下的所有文本文件到其他语言目录。
- 保持目录结构，遇到代码块自动跳过翻译。
- 目标语言集合：自动读取 i18n 下的子目录，排除 zh。
用法：
    python bulk_translate.py --src-root ../../i18n/zh --dst-root ../../i18n --src-lang zh
可选：
    --langs en es ...   # 指定目标语言；默认自动扫描
    --overwrite         # 允许覆盖已有文件
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, List

try:
    from deep_translator import GoogleTranslator
except ImportError:
    sys.stderr.write("[错误] 缺少 deep-translator，请先 pip install -r requirements.txt\n")
    sys.exit(1)


def translate_blocks(text: str, translator: GoogleTranslator) -> str:
    lines = text.splitlines()
    translated: List[str] = []
    in_code = False
    buffer: List[str] = []

    def flush_buffer():
        if not buffer:
            return
        chunk = "\n".join(buffer)
        try:
            result = translator.translate(chunk)
            if result is None:
                raise RuntimeError("翻译返回空结果")
            translated.extend(result.split("\n"))
        except Exception:
            # 兜底：按行逐条翻译，避免整段失败
            for line in buffer:
                try:
                    res_line = translator.translate(line) or line
                except Exception:
                    res_line = line  # 保留原文，留待人工校对
                translated.append(res_line)
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
        if not line.strip():
            flush_buffer()
            translated.append(line)
            continue
        buffer.append(line)
    flush_buffer()
    return "\n".join(translated)


def iter_source_files(src_root: Path) -> Iterable[Path]:
    for path in src_root.rglob('*'):
        if path.is_file():
            yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="批量翻译 i18n/zh -> 其他语言")
    parser.add_argument('--src-root', default='../../i18n/zh', help='源语言根目录')
    parser.add_argument('--dst-root', default='../../i18n', help='目标语言根目录集合')
    parser.add_argument('--src-lang', default='zh-CN', help='源语言代码')
    parser.add_argument('--langs', nargs='*', help='指定目标语言，不含源语言')
    parser.add_argument('--overwrite', action='store_true', help='允许覆盖已有文件')
    args = parser.parse_args()

    src_root = Path(args.src_root).resolve()
    dst_root = Path(args.dst_root).resolve()

    if not src_root.exists():
        sys.stderr.write(f"[错误] 源目录不存在: {src_root}\n")
        return 1

    code_overrides = {
        'zh': 'zh-CN',
        'zh-CN': 'zh-CN',
        'he': 'iw',  # Google 使用旧代码
    }

    def map_code(lang: str) -> str:
        return code_overrides.get(lang, lang)

    if args.langs:
        target_langs = [lang for lang in args.langs if map_code(lang) != args.src_lang]
    else:
        target_langs = [p.name for p in dst_root.iterdir() if p.is_dir() and map_code(p.name) != args.src_lang]

    if not target_langs:
        sys.stderr.write("[错误] 无目标语言目录\n")
        return 1

    for lang in target_langs:
        translator = GoogleTranslator(source=args.src_lang, target=map_code(lang))
        print(f"==== 开始翻译 -> {lang} ====")
        for src_file in iter_source_files(src_root):
            rel_path = src_file.relative_to(src_root)
            dst_file = dst_root / lang / rel_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)

            if dst_file.exists() and not args.overwrite:
                print(f"跳过已存在 {dst_file}")
                continue

            with open(src_file, 'r', encoding='utf-8') as f:
                content = f.read()

            try:
                translated = translate_blocks(content, translator)
            except Exception as exc:
                print(f"[失败] {src_file} -> {dst_file}: {exc}")
                continue

            with open(dst_file, 'w', encoding='utf-8') as f:
                f.write(translated + '\n')
            print(f"[OK] {src_file} -> {dst_file}")

    print("全部翻译完成")
    return 0


if __name__ == '__main__':
    sys.exit(main())
