# XAUUSDT MTCPS 黄金校准版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 启动一个与现有 XAU 原版/激进版完全隔离的 MTCPS 黄金校准版影子实例，仅按 XAU 自身 1h ATR% 分布校准波动率门槛，并保留可复算的快照、配置和事件审计信息。

**Architecture:** 新服务 `mtcps-xau-calibrated-shadow` 继续复用 btc-eth 镜像和 MTCPS 原版策略类，通过独立 instance/profile/run 标识隔离。校准工具在本地对部署前闭合 1h K 线生成不可变 JSONL 快照和 manifest，profile 固定 P25/P50；在线采集器只为显式开启的校准 profile 从当前可见闭合 1h K 线确定性初始化 ATR 历史，原版、激进版与生产实例不改变行为。

**Tech Stack:** Python 3.9、pytest、pandas/numpy、PyYAML、asyncio/APScheduler、PostgreSQL `research` schema、Docker Compose、GitHub Actions。

---

## 文件结构与职责

### 新建

- `strategies/btc_eth/shadow/atr_calibration.py`：纯函数校准域；验证闭合 K 线、复用生产 ATR14、生成 P25/P50、规范化快照、生成并验证 manifest、为在线 profile 准备 ATR 初始化序列。
- `strategies/btc_eth/shadow/config/xau_calibrated.yaml`：MTCPS 原版的冻结副本，只允许身份字段和 ATR 校准字段不同。
- `strategies/btc_eth/shadow/config/calibration/xau_calibrated_atr_manifest.json`：部署前校准证据；记录截止时间、样本数、剔除原因、分位数、快照哈希和算法版本。
- `scripts/generate_xau_atr_calibration.py`：本地命令行工具；读取数据库导出的 JSON/JSONL，输出不可变规范化快照与 manifest，不联网、不写数据库。
- `tests/test_strategies/test_xau_atr_calibration.py`：校准算法、筛选、哈希和防未来数据测试。
- `tests/test_strategies/test_xau_calibrated_profile.py`：profile 单变量差异、策略类映射、实例隔离和动态历史初始化集成测试。
- `tests/test_strategies/test_xau_calibrated_compose.py`：Compose 只读安全边界、独立卷、调度错峰及 CI 容器清单守护测试。

### 修改

- `strategies/btc_eth/shadow/instance.py`：增加 `run_id_prefix` 运行时字段，支持 `SHADOW_RUN_ID_PREFIX`。
- `strategies/btc_eth/shadow/main.py`：run ID 和只读客户端使用解析后的 instance 标识；不再硬编码 `mtcps_xau_shadow`。
- `strategies/btc_eth/shadow/collector.py`：注册 `xau_calibrated -> OriginalBTCEthStrategy`，只为显式开启的 profile 初始化 ATR 历史，并把 ATR 审计快照写入 `feature_snapshot`。
- `strategies/btc_eth/strategy.py`：把校准 profile 的 `atr_fallback_percent` 显式映射为动态过滤器的 `fallback_atr_percent`；未配置该键的所有现有 profile 行为不变。
- `docker-compose.yml`：增加独立校准服务，不挂载 shadow 配置目录。
- `.github/workflows/deploy.yml`：把新容器加入残留清理白名单和健康检查清单。
- `tests/test_strategies/test_shadow_collector.py`：补充 run prefix、客户端 instance 和映射回归。
- `tests/test_strategies/test_shadow_compose_config_mount.py`：让遮蔽配置目录的守护覆盖两个 shadow 服务。
- `CLAUDE.md`、`.trae/rules/deployment.md`、`docs/README.md`：更新运行、回退和验证口径。
- `docs/knowledge/decisions/Binance_quantitative_trading-20261027-xau-calibrated-shadow-isolation.md`：记录只校准 ATR、独立八周观察和不全局兼容 `min_samples` 的决策。
- `docs/knowledge/changelog/2026-10-27.md`、`docs/knowledge/INDEX.md`：登记实施与技术债。

### 本地证据，不提交到 Git

- `/Users/yl/vscode/Binance_quantitative_trading/data/research/xau_calibration/xauusdt-1h-source.jsonl`：数据库只读导出的原始输入；主工作区 `data/` 已被 Git 忽略，归档 worktree 时不会丢失。
- `/Users/yl/vscode/Binance_quantitative_trading/data/research/xau_calibration/xauusdt-1h-snapshot.jsonl`：规范化、排序、去重后的不可变快照；其 SHA-256 写入已跟踪 manifest。

---

### Task 1: 参数化校准实例身份与 run 前缀

**Files:**
- Modify: `strategies/btc_eth/shadow/instance.py`
- Modify: `strategies/btc_eth/shadow/main.py`
- Modify: `tests/test_strategies/test_shadow_collector.py`

- [ ] **Step 1: 写失败测试，固定独立 instance 与 run prefix 契约**

在 `tests/test_strategies/test_shadow_collector.py` 增加：

```python
def test_calibrated_instance_env_overrides_identity(monkeypatch):
    from strategies.btc_eth.shadow.instance import parse_shadow_instance_config

    monkeypatch.setenv("INSTANCE_ID", "mtcps_xau_calibrated_shadow")
    monkeypatch.setenv("SHADOW_PROFILES", "xau_calibrated")
    monkeypatch.setenv("SHADOW_RUN_ID_PREFIX", "xau-calibrated-shadow")
    monkeypatch.setenv("SHADOW_CRON", "20 * * * *")

    cfg = {
        "instance": {"execution_mode": "shadow_batch"},
        "shadow": {"run_id_prefix": "xau-shadow", "cron": "15 * * * *"},
    }
    parsed = parse_shadow_instance_config(cfg)

    assert parsed.instance_id == "mtcps_xau_calibrated_shadow"
    assert parsed.profiles == ["xau_calibrated"]
    assert parsed.run_id_prefix == "xau-calibrated-shadow"
    assert parsed.cron == "20 * * * *"
    assert parsed.instance.real_ordering_enabled is False
    assert parsed.instance.state_reporting_enabled is False


def test_calibrated_run_id_uses_configured_prefix():
    from strategies.btc_eth.shadow.main import _generate_run_id

    started = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    run_id = _generate_run_id(
        started,
        "abc123",
        {"xau_calibrated": "cfg-hash"},
        prefix="xau-calibrated-shadow",
    )
    assert run_id.startswith("xau-calibrated-shadow-20261027080000-")


def test_binance_client_receives_runtime_instance_id(monkeypatch):
    from strategies.btc_eth.shadow.main import _build_binance_client

    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    client = _build_binance_client("mtcps_xau_calibrated_shadow")
    assert client.instance_id == "mtcps_xau_calibrated_shadow"
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
python3 -m pytest tests/test_strategies/test_shadow_collector.py \
  -k 'calibrated_instance_env_overrides_identity or calibrated_run_id_uses_configured_prefix or binance_client_receives_runtime_instance_id' -q
```

Expected: `ShadowInstanceConfig` 缺少 `run_id_prefix`，且 `_build_binance_client` 不接受 instance 参数。

- [ ] **Step 3: 最小实现身份参数化**

在 `strategies/btc_eth/shadow/instance.py` 的 dataclass 增加：

```python
run_id_prefix: str
```

在解析函数中增加：

```python
run_id_prefix = (
    os.getenv("SHADOW_RUN_ID_PREFIX")
    or config.get("shadow", {}).get("run_id_prefix")
    or "xau-shadow"
)
```

并在返回值中传入 `run_id_prefix=run_id_prefix`。

把 `strategies/btc_eth/shadow/main.py` 的客户端构造改为：

```python
def _build_binance_client(instance_id: str) -> ReadOnlyBinanceClient:
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        logger.warning(
            "BINANCE_API_KEY / BINANCE_API_SECRET 未设置，只读客户端可能无法部分初始化",
        )
    return ReadOnlyBinanceClient(
        api_key=api_key,
        api_secret=api_secret,
        instance_id=instance_id,
    )
```

两处新 run 的生成都显式传入前缀：

```python
run_id = _generate_run_id(
    now,
    git_commit,
    config_hashes,
    prefix=shadow_cfg.run_id_prefix,
)
```

客户端装配改为：

```python
binance_client = _build_binance_client(shadow_cfg.instance_id)
```

- [ ] **Step 4: 运行定向回归**

Run:

```bash
python3 -m pytest tests/test_strategies/test_shadow_collector.py -q
```

Expected: 全部通过；原 `xau-shadow-` 默认前缀测试仍通过。

- [ ] **Step 5: 提交**

```bash
git add strategies/btc_eth/shadow/instance.py \
  strategies/btc_eth/shadow/main.py \
  tests/test_strategies/test_shadow_collector.py
git commit -m "feat(shadow): parameterize calibrated instance identity"
```

---

### Task 2: 建立校准算法、快照和 manifest 的纯函数边界

**Files:**
- Create: `strategies/btc_eth/shadow/atr_calibration.py`
- Create: `tests/test_strategies/test_xau_atr_calibration.py`

- [ ] **Step 1: 写失败测试，覆盖筛选、生产 ATR、P25/P50 和哈希**

创建 `tests/test_strategies/test_xau_atr_calibration.py`：

```python
import copy
from datetime import datetime, timezone

import pandas as pd
import pytest

from shared.indicators import TechnicalIndicators
from strategies.btc_eth.shadow.atr_calibration import (
    CalibrationError,
    build_calibration,
    canonical_snapshot_bytes,
    prepare_runtime_atr_history,
    verify_manifest,
)


def _rows(count=60, end_ms=1_793_059_199_999):
    rows = []
    for i in range(count):
        close_time = end_ms - (count - 1 - i) * 3_600_000
        base = 3900.0 + i
        rows.append({
            "open_time": close_time - 3_599_999,
            "close_time": close_time,
            "open": base,
            "high": base + 12.0,
            "low": base - 8.0,
            "close": base + 3.0,
            "volume": 1000.0,
            "trades": 50,
        })
    return rows


def test_calibration_reuses_production_atr_and_percentiles():
    rows = _rows()
    result = build_calibration(rows, cutoff_ms=rows[-1]["close_time"])

    df = pd.DataFrame(rows)
    expected_atr = TechnicalIndicators.calculate_atr(df, period=14).dropna()
    expected_close = df.loc[expected_atr.index, "close"]
    expected_pct = expected_atr / expected_close * 100

    assert result.valid_rows == 60
    assert result.atr_sample_count == len(expected_pct)
    assert result.p25_percent == pytest.approx(expected_pct.quantile(0.25))
    assert result.p50_percent == pytest.approx(expected_pct.quantile(0.50))
    assert result.atr_abs_min == pytest.approx(result.p25_percent / 100.0)


def test_invalid_future_duplicate_and_zero_activity_rows_are_counted():
    rows = _rows()
    future = dict(rows[-1], close_time=rows[-1]["close_time"] + 3_600_000)
    duplicate = dict(rows[-1])
    zero_activity = dict(
        rows[0],
        open_time=rows[0]["open_time"] - 3_600_000,
        close_time=rows[0]["close_time"] - 3_600_000,
        volume=0,
        trades=0,
    )
    flat_price = rows[0]["close"]
    flat = dict(
        rows[0],
        open_time=rows[0]["open_time"] - 7_200_000,
        close_time=rows[0]["close_time"] - 7_200_000,
        open=flat_price,
        high=flat_price,
        low=flat_price,
        close=flat_price,
    )

    result = build_calibration(
        rows + [future, duplicate, zero_activity, flat],
        cutoff_ms=rows[-1]["close_time"],
    )
    assert result.exclusions == {
        "duplicate_close_time": 1,
        "future_or_open": 1,
        "non_positive_activity": 1,
        "zero_range": 1,
    }


def test_manifest_hash_tamper_fails_closed():
    rows = _rows()
    result = build_calibration(rows, cutoff_ms=rows[-1]["close_time"])
    manifest = result.to_manifest(
        source="kline-service-postgres",
        git_commit="abc123",
        snapshot_sha256=result.snapshot_sha256,
        generated_at="2026-10-27T08:00:00+00:00",
    )
    verify_manifest(manifest, canonical_snapshot_bytes(result.snapshot_rows))

    tampered = copy.deepcopy(manifest)
    tampered["calibration"]["p50_percent"] += 0.01
    with pytest.raises(CalibrationError, match="manifest_sha256"):
        verify_manifest(tampered, canonical_snapshot_bytes(result.snapshot_rows))


def test_runtime_history_excludes_current_bar_and_future_bars():
    rows = _rows()
    rows.append(dict(rows[-1], close_time=rows[-1]["close_time"] + 3_600_000))
    atr_values, closes = prepare_runtime_atr_history(
        rows,
        scheduled_at=datetime.fromtimestamp(
            _rows()[-1]["close_time"] / 1000,
            tz=timezone.utc,
        ),
    )
    assert len(atr_values) == len(closes) == 46
    assert all(value > 0 for value in atr_values)
```

- [ ] **Step 2: 运行测试确认模块不存在**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_atr_calibration.py -q
```

Expected: collection error，`strategies.btc_eth.shadow.atr_calibration` 尚不存在。

- [ ] **Step 3: 实现规范化、校准和 manifest 验证**

创建 `strategies/btc_eth/shadow/atr_calibration.py`，接口固定为：

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

from shared.indicators import TechnicalIndicators


ALGORITHM_VERSION = "xau-atr14-sma-v1"


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class CalibrationResult:
    cutoff_ms: int
    source_rows: int
    valid_rows: int
    atr_sample_count: int
    exclusions: Dict[str, int]
    gaps: List[Dict[str, int]]
    p25_percent: float
    p50_percent: float
    atr_abs_min: float
    snapshot_sha256: str
    snapshot_rows: List[Dict[str, Any]]

    def to_manifest(
        self,
        *,
        source: str,
        git_commit: str,
        snapshot_sha256: str,
        generated_at: str,
    ) -> Dict[str, Any]:
        manifest = {
            "schema_version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "symbol": "XAUUSDT",
            "interval": "1h",
            "source": source,
            "generated_at": generated_at,
            "cutoff_ms": self.cutoff_ms,
            "snapshot_start_close_time": self.snapshot_rows[0]["close_time"],
            "snapshot_end_close_time": self.snapshot_rows[-1]["close_time"],
            "source_rows": self.source_rows,
            "valid_rows": self.valid_rows,
            "atr_sample_count": self.atr_sample_count,
            "exclusions": self.exclusions,
            "gaps": self.gaps,
            "snapshot_sha256": snapshot_sha256,
            "git_commit": git_commit,
            "calibration": {
                "p25_percent": self.p25_percent,
                "p50_percent": self.p50_percent,
                "atr_abs_min": self.atr_abs_min,
                "fallback_atr_percent": self.p50_percent,
                "atr_percentile": 50,
                "min_history_count": 30,
            },
        }
        manifest["manifest_sha256"] = _manifest_hash(manifest)
        return manifest


def _manifest_hash(manifest: Dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def canonical_snapshot_bytes(rows: Iterable[Dict[str, Any]]) -> bytes:
    lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for row in rows
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _number(row: Dict[str, Any], key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationError(f"invalid_{key}") from exc


def _normalize_rows(rows: Iterable[Dict[str, Any]], cutoff_ms: int):
    source = list(rows)
    exclusions: Dict[str, int] = {}
    accepted: Dict[int, Dict[str, Any]] = {}
    for raw in sorted(source, key=lambda row: int(row.get("close_time", 0))):
        try:
            close_time = int(raw["close_time"])
            open_time = int(raw["open_time"])
            open_price = _number(raw, "open")
            high = _number(raw, "high")
            low = _number(raw, "low")
            close = _number(raw, "close")
            volume = _number(raw, "volume")
            trades = int(raw.get("trades", raw.get("trade_count", 0)))
        except (CalibrationError, KeyError, TypeError, ValueError):
            exclusions["invalid_shape"] = exclusions.get("invalid_shape", 0) + 1
            continue
        if close_time > cutoff_ms or close_time <= open_time:
            exclusions["future_or_open"] = exclusions.get("future_or_open", 0) + 1
            continue
        if close_time in accepted:
            exclusions["duplicate_close_time"] = exclusions.get("duplicate_close_time", 0) + 1
            continue
        if min(open_price, high, low, close) <= 0 or high < max(open_price, close) or low > min(open_price, close):
            exclusions["invalid_ohlc"] = exclusions.get("invalid_ohlc", 0) + 1
            continue
        if volume <= 0 or trades <= 0:
            exclusions["non_positive_activity"] = exclusions.get("non_positive_activity", 0) + 1
            continue
        if high <= low:
            exclusions["zero_range"] = exclusions.get("zero_range", 0) + 1
            continue
        accepted[close_time] = {
            "open_time": open_time,
            "close_time": close_time,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "trades": trades,
        }
    normalized = [accepted[key] for key in sorted(accepted)]
    gaps = []
    for previous, current in zip(normalized, normalized[1:]):
        delta = current["close_time"] - previous["close_time"]
        if delta != 3_600_000:
            gaps.append({
                "previous_close_time": previous["close_time"],
                "next_close_time": current["close_time"],
                "delta_ms": delta,
            })
    return source, normalized, exclusions, gaps


def build_calibration(rows: Iterable[Dict[str, Any]], *, cutoff_ms: int) -> CalibrationResult:
    source, normalized, exclusions, gaps = _normalize_rows(rows, cutoff_ms)
    if len(normalized) < 30:
        raise CalibrationError(f"valid_rows_below_30:{len(normalized)}")
    frame = pd.DataFrame(normalized)
    atr = TechnicalIndicators.calculate_atr(frame, period=14).dropna()
    close = frame.loc[atr.index, "close"]
    atr_percent = atr / close * 100
    if len(atr_percent) < 17:
        raise CalibrationError(f"atr_samples_below_17:{len(atr_percent)}")
    snapshot_bytes = canonical_snapshot_bytes(normalized)
    p25 = float(np.percentile(atr_percent.to_numpy(), 25))
    p50 = float(np.percentile(atr_percent.to_numpy(), 50))
    return CalibrationResult(
        cutoff_ms=cutoff_ms,
        source_rows=len(source),
        valid_rows=len(normalized),
        atr_sample_count=len(atr_percent),
        exclusions=dict(sorted(exclusions.items())),
        gaps=gaps,
        p25_percent=p25,
        p50_percent=p50,
        atr_abs_min=p25 / 100.0,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        snapshot_rows=normalized,
    )


def prepare_runtime_atr_history(
    rows: Iterable[Dict[str, Any]], *, scheduled_at: datetime
) -> Tuple[List[float], List[float]]:
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    cutoff_ms = int(scheduled_at.astimezone(timezone.utc).timestamp() * 1000)
    _source, normalized, _exclusions, _gaps = _normalize_rows(rows, cutoff_ms)
    if len(normalized) < 15:
        raise CalibrationError(f"runtime_rows_below_15:{len(normalized)}")
    frame = pd.DataFrame(normalized)
    atr = TechnicalIndicators.calculate_atr(frame, period=14)
    history = []
    closes = []
    for index in range(len(frame) - 1):
        value = atr.iloc[index]
        if pd.notna(value) and float(value) > 0:
            history.append(float(value))
            closes.append(float(frame.iloc[index]["close"]))
    return history, closes


def verify_manifest(manifest: Dict[str, Any], snapshot_bytes: bytes) -> None:
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise CalibrationError("manifest_sha256 mismatch")
    actual_snapshot = hashlib.sha256(snapshot_bytes).hexdigest()
    if manifest.get("snapshot_sha256") != actual_snapshot:
        raise CalibrationError("snapshot_sha256 mismatch")
```

- [ ] **Step 4: 运行测试并修正测试夹具的确定性边界**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_atr_calibration.py -q
```

Expected: `4 passed`。

- [ ] **Step 5: 提交**

```bash
git add strategies/btc_eth/shadow/atr_calibration.py \
  tests/test_strategies/test_xau_atr_calibration.py
git commit -m "feat(shadow): add deterministic XAU ATR calibration"
```

---

### Task 3: 注册校准 profile 并限定唯一参数差异

**Files:**
- Create: `strategies/btc_eth/shadow/config/xau_calibrated.yaml`
- Modify: `strategies/btc_eth/shadow/collector.py`
- Modify: `strategies/btc_eth/strategy.py`
- Create: `tests/test_strategies/test_xau_calibrated_profile.py`

- [ ] **Step 1: 从原版复制冻结配置**

Run:

```bash
cp strategies/btc_eth/shadow/config/xau_original.yaml \
  strategies/btc_eth/shadow/config/xau_calibrated.yaml
```

只修改以下 YAML 节点；其余节点保持逐值相等：

```yaml
instance:
  id: "mtcps_xau_calibrated_shadow"
  profiles:
    - xau_calibrated

shadow:
  kline_intervals: ["1h", "4h", "1d"]
  cron: "20 * * * *"
  run_id_prefix: "xau-calibrated-shadow"
  bootstrap_dynamic_atr_history: true
  calibration_manifest: "strategies/btc_eth/shadow/config/calibration/xau_calibrated_atr_manifest.json"

strategy:
  name: mtcps_xau_calibrated
  version: "2.6.0-xau-shadow-calibrated"
  record_name: "mtcps_xau_shadow_calibrated"
  symbol_config:
    XAUUSDT:
      atr_abs_min: 0.00153
      atr_fallback_percent: 0.317
  risk:
    dynamic_atr:
      enabled: true
      lookback_hours: 720
      min_history_count: 30
      cache_ttl_seconds: 3600
```

这里的 `0.00153` 和 `0.317` 仅用于先让结构测试进入红灯阶段；Task 5 会由部署前快照的实际 P25/P50 替换并冻结，禁止以这两个设计期估算值上线。

- [ ] **Step 2: 写 profile 差异白名单和策略映射失败测试**

创建 `tests/test_strategies/test_xau_calibrated_profile.py`，先写：

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from strategies.btc_eth.strategy import BTCEthStrategy as OriginalBTCEthStrategy
from strategies.btc_eth.shadow.collector import ShadowCollector, _build_shadow_strategy


ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "strategies" / "btc_eth" / "shadow" / "config"


def _load(name):
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def _closed_klines(count, step_ms, end_ms):
    rows = []
    for index in range(count):
        close_time = end_ms - (count - 1 - index) * step_ms
        price = 3900.0 + index
        rows.append({
            "open_time": close_time - step_ms + 1,
            "close_time": close_time,
            "open": price,
            "high": price + 12.0,
            "low": price - 8.0,
            "close": price + 3.0,
            "volume": 1000.0,
            "trades": 50,
        })
    return rows


def _closed_multi_timeframe_klines():
    scheduled = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    end_ms = int(scheduled.timestamp() * 1000) - 1
    return {
        "1h": _closed_klines(120, 3_600_000, end_ms),
        "4h": _closed_klines(100, 14_400_000, end_ms),
        "1d": _closed_klines(60, 86_400_000, end_ms),
    }


def _collector_for_single_profile(profile_id, config, strategy):
    collector = ShadowCollector(
        run_id="xau-calibrated-shadow-test",
        instance_id="mtcps_xau_calibrated_shadow",
        symbol="XAUUSDT",
        profiles=[profile_id],
        profile_configs={profile_id: config},
        repository=MagicMock(),
        kline_service=MagicMock(),
        binance_client=MagicMock(),
    )
    collector._strategies[profile_id] = strategy
    return collector


def _strip_allowed_differences(config):
    data = yaml.safe_load(yaml.safe_dump(config))
    data["instance"].pop("id", None)
    data["instance"].pop("profiles", None)
    data["shadow"].pop("cron", None)
    data["shadow"].pop("run_id_prefix", None)
    data["shadow"].pop("bootstrap_dynamic_atr_history", None)
    data["shadow"].pop("calibration_manifest", None)
    data["shadow"].pop("calibration_manifest_sha256", None)
    data["shadow"].pop("calibration_snapshot_sha256", None)
    data["shadow"].pop("calibration_cutoff_ms", None)
    data["strategy"].pop("name", None)
    data["strategy"].pop("version", None)
    data["strategy"].pop("record_name", None)
    symbol = data["strategy"]["symbol_config"]["XAUUSDT"]
    symbol.pop("atr_abs_min", None)
    symbol.pop("atr_fallback_percent", None)
    dynamic = data["strategy"]["risk"]["dynamic_atr"]
    dynamic.pop("min_samples", None)
    dynamic.pop("min_history_count", None)
    return data


def test_calibrated_profile_differs_from_original_only_by_whitelist():
    original = _load("xau_original.yaml")
    calibrated = _load("xau_calibrated.yaml")
    assert _strip_allowed_differences(calibrated) == _strip_allowed_differences(original)
    assert calibrated["instance"]["initial_virtual_equity_usdt"] == 300
    assert calibrated["strategy"]["risk"]["dynamic_atr"]["min_history_count"] == 30
    assert "min_samples" not in calibrated["strategy"]["risk"]["dynamic_atr"]


def test_calibrated_profile_uses_original_strategy_class():
    config = _load("xau_calibrated.yaml")
    strategy = _build_shadow_strategy(
        profile_id="xau_calibrated",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy._analyze_stationary_signal = AsyncMock(return_value=(None, "评分不足"))
    assert isinstance(strategy, OriginalBTCEthStrategy)
    assert strategy.execution_mode == "shadow_batch"
    assert strategy.real_ordering_enabled is False
    assert strategy.state_reporting_enabled is False


def test_fallback_atr_percent_reaches_dynamic_filter():
    config = _load("xau_calibrated.yaml")
    strategy = _build_shadow_strategy(
        profile_id="xau_calibrated",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy._analyze_stationary_signal = AsyncMock(return_value=(None, "评分不足"))
    override = strategy.atr_filter.symbol_overrides["XAUUSDT"]
    expected = config["strategy"]["symbol_config"]["XAUUSDT"]["atr_fallback_percent"]
    assert override["fallback_atr_percent"] == expected
```

- [ ] **Step 3: 运行测试确认映射和 fallback 失败**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_calibrated_profile.py -q
```

Expected: 未注册 `xau_calibrated`，并且 fallback 未进入 `symbol_overrides`。

- [ ] **Step 4: 最小实现 profile 映射和 fallback 映射**

在 `strategies/btc_eth/shadow/collector.py` 修改显式映射：

```python
_PROFILE_TO_STRATEGY_CLASS: Dict[str, Any] = {
    "xau_original": OriginalBTCEthStrategy,
    "xau_aggressive": AggressiveBTCEthStrategy,
    "xau_calibrated": OriginalBTCEthStrategy,
}
```

在 `strategies/btc_eth/strategy.py` 构造 symbol override 时增加：

```python
if 'atr_fallback_percent' in cfg:
    override['fallback_atr_percent'] = cfg['atr_fallback_percent']
```

不要改 `DynamicATRFilter` 对 `min_samples` 的读取逻辑；校准 YAML 直接使用正确键 `min_history_count`。

- [ ] **Step 5: 运行 profile 与原 shadow 回归**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_calibrated_profile.py \
  tests/test_strategies/test_shadow_collector.py -q
```

Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add strategies/btc_eth/shadow/config/xau_calibrated.yaml \
  strategies/btc_eth/shadow/collector.py \
  strategies/btc_eth/strategy.py \
  tests/test_strategies/test_xau_calibrated_profile.py
git commit -m "feat(shadow): register isolated XAU calibrated profile"
```

---

### Task 4: 在线确定性初始化 ATR 历史并记录阈值证据

**Files:**
- Modify: `strategies/btc_eth/shadow/collector.py`
- Modify: `tests/test_strategies/test_xau_calibrated_profile.py`
- Modify: `tests/test_strategies/test_shadow_collector.py`

- [ ] **Step 1: 写真实 `_analyze_profile` 路径的失败测试**

在 `tests/test_strategies/test_xau_calibrated_profile.py` 增加：

```python
@pytest.mark.asyncio
async def test_calibrated_profile_rebuilds_atr_history_each_decision(monkeypatch):
    config = _load("xau_calibrated.yaml")
    strategy = _build_shadow_strategy(
        profile_id="xau_calibrated",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy._analyze_stationary_signal = AsyncMock(return_value=(None, "评分不足"))
    collector = _collector_for_single_profile("xau_calibrated", config, strategy)
    multi = _closed_multi_timeframe_klines()
    scheduled = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)

    first = await collector._analyze_profile(
        profile_id="xau_calibrated",
        strategy=strategy,
        multi_klines=multi,
        close_times={key: rows[-1]["close_time"] for key, rows in multi.items()},
        scheduled_at=scheduled,
    )
    first_history = list(strategy.atr_filter._atr_history["XAUUSDT"])

    strategy.atr_filter._atr_history["XAUUSDT"].clear()
    second = await collector._analyze_profile(
        profile_id="xau_calibrated",
        strategy=strategy,
        multi_klines=multi,
        close_times={key: rows[-1]["close_time"] for key, rows in multi.items()},
        scheduled_at=scheduled,
    )
    second_history = list(strategy.atr_filter._atr_history["XAUUSDT"])

    assert first_history == second_history
    assert len(first_history) >= 30
    assert first[0]["feature_snapshot"]["atr_filter"]["history_count"] >= 30
    assert first[0]["feature_snapshot"]["atr_filter"] == second[0]["feature_snapshot"]["atr_filter"]


@pytest.mark.asyncio
async def test_original_profile_does_not_use_deterministic_bootstrap(monkeypatch):
    config = _load("xau_original.yaml")
    strategy = _build_shadow_strategy(
        profile_id="xau_original",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy._analyze_stationary_signal = AsyncMock(return_value=(None, "评分不足"))
    initialize = MagicMock(wraps=strategy.atr_filter.initialize_history)
    monkeypatch.setattr(strategy.atr_filter, "initialize_history", initialize)
    collector = _collector_for_single_profile("xau_original", config, strategy)
    await collector._analyze_profile(
        profile_id="xau_original",
        strategy=strategy,
        multi_klines=_closed_multi_timeframe_klines(),
        close_times={"1h": 1, "4h": 1, "1d": 1},
        scheduled_at=datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc),
    )
    initialize.assert_not_called()
```

上述 helper 生成 120 根 1h、100 根 4h、60 根 1d 闭合 K 线，并让最后一根 1h `close_time < scheduled_at`。

- [ ] **Step 2: 运行测试确认校准 history 未初始化**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_calibrated_profile.py \
  -k 'rebuilds_atr_history or original_profile_does_not_use' -q
```

Expected: 校准 profile 的 history 不足 30 或 `feature_snapshot` 没有 `atr_filter`。

- [ ] **Step 3: 实现仅校准 profile 生效的 bootstrap helper**

在 `collector.py` 增加导入：

```python
import pandas as pd

from .atr_calibration import CalibrationError, prepare_runtime_atr_history
```

增加方法：

```python
def _bootstrap_atr_history(
    self,
    *,
    profile_id: str,
    strategy: Any,
    multi_klines: Dict[str, List[Dict]],
    scheduled_at: datetime,
) -> int:
    shadow_cfg = self.profile_configs[profile_id].get("shadow", {})
    if not shadow_cfg.get("bootstrap_dynamic_atr_history", False):
        return 0
    if strategy.atr_filter is None or not strategy.atr_filter.enabled:
        raise CalibrationError("calibrated profile requires enabled atr_filter")
    atr_values, close_prices = prepare_runtime_atr_history(
        multi_klines.get("1h", []),
        scheduled_at=scheduled_at,
    )
    count = strategy.atr_filter.initialize_history(
        self.symbol,
        atr_values,
        close_prices,
    )
    if count < strategy.atr_filter.min_history_count:
        raise CalibrationError(
            f"atr_history_below_min:{count}<{strategy.atr_filter.min_history_count}"
        )
    return count
```

在 `_analyze_profile` 的 `_build_indicators` 成功后、市场状态识别前调用；异常返回 `analysis_error`，reason code 为 `atr_bootstrap_error`，不得回退 ETH 默认值。

- [ ] **Step 4: 生成 ATR 审计快照并附加到 candidate/no_signal**

在 `collector.py` 增加：

```python
def _atr_filter_snapshot(
    self,
    strategy: Any,
    indicators: Dict[str, Any],
    current_close: float,
) -> Dict[str, Any] | None:
    if strategy.atr_filter is None or not strategy.atr_filter.enabled:
        return None
    atr = float(indicators["1h"]["ATR"].iloc[-1])
    adx_series = indicators.get("1d", {}).get("ADX")
    adx_value = (
        float(adx_series.iloc[-1])
        if adx_series is not None and not pd.isna(adx_series.iloc[-1])
        else 0.0
    )
    if adx_value == 0.0:
        adx_4h = indicators.get("4h", {}).get("ADX")
        if adx_4h is not None and not pd.isna(adx_4h.iloc[-1]):
            adx_value = float(adx_4h.iloc[-1])
    final_threshold, info = strategy.atr_filter.get_dynamic_min_atr_percent(
        self.symbol,
        adx_value,
    )
    return {
        "current_atr_percent": atr / current_close * 100,
        "base_threshold": info["base_threshold"],
        "final_threshold": final_threshold,
        "absolute_min": info["absolute_min"],
        "coefficient": info["coefficient"],
        "adx": adx_value,
        "history_count": info["history_count"],
    }
```

调用时固定传入 `float(multi_klines["1h"][-1]["close"])`，禁止从指标字典猜测价格。扩展 `_make_event` 的可选参数 `feature_snapshot`，并扩展 `_make_event_from_signal` 的可选参数 `atr_filter_snapshot`；candidate 的原有三个字段继续保留：

```python
"feature_snapshot": {
    "market_state": signal_components.get("market_state"),
    "atr": float(atr) if atr is not None else None,
    "price_change_1h": float(signal_components.get("price_change_1h", 0)),
    "atr_filter": atr_filter_snapshot,
},
```

no_signal 在指标已成功时也写入：

```python
feature_snapshot={"atr_filter": atr_filter_snapshot}
```

- [ ] **Step 5: 运行校准与原版回归**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_calibrated_profile.py \
  tests/test_strategies/test_shadow_collector.py \
  tests/test_strategies/test_shadow_no_trading_write.py -q
```

Expected: 全部通过；原版测试的事件字段仍兼容。

- [ ] **Step 6: 做两次变异验证并立即还原**

变异 A：临时把 `prepare_runtime_atr_history` 的 `range(len(frame) - 1)` 改为 `range(len(frame))`，运行：

```bash
python3 -m pytest tests/test_strategies/test_xau_atr_calibration.py \
  -k runtime_history_excludes_current_bar -q
```

Expected: FAIL，证明测试能发现“当前值被 bootstrap 后又被策略重复追加”。

变异 B：临时移除 `_bootstrap_atr_history` 调用，运行：

```bash
python3 -m pytest tests/test_strategies/test_xau_calibrated_profile.py \
  -k rebuilds_atr_history_each_decision -q
```

Expected: FAIL。用反向补丁逐字恢复两个文件，再运行同一命令确认 PASS。

- [ ] **Step 7: 提交**

```bash
git add strategies/btc_eth/shadow/collector.py \
  tests/test_strategies/test_xau_calibrated_profile.py \
  tests/test_strategies/test_shadow_collector.py
git commit -m "feat(shadow): rebuild calibrated ATR history deterministically"
```

---

### Task 5: 增加本地校准命令并冻结真实参数

**Files:**
- Create: `scripts/generate_xau_atr_calibration.py`
- Create: `strategies/btc_eth/shadow/config/calibration/xau_calibrated_atr_manifest.json`
- Modify: `strategies/btc_eth/shadow/config/xau_calibrated.yaml`
- Modify: `tests/test_strategies/test_xau_atr_calibration.py`

- [ ] **Step 1: 写 CLI 失败测试**

在 `tests/test_strategies/test_xau_atr_calibration.py` 增加 subprocess 测试，输入 60 根确定性 JSONL，执行：

```python
completed = subprocess.run(
    [
        sys.executable,
        str(ROOT / "scripts" / "generate_xau_atr_calibration.py"),
        "--input", str(source),
        "--snapshot-output", str(snapshot),
        "--manifest-output", str(manifest),
        "--profile-config", str(profile_config),
        "--cutoff-ms", str(rows[-1]["close_time"]),
        "--source", "test-fixture",
        "--git-commit", "abc123",
    ],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)
assert completed.returncode == 0, completed.stderr
payload = json.loads(manifest.read_text())
verify_manifest(payload, snapshot.read_bytes())
profile = yaml.safe_load(profile_config.read_text())
assert profile["strategy"]["symbol_config"]["XAUUSDT"]["atr_abs_min"] == payload["calibration"]["atr_abs_min"]
assert profile["shadow"]["calibration_manifest_sha256"] == payload["manifest_sha256"]
assert "atr_abs_min" in completed.stdout
assert "fallback_atr_percent" in completed.stdout
```

- [ ] **Step 2: 运行测试确认脚本不存在**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_atr_calibration.py -k cli -q
```

Expected: FAIL，脚本路径不存在。

- [ ] **Step 3: 实现只读输入、原子输出 CLI**

`scripts/generate_xau_atr_calibration.py` 必须：

1. 支持 JSON 数组和每行一个对象的 JSONL；
2. 调用 `build_calibration`，不重复实现 ATR；
3. 在目标文件同目录先写 `.tmp`，`fsync` 后 `os.replace`；
4. 如果目标文件已存在且内容不同，除非显式 `--replace`，否则退出码 2；
5. 支持 `--cutoff-ms max`，从输入中选择最大的已闭合 `close_time`；
6. 支持 `--git-commit auto`，用 `subprocess.run(["git", "rev-parse", "HEAD"], check=True)` 读取生成器版本；
7. 用同一 manifest 自动更新 `--profile-config` 的 P25/P50、两个哈希和 cutoff，禁止人工复制；
8. stdout 只输出可审计摘要 JSON。

主流程固定为：

```python
result = build_calibration(rows, cutoff_ms=args.cutoff_ms)
snapshot_bytes = canonical_snapshot_bytes(result.snapshot_rows)
manifest = result.to_manifest(
    source=args.source,
    git_commit=args.git_commit,
    snapshot_sha256=result.snapshot_sha256,
    generated_at=datetime.now(timezone.utc).isoformat(),
)
_atomic_write(args.snapshot_output, snapshot_bytes, replace=args.replace)
_atomic_write(
    args.manifest_output,
    (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    replace=args.replace,
)
profile = yaml.safe_load(args.profile_config.read_text(encoding="utf-8"))
symbol_cfg = profile["strategy"]["symbol_config"]["XAUUSDT"]
shadow_cfg = profile["shadow"]
symbol_cfg["atr_abs_min"] = manifest["calibration"]["atr_abs_min"]
symbol_cfg["atr_fallback_percent"] = manifest["calibration"]["fallback_atr_percent"]
shadow_cfg["calibration_manifest_sha256"] = manifest["manifest_sha256"]
shadow_cfg["calibration_snapshot_sha256"] = manifest["snapshot_sha256"]
shadow_cfg["calibration_cutoff_ms"] = manifest["cutoff_ms"]
_atomic_write(
    args.profile_config,
    yaml.safe_dump(profile, allow_unicode=True, sort_keys=False).encode("utf-8"),
    replace=True,
)
print(json.dumps({
    "snapshot_sha256": result.snapshot_sha256,
    "manifest_sha256": manifest["manifest_sha256"],
    "valid_rows": result.valid_rows,
    "atr_sample_count": result.atr_sample_count,
    "atr_abs_min": result.atr_abs_min,
    "fallback_atr_percent": result.p50_percent,
}, sort_keys=True))
```

- [ ] **Step 4: 运行 CLI 测试**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_atr_calibration.py -q
```

Expected: 全部通过。

- [ ] **Step 5: 从 kline-service 的 PostgreSQL 只读导出部署前数据**

在主工作区创建已被 Git 忽略的持久证据目录：

```bash
mkdir -p /Users/yl/vscode/Binance_quantitative_trading/data/research/xau_calibration
```

通过现有 SSH 访问在数据库容器内执行只读查询。kline-service 使用按品种/周期分表，目标表固定为 `kline_xauusdt_1h`，不是带 `symbol`/`interval` 列的统一表。查询固定为：

```sql
SELECT json_build_object(
  'open_time', (extract(epoch FROM open_time) * 1000)::bigint,
  'close_time', (extract(epoch FROM close_time) * 1000)::bigint,
  'open', open_price,
  'high', high_price,
  'low', low_price,
  'close', close_price,
  'volume', volume,
  'trades', trade_count
)
FROM kline_xauusdt_1h
WHERE close_time < date_trunc('hour', now())
ORDER BY close_time ASC;
```

把 psql 的每行 JSON 输出保存为 `/Users/yl/vscode/Binance_quantitative_trading/data/research/xau_calibration/xauusdt-1h-source.jsonl`。查询只允许 `SELECT`，不得创建临时表或修改生产数据。

导出后先验证：

```bash
python3 -c 'import json,sys; rows=[json.loads(x) for x in open(sys.argv[1]) if x.strip()]; assert len(rows)>=100; assert all(r["close_time"] for r in rows); print(len(rows))' /Users/yl/vscode/Binance_quantitative_trading/data/research/xau_calibration/xauusdt-1h-source.jsonl
```

Expected: 输出至少 `100`，无断言失败。该查询只读，不在服务器运行回测或参数搜索。

- [ ] **Step 6: 生成不可变快照与已跟踪 manifest**

以导出结果最后一个完整小时作为 cutoff，运行 CLI；snapshot 输出到主工作区持久证据目录，manifest 输出到仓库，并由 CLI 原子更新 profile：

```bash
python3 scripts/generate_xau_atr_calibration.py \
  --input /Users/yl/vscode/Binance_quantitative_trading/data/research/xau_calibration/xauusdt-1h-source.jsonl \
  --snapshot-output /Users/yl/vscode/Binance_quantitative_trading/data/research/xau_calibration/xauusdt-1h-snapshot.jsonl \
  --manifest-output strategies/btc_eth/shadow/config/calibration/xau_calibrated_atr_manifest.json \
  --profile-config strategies/btc_eth/shadow/config/xau_calibrated.yaml \
  --cutoff-ms max \
  --source kline-service-postgres \
  --git-commit auto
```

生成器版本和 cutoff 都由 CLI 从当前仓库与输入数据确定，不由人工估计或复制。

- [ ] **Step 7: 验证 CLI 已用 manifest 实际值冻结 profile**

读取 manifest 的：

```text
calibration.atr_abs_min
calibration.fallback_atr_percent
manifest_sha256
snapshot_sha256
cutoff_ms
```

CLI 必须已经把这五个确定值原子写入 `xau_calibrated.yaml`。人工不编辑这些字段；如果 YAML 与 manifest 不一致，删除未提交输出并修复生成器，再重新生成。

- [ ] **Step 8: 增加 YAML 与 manifest 绑定测试**

在校准测试中读取两个文件并断言：

```python
assert symbol_cfg["atr_abs_min"] == manifest["calibration"]["atr_abs_min"]
assert symbol_cfg["atr_fallback_percent"] == manifest["calibration"]["fallback_atr_percent"]
assert shadow_cfg["calibration_manifest_sha256"] == manifest["manifest_sha256"]
assert shadow_cfg["calibration_snapshot_sha256"] == manifest["snapshot_sha256"]
assert shadow_cfg["calibration_cutoff_ms"] == manifest["cutoff_ms"]
```

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_atr_calibration.py \
  tests/test_strategies/test_xau_calibrated_profile.py -q
```

Expected: 全部通过。

- [ ] **Step 9: 提交脚本、manifest 和冻结 profile**

```bash
git add scripts/generate_xau_atr_calibration.py \
  strategies/btc_eth/shadow/config/calibration/xau_calibrated_atr_manifest.json \
  strategies/btc_eth/shadow/config/xau_calibrated.yaml \
  tests/test_strategies/test_xau_atr_calibration.py \
  tests/test_strategies/test_xau_calibrated_profile.py
git commit -m "feat(shadow): freeze XAU ATR calibration evidence"
```

---

### Task 6: 增加独立 Compose 服务和部署守护

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.github/workflows/deploy.yml`
- Create: `tests/test_strategies/test_xau_calibrated_compose.py`
- Modify: `tests/test_strategies/test_shadow_compose_config_mount.py`

- [ ] **Step 1: 写 Compose 安全边界失败测试**

创建 `tests/test_strategies/test_xau_calibrated_compose.py`：

```python
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICE = COMPOSE["services"]["mtcps-xau-calibrated-shadow"]


def _env_map(service):
    return dict(item.split("=", 1) for item in service["environment"])


def test_calibrated_service_is_isolated_and_read_only():
    env = _env_map(SERVICE)
    assert SERVICE["container_name"] == "trading_system-mtcps-xau-calibrated-shadow"
    assert SERVICE["command"] == ["python", "-m", "strategies.btc_eth.shadow.main"]
    assert env["INSTANCE_ID"] == "mtcps_xau_calibrated_shadow"
    assert env["SHADOW_PROFILES"] == "xau_calibrated"
    assert env["SHADOW_RUN_ID_PREFIX"] == "xau-calibrated-shadow"
    assert env["SHADOW_CRON"] == "20 * * * *"
    assert env["EXECUTION_MODE"] == "shadow_batch"
    assert env["REAL_ORDERING_ENABLED"] == "false"
    assert env["STATE_REPORTING_ENABLED"] == "false"
    assert env["CAPITAL_STRATEGY_ID"] == ""
    assert env["CIRCUIT_BREAKER_POOL"] == ""


def test_calibrated_service_has_independent_runtime_paths():
    volumes = SERVICE["volumes"]
    assert "./logs/mtcps_xau_calibrated_shadow:/app/logs" in volumes
    assert "./data/mtcps_xau_calibrated_shadow:/app/data" in volumes
    assert all("strategies/btc_eth/shadow" not in volume for volume in volumes)


def test_deploy_health_gate_includes_calibrated_container():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    assert "trading_system-mtcps-xau-calibrated-shadow" in workflow
```

- [ ] **Step 2: 运行测试确认服务不存在**

Run:

```bash
python3 -m pytest tests/test_strategies/test_xau_calibrated_compose.py -q
```

Expected: collection error 或 key error，Compose 尚无该服务。

- [ ] **Step 3: 新增 Compose 服务**

在 `mtcps-xau-shadow` 后新增：

```yaml
  mtcps-xau-calibrated-shadow:
    image: ghcr.io/bearflower/trading-btc-eth:latest
    container_name: trading_system-mtcps-xau-calibrated-shadow
    restart: unless-stopped
    command: ["python", "-m", "strategies.btc_eth.shadow.main"]
    env_file:
      - .env
    environment:
      - STRATEGY_NAME=btc_eth
      - INSTANCE_ID=mtcps_xau_calibrated_shadow
      - SHADOW_PROFILES=xau_calibrated
      - SHADOW_RUN_ID_PREFIX=xau-calibrated-shadow
      - SHADOW_CRON=20 * * * *
      - EXECUTION_MODE=shadow_batch
      - STATE_REPORTING_ENABLED=false
      - REAL_ORDERING_ENABLED=false
      - CAPITAL_STRATEGY_ID=
      - CIRCUIT_BREAKER_POOL=
      - DATABASE_URL=postgresql://trading_user:${DATABASE_PASSWORD:-trading_password_2024}@postgres:5432/trading_platform?schema=research
    volumes:
      - ./logs/mtcps_xau_calibrated_shadow:/app/logs
      - ./data/mtcps_xau_calibrated_shadow:/app/data
    networks:
      - trading-network
    depends_on:
      postgres:
        condition: service_healthy
      kline-service:
        condition: service_healthy
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

- [ ] **Step 4: 更新 CI 容器清单和遮蔽目录守护**

在 `.github/workflows/deploy.yml` 的 `RESIDUAL_CANDIDATES` 和长期运行健康循环中都加入：

```text
trading_system-mtcps-xau-calibrated-shadow
```

把 `test_shadow_compose_config_mount.py` 的单一服务常量改成：

```python
SERVICE_NAMES = ("mtcps-xau-shadow", "mtcps-xau-calibrated-shadow")
```

测试参数化两个服务，确认都不存在 shadow/config bind mount。

- [ ] **Step 5: 验证 Compose 与静态测试**

Run:

```bash
docker compose config --quiet
python3 -m pytest tests/test_strategies/test_xau_calibrated_compose.py \
  tests/test_strategies/test_shadow_compose_config_mount.py \
  tests/test_strategies/test_shadow_image_dependencies.py -q
```

Expected: Compose 命令退出 0，所有测试通过。

- [ ] **Step 6: 提交**

```bash
git add docker-compose.yml .github/workflows/deploy.yml \
  tests/test_strategies/test_xau_calibrated_compose.py \
  tests/test_strategies/test_shadow_compose_config_mount.py
git commit -m "feat(deploy): add isolated XAU calibrated shadow service"
```

---

### Task 7: 全量回归、规范审查与文档同步

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.trae/rules/deployment.md`
- Modify: `docs/README.md`
- Create: `docs/knowledge/decisions/Binance_quantitative_trading-20261027-xau-calibrated-shadow-isolation.md`
- Create or Modify: `docs/knowledge/changelog/2026-10-27.md`
- Modify: `docs/knowledge/INDEX.md`

- [ ] **Step 1: 运行影子与回放全套测试**

Run:

```bash
python3 -m pytest \
  tests/test_strategies/test_shadow_collector.py \
  tests/test_strategies/test_shadow_no_trading_write.py \
  tests/test_strategies/test_shadow_image_dependencies.py \
  tests/test_strategies/test_shadow_compose_config_mount.py \
  tests/test_strategies/test_xau_atr_calibration.py \
  tests/test_strategies/test_xau_calibrated_profile.py \
  tests/test_strategies/test_xau_calibrated_compose.py \
  tests/test_shadow_replay -q
```

Expected: 全部通过，无 xfail、xpass、error。

- [ ] **Step 2: 运行静态检查**

Run:

```bash
python3 -m py_compile \
  strategies/btc_eth/shadow/atr_calibration.py \
  strategies/btc_eth/shadow/instance.py \
  strategies/btc_eth/shadow/main.py \
  strategies/btc_eth/shadow/collector.py \
  scripts/generate_xau_atr_calibration.py
git diff --check
docker compose config --quiet
```

Expected: 三条命令均退出 0。

- [ ] **Step 3: 做配置差异审计**

运行 profile 白名单测试，并额外打印标准化后的差异：

```bash
python3 -m pytest tests/test_strategies/test_xau_calibrated_profile.py \
  -k differs_from_original_only_by_whitelist -q
git diff -- strategies/btc_eth/shadow/config/xau_original.yaml \
  strategies/btc_eth/shadow/config/xau_calibrated.yaml
```

Expected: 只有 instance/shadow 身份字段、策略展示名、ATR 校准字段和正确的 `min_history_count` 不同。

- [ ] **Step 4: 更新文档**

文档必须明确写入：

- 展示名为“MTPCS 黄金校准版（XAU）”，技术标识保持英文；
- 基于 MTCPS 原版，只校准 ATR；
- 300 USDT 独立虚拟权益；
- 每小时第 20 分钟运行，首个干净事件起独立八周；
- 停止命令 `docker compose stop mtcps-xau-calibrated-shadow`；
- `min_samples` 命名问题不做全局兼容，另列技术债；
- 现有原版/激进版观察期不重置，报告按同窗口比较。

- [ ] **Step 5: 自审计划与实现的一致性**

Run:

```bash
rg -n "xau_calibrated|mtcps_xau_calibrated_shadow|xau-calibrated-shadow" \
  strategies/btc_eth/shadow docker-compose.yml .github/workflows/deploy.yml \
  tests/test_strategies CLAUDE.md .trae/rules/deployment.md docs
```

Expected: 每个技术标识拼写一致，没有 `mtcps_xau_shadow` 被误用于校准配置或客户端。

- [ ] **Step 6: 提交文档**

```bash
git add CLAUDE.md .trae/rules/deployment.md docs/README.md \
  docs/knowledge/decisions/Binance_quantitative_trading-20261027-xau-calibrated-shadow-isolation.md \
  docs/knowledge/changelog/2026-10-27.md docs/knowledge/INDEX.md
git commit -m "docs: document XAU calibrated shadow operations"
```

---

### Task 8: 部署前复审与明确授权门禁

**Files:**
- No code changes

- [ ] **Step 1: 比较分支与基线**

Run:

```bash
git status --short
git log --oneline --decorate main..HEAD
git diff --stat main...HEAD
git diff --check main...HEAD
```

Expected: 工作区干净；差异仅包含本计划列出的文件。

- [ ] **Step 2: 独立代码审查**

审查重点：

1. 原版和激进版无配置改动；
2. 校准 profile 只改变 ATR 白名单字段；
3. 未来 K 线不能进入 runtime history；
4. 校准失败为 `analysis_error`，不会降级使用 0.6%；
5. 新服务没有 Binance 写权限路径；
6. manifest 和 YAML 数值、哈希严格绑定；
7. 新容器加入 CI 健康清单；
8. 回退不会停止任何既有服务。

- [ ] **Step 3: 报告不可避免的部署代价并取得授权**

当前 CI 在服务器执行 `docker compose pull` 后统一运行 `docker compose up -d --remove-orphans`。btc-eth 镜像变化会使同镜像的 `btc-eth-strategy` 和 `mtcps-xau-shadow` 被 Compose recreate；因此，在 push/merge 前必须向用户报告：

- btc-eth 镜像需要重建；
- 生产 btc-eth 与现有 XAU shadow 可能短时重启；
- 原版/激进版配置哈希和观察期口径不变，但可能形成新的物理 run 边界；
- 报告层按等价配置链聚合，并按 `(profile_id, symbol, scheduled_at)` 去重。

只有用户再次明确授权部署，才执行 Task 9。未授权时保持分支，不 push、不部署。

---

### Task 9: Actions 部署与上线五层验证

**Files:**
- No code changes unless verification finds a defect

- [ ] **Step 1: 获得授权后合并并 push main**

按项目分支完成流程将 `codex/xau-calibrated-shadow` 合并到 `main`，确认主工作区其他未提交改动未进入提交，然后：

```bash
git push origin main
```

Expected: GitHub Actions deploy workflow 启动。

- [ ] **Step 2: 观察 Actions 到终态**

确认 btc-eth build 和 deploy job 均成功，记录 Run 编号、commit SHA 与 `DEPLOY_ID`。任一 job 失败即停止，不进行手工绕过上线。

- [ ] **Step 3: 完成五层部署验证**

在服务器验证：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect trading_system-mtcps-xau-calibrated-shadow --format '{{.Image}}'
docker exec trading_system-mtcps-xau-calibrated-shadow cat /app/VERSION
docker logs --tail 200 trading_system-mtcps-xau-calibrated-shadow
docker compose ps
```

Expected:

- 新容器 running/healthy；
- 镜像 ID 与本轮构建一致；
- VERSION 的 commit 和 DEPLOY_ID 与 Actions 一致；
- 关键配置、manifest、`collector.py` 的本地 MD5 与容器内 MD5 一致；
- 日志显示只加载 `xau_calibrated`、instance 为 `mtcps_xau_calibrated_shadow`、cron 为 `20 * * * *`；
- 所有既有长期运行容器仍 healthy。

- [ ] **Step 4: 验证首个干净决策事件**

查询 `research.shadow_runs` 和 `research.shadow_decision_events`，要求：

```text
instance_id = mtcps_xau_calibrated_shadow
profile_id = xau_calibrated
symbol = XAUUSDT
data_quality_reason IS NULL
feature_snapshot.atr_filter.history_count >= 30
feature_snapshot.atr_filter.final_threshold IS NOT NULL
```

同时确认：

- run_id 以 `xau-calibrated-shadow-` 开头；
- `config_hash` 等于容器内冻结配置哈希；
- ATR 门槛与同一批 K 线本地复算结果一致；
- `trading.*` 没有该 instance/profile 的写入；
- 原版/激进版最近一个调度周期仍各有且只有一条有效事件。

- [ ] **Step 5: 记录八周起点与回退信息**

以首个满足 Step 4 的 `scheduled_at` 为观察起点，结束时间为起点加 56 天。更新交接文件（位于 `.gitignore`）并记录：run_id、起止时间、commit、DEPLOY_ID、manifest hash、snapshot hash、回退命令。

- [ ] **Step 6: 失败时只回退新容器**

若校准实例自身异常而既有服务正常：

```bash
docker compose stop mtcps-xau-calibrated-shadow
```

保留 `research` 数据和日志用于审计，不删除表、不停止 `mtcps-xau-shadow`，并在修复前不宣布观察期开始。

---

## 最终验收清单

- [ ] 校准 manifest 与 profile 数值、哈希一致。
- [ ] 校准 profile 与原版仅存在批准的差异。
- [ ] 同输入在首次运行、重试、重启后产生相同 ATR history 与门槛。
- [ ] 原版、激进版和生产策略行为测试保持通过。
- [ ] 校准实例独立使用 300 USDT 虚拟权益和独立 run ID。
- [ ] 校准实例只写 `research`，所有 Binance 写接口 fail closed。
- [ ] 首个干净事件已落库并记录独立八周起止时间。
- [ ] 现有原版/激进版数据未减少、配置未变化、运行正常。
- [ ] 回退命令经过静态核对，且只影响新容器。
