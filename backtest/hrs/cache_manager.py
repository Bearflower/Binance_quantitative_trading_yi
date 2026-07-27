#!/usr/bin/env python3
"""
K线数据缓存管理器
管理本地K线数据的缓存生命周期，包括有效性检查、元数据管理和过期清理。
"""
import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple

import structlog

logger = structlog.get_logger()

# 缓存目录
CACHE_DIR = Path(__file__).parent / "data"

# 缓存最大容量（MB），超过时打印警告
MAX_CACHE_SIZE_MB = 500


def _get_cache_paths(symbol: str) -> Tuple[Path, Path]:
    """
    获取指定交易对的缓存文件路径

    Args:
        symbol: 交易对，如 LABUSDT

    Returns:
        (数据文件路径, 元数据文件路径)
    """
    symbol_lower = symbol.lower()
    data_file = CACHE_DIR / f"{symbol_lower}_1h.csv"
    meta_file = CACHE_DIR / f"{symbol_lower}_meta.json"
    return data_file, meta_file


def is_cache_valid(symbol: str, max_age_hours: int = 24) -> bool:
    """
    检查指定交易对的缓存是否有效

    Args:
        symbol: 交易对，如 LABUSDT
        max_age_hours: 缓存最大有效时长（小时），默认24小时

    Returns:
        缓存是否有效（数据文件存在、元数据存在且未过期）
    """
    data_file, meta_file = _get_cache_paths(symbol)

    # 数据文件必须存在
    if not data_file.exists():
        logger.debug("缓存数据文件不存在", symbol=symbol, path=str(data_file))
        return False

    # 元数据文件必须存在
    if not meta_file.exists():
        logger.debug("缓存元数据文件不存在", symbol=symbol, path=str(meta_file))
        return False

    # 检查元数据中的下载时间
    try:
        meta = get_cache_meta(symbol)
        if meta is None:
            return False

        downloaded_at = meta.get("downloaded_at")
        if not downloaded_at:
            logger.debug("元数据中缺少下载时间", symbol=symbol)
            return False

        download_time = datetime.fromisoformat(downloaded_at)
        age = datetime.now(timezone.utc) - download_time
        age_hours = age.total_seconds() / 3600

        if age_hours > max_age_hours:
            logger.info(
                "缓存已过期",
                symbol=symbol,
                age_hours=round(age_hours, 1),
                max_age_hours=max_age_hours,
            )
            return False

        logger.debug(
            "缓存有效",
            symbol=symbol,
            age_hours=round(age_hours, 1),
            rows=meta.get("rows", 0),
        )
        return True

    except (ValueError, TypeError) as e:
        logger.warning("缓存有效性检查异常", symbol=symbol, error=str(e))
        return False


def get_cache_meta(symbol: str) -> Optional[Dict]:
    """
    读取指定交易对的缓存元数据

    Args:
        symbol: 交易对

    Returns:
        元数据字典，读取失败返回 None
    """
    _, meta_file = _get_cache_paths(symbol)

    if not meta_file.exists():
        return None

    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("读取缓存元数据失败", symbol=symbol, error=str(e))
        return None


def save_cache_meta(symbol: str, meta: Dict) -> bool:
    """
    保存指定交易对的缓存元数据

    Args:
        symbol: 交易对
        meta: 元数据字典，至少包含 symbol, downloaded_at, rows, start_time, end_time

    Returns:
        是否保存成功
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _, meta_file = _get_cache_paths(symbol)

    try:
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        logger.debug("缓存元数据已保存", symbol=symbol, file=str(meta_file))
        return True
    except IOError as e:
        logger.error("保存缓存元数据失败", symbol=symbol, error=str(e))
        return False


def clean_old_cache(max_age_days: int = 7) -> int:
    """
    清理过期的缓存文件

    删除超过 max_age_days 天的元数据文件、对应的数据文件和OI缓存文件。

    Args:
        max_age_days: 最大保留天数，默认7天

    Returns:
        清理的交易对数量
    """
    if not CACHE_DIR.exists():
        return 0

    cutoff_time = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cleaned_count = 0

    for meta_file in CACHE_DIR.glob("*_meta.json"):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)

            downloaded_at = meta.get("downloaded_at")
            if not downloaded_at:
                continue

            download_time = datetime.fromisoformat(downloaded_at)
            if download_time < cutoff_time:
                symbol_lower = meta_file.stem.replace("_meta", "")

                # 删除元数据文件
                meta_file.unlink()
                # 删除对应的数据文件
                data_file = CACHE_DIR / f"{symbol_lower}_1h.csv"
                if data_file.exists():
                    data_file.unlink()
                # 删除对应的OI缓存文件
                oi_cache = CACHE_DIR / f"{symbol_lower}_oi_cache.json"
                if oi_cache.exists():
                    oi_cache.unlink()

                cleaned_count += 1
                logger.info(
                    "已清理过期缓存",
                    symbol=meta.get("symbol", symbol_lower),
                    age_days=(datetime.now(timezone.utc) - download_time).days,
                )
        except (json.JSONDecodeError, IOError, ValueError) as e:
            logger.warning("清理缓存时读取元数据失败", file=str(meta_file), error=str(e))

    if cleaned_count > 0:
        logger.info("缓存清理完成", cleaned_count=cleaned_count, max_age_days=max_age_days)

    return cleaned_count


def get_cache_size_mb() -> float:
    """
    获取缓存目录总大小（MB），超过500MB时打印警告

    Returns:
        缓存总大小（MB）
    """
    if not CACHE_DIR.exists():
        return 0.0

    total_bytes = 0
    for file_path in CACHE_DIR.rglob("*"):
        if file_path.is_file():
            total_bytes += file_path.stat().st_size

    total_mb = total_bytes / (1024 * 1024)

    if total_mb > MAX_CACHE_SIZE_MB:
        logger.warning(
            "缓存大小超过阈值",
            size_mb=round(total_mb, 2),
            threshold_mb=MAX_CACHE_SIZE_MB,
            recommendation="建议运行 clean_old_cache() 清理过期缓存",
        )

    return total_mb