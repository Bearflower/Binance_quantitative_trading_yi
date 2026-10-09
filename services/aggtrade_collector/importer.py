"""现有 20 分钟历史样本导入器（需求 §2.1 固定指纹文件）。

导入前强制校验 SHA-256，指纹不符直接失败，不替换来源（对应审计脚本同口径）。
文件无 received_at，按 S3/§2.1 口径以「本地导入执行时刻」落 received_at_ms，
该字段不可用于 arrival_time 回放（计划 §2.1）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List

from .models import FILE_FIELD_MAP, normalize_trade

logger = logging.getLogger(__name__)


def verify_sha256(path: Path, expected_sha256: str) -> str:
    """校验文件 SHA-256，不符抛 ValueError；返回实际指纹。"""
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"样本文件指纹不符: {path}\n期望 {expected_sha256}\n实际 {actual}")
    return actual


def load_sample_trades(path: Path, expected_sha256: str, symbol: str,
                      received_at_ms: int) -> List[Dict]:
    """校验指纹并加载样本，逐行归一化为库内记录。"""
    sha256 = verify_sha256(path, expected_sha256)
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"样本文件为空或格式非法: {path}")
    trades = [normalize_trade(r, FILE_FIELD_MAP, symbol, received_at_ms) for r in rows]
    logger.info("样本加载完成: %s 指纹=%s 行数=%s",
                path.name, sha256[:12], len(trades))
    return trades
