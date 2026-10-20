"""
本地快照生成器：从 Binance 公共接口拉 1m K 线、历史资金费、exchangeInfo，
落成本地不可变快照 + manifest.json（含 SHA-256 与缺口检查）。

仅离线调用，不进镜像；snapshot_manifest 被 replay_run_id 引用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

UTC = timezone.utc

BINANCE_FAPI_BASE = "https://fapi.binance.com"


def _fetch_json(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Binance 只读端点 GET；无认证。"""
    q = ("?" + urlencode(params)) if params else ""
    url = f"{BINANCE_FAPI_BASE}{path}{q}"
    logger.info("GET %s", url)
    req = Request(url, headers={"User-Agent": "shadow-replay-snapshot/1.0"})
    with urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def fetch_klines_1m(symbol: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """拉 1m K 线。Binance 单次最多 1000 根，循环分页。"""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    all_klines: List[Dict[str, Any]] = []
    limit = 1000
    cur = start_ms
    while cur < end_ms:
        batch = _fetch_json("/fapi/v1/klines", {
            "symbol": symbol,
            "interval": "1m",
            "startTime": cur,
            "endTime": end_ms,
            "limit": limit,
        })
        if not batch:
            break
        for k in batch:
            # Binance 返回数组，转 dict
            all_klines.append({
                "open_time": k[0],
                "open": k[1],
                "high": k[2],
                "low": k[3],
                "close": k[4],
                "volume": k[5],
                "close_time": k[6],
                "quote_volume": k[7],
                "count": k[8],
            })
        cur = batch[-1][0] + 60_000  # 下一分钟
        if len(batch) < limit:
            break
        time.sleep(0.2)  # 轻微限速
    return all_klines


def fetch_funding_rate_history(symbol: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """拉历史资金费。"""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    result: List[Dict[str, Any]] = []
    cur = start_ms
    limit = 1000
    while cur < end_ms:
        batch = _fetch_json("/fapi/v1/fundingRate", {
            "symbol": symbol,
            "startTime": cur,
            "endTime": end_ms,
            "limit": limit,
        })
        if not batch:
            break
        result.extend(batch)
        last_funding_time = max(item["fundingTime"] for item in batch)
        cur = last_funding_time + 1
        if len(batch) < limit:
            break
        time.sleep(0.2)
    return result


def fetch_exchange_info(symbol: str) -> Dict[str, Any]:
    """拉 exchangeInfo，包含 tickSize / stepSize / minNotional。"""
    return _fetch_json("/fapi/v1/exchangeInfo", {"symbol": symbol})


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_manifest_sha256(files: Dict[str, Any]) -> str:
    """基于三个文件的 sha256 字段，按 canonical 口径组合出顶层 manifest 哈希。

    canonical 口径：json.dumps({"klines_1m":..,"funding_rates":..,"exchange_info":..},
    sort_keys=True, separators=(",",":")) 的 utf-8 SHA-256。
    build_snapshot 与 verify_snapshot 必须共用本函数，消除重复代码并保证口径一致。
    """
    payload = {
        "klines_1m": files["klines_1m"]["sha256"],
        "funding_rates": files["funding_rates"]["sha256"],
        "exchange_info": files["exchange_info"]["sha256"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _check_gaps(klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """检查 1m K 线是否存在时间戳缺口或 volume=0 空洞。"""
    gaps: List[Dict[str, Any]] = []
    for i in range(1, len(klines)):
        prev = klines[i - 1]["open_time"]
        cur = klines[i]["open_time"]
        if cur - prev != 60_000:
            missing_minutes = (cur - prev) // 60_000 - 1
            gaps.append({
                "from_open_time": prev,
                "to_open_time": cur,
                "missing_minutes": missing_minutes,
            })
    return gaps


def build_snapshot(
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: str,
) -> Dict[str, Any]:
    """拉取三份数据、落盘、写 manifest.json。返回 manifest dict。"""
    import os
    os.makedirs(output_dir, exist_ok=True)

    klines = fetch_klines_1m(symbol, start, end)
    funding = fetch_funding_rate_history(symbol, start, end)
    exchange_info = fetch_exchange_info(symbol)

    # 写文件
    klines_path = os.path.join(output_dir, f"{symbol.lower()}_1m.jsonl")
    funding_path = os.path.join(output_dir, f"{symbol.lower()}_funding.json")
    exchange_info_path = os.path.join(output_dir, f"{symbol.lower()}_exchange_info.json")

    with open(klines_path, "w", encoding="utf-8") as f:
        for k in klines:
            f.write(json.dumps(k) + "\n")

    with open(funding_path, "w", encoding="utf-8") as f:
        json.dump(funding, f, indent=2)

    with open(exchange_info_path, "w", encoding="utf-8") as f:
        json.dump(exchange_info, f, indent=2)

    gaps = _check_gaps(klines)

    # 阻断 #4：先单独组装 files 字典（含三个文件的真实 SHA-256），
    # 再基于 files 的 sha256 组合顶层哈希——这是"内容变化 → 顶层哈希变化"的唯一可靠来源，
    # 不再只哈希行数或 exchangeInfo.time（行数相同但内容不同会产生相同哈希）
    files = {
        "klines_1m": {"path": klines_path, "rows": len(klines), "sha256": _sha256_file(klines_path)},
        "funding_rates": {"path": funding_path, "rows": len(funding), "sha256": _sha256_file(funding_path)},
        "exchange_info": {"path": exchange_info_path, "sha256": _sha256_file(exchange_info_path)},
    }

    manifest = {
        "symbol": symbol,
        "interval": "1m",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fetched_at": datetime.now(UTC).isoformat(),
        "sources": {
            "klines_1m": f"{BINANCE_FAPI_BASE}/fapi/v1/klines",
            "funding_rate": f"{BINANCE_FAPI_BASE}/fapi/v1/fundingRate",
            "exchange_info": f"{BINANCE_FAPI_BASE}/fapi/v1/exchangeInfo",
        },
        "files": files,
        "gaps": gaps,
        # 顶层 sha256：必须基于三个文件的**真实内容**哈希组合
        # （与 verify_snapshot 共用 helper，避免重复实现导致口径漂移）
        "sha256": _compute_manifest_sha256(files),
    }

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    logger.info("快照写入：%s，缺口 %d 处", manifest_path, len(gaps))
    return manifest


def verify_snapshot(manifest_path: str) -> Dict[str, Any]:
    """加载 manifest 并校验文件 SHA-256 未被篡改。返回校验结果。

    除逐文件校验外，还会用 `manifest["files"]` 的三个 sha256
    重算顶层 `manifest["sha256"]`，防篡改路径：
    「改文件 + 同步更新嵌套 files.*.sha256 + 保留旧顶层哈希」——
    这种情况下逐文件会过，但顶层哈希必错，fail-closed。
    """
    import os
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    result = {"manifest_path": manifest_path, "files_ok": True, "mismatches": []}
    for key, info in manifest["files"].items():
        path = info["path"]
        if not os.path.exists(path):
            result["files_ok"] = False
            result["mismatches"].append({"file": key, "error": "missing"})
            continue
        actual = _sha256_file(path)
        if actual != info["sha256"]:
            result["files_ok"] = False
            result["mismatches"].append({
                "file": key,
                "expected": info["sha256"],
                "actual": actual,
            })

    # 顶层哈希校验：基于 files.*.sha256 重算，防「嵌套哈希被同步篡改但顶层留旧值」
    expected_top = manifest.get("sha256", "")
    actual_top = _compute_manifest_sha256(manifest["files"])
    if actual_top != expected_top:
        result["files_ok"] = False
        result["mismatches"].append({
            "file": "__manifest_sha256__",
            "expected": expected_top,
            "actual": actual_top,
        })

    result["gaps"] = len(manifest.get("gaps", []))
    return result
