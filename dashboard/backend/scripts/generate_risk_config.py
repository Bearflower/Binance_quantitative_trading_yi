#!/usr/bin/env python3
"""
风控阈值配置生成器（策略 config 为源 → 自动汇总生成 risk.yaml）

背景
----
此前风控上限（account_ratio_cap）在看板 risk.yaml 与各策略 config.yaml 各存一份，
存在"双维护不一致"隐患。为统一单一权威源，采用如下方案：

- **权威源**：各策略 `config.yaml`（策略侧一直读自己的配置，天然读得到、不依赖 DB）。
- **生成物**：部署前本地执行本脚本，读取各策略 config，汇总写入
  `dashboard/backend/config/risk.yaml` 的 `account_ratio_caps`。
- 看板只读生成物，人工改策略 config → 下次部署 risk.yaml 自动同步，零手写双份。

注意：
- 仅重写 `risk.account_ratio_caps`，其它告警阈值（occupancy_warning_ratio、
  consecutive_loss_days、daily_drawdown_pct、stop_loss_days_window）保持不变，
  避免误覆盖人工调整的告警参数。
- 使用 `shared/config_loader.load_strategy_config` 读取合并配置（含 tuning_overrides，
  保证读到当前生效值）。
- 原子写入：先写临时文件再 rename，避免写一半损坏配置。

用法
----
    python3 dashboard/backend/scripts/generate_risk_config.py [项目根目录]
    默认项目根目录为本文件所在目录向上三级（<repo>/dashboard/backend/scripts/.. => repo）
"""

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple

import structlog
import yaml

# 加入项目根目录到搜索路径，便于 import shared.config_loader
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from shared.config_loader import load_strategy_config  # noqa: E402

logger = structlog.get_logger()

# 北京时区
_BEIJING_TZ = timezone(timedelta(hours=8))

# 风控配置目标文件
RISK_YAML_REL = os.path.join("dashboard", "backend", "config", "risk.yaml")


# 各策略占用上限的取数路径与类型
#  - key_path: 读取 merged config 的嵌套路径
#  - cap_type: "ratio"（总持仓保证金占账户权益比例）| "absolute"（绝对 USDT 上限）
#  - config_rel: 相对项目根目录的配置文件（仅用于元数据展示）
_STRATEGY_CAPS_CONFIG: Dict[str, dict] = {
    "btc_eth": {
        "key_path": ("position_sizing", "total", "account_ratio_cap"),
        "cap_type": "ratio",
        "config_rel": os.path.join("strategies", "btc_eth", "config.yaml"),
    },
    "btc_eth_aggressive": {
        "key_path": ("position_sizing", "total", "account_ratio_cap"),
        "cap_type": "ratio",
        "config_rel": os.path.join("strategies", "btc_eth_aggressive", "config.yaml"),
    },
    "hrs": {
        "key_path": ("position_sizing", "total", "account_ratio_cap"),
        "cap_type": "ratio",
        "config_rel": os.path.join("strategies", "hrs", "config.yaml"),
    },
    "new_coin": {
        "key_path": ("trading", "total_position_margin_limit"),
        "cap_type": "absolute",
        "config_rel": os.path.join("strategies", "new_coin", "config.yaml"),
    },
}


def _read_nested_dict(node: Any, key_path: Tuple[str, ...]) -> Any:
    """按嵌套路径取值，任一层缺失返回 None"""
    cur = node
    for key in key_path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _collect_ratio_caps(strategy_dir: str, cfg: dict) -> Dict[str, Any]:
    """
    读取单个策略当前生效的占用上限

    Returns:
        读取成功返回 {"value": float, "type": str}；读取失败返回 None
    """
    merged = load_strategy_config(strategy_dir)
    value = _read_nested_dict(merged, cfg["key_path"])
    if value is None:
        logger.warning(
            "策略占用上限缺失，跳过该策略",
            strategy_dir=strategy_dir,
            key_path=list(cfg["key_path"]),
        )
        return None
    return {"value": float(value), "type": cfg["cap_type"]}


def build_risk_config(project_root: str) -> Dict[str, Any]:
    """
    构造新的 risk 配置（保留原有告警阈值，刷新 account_ratio_caps 与元数据）

    Args:
        project_root: 项目根目录（含 strategies/ 与 dashboard/）

    Returns:
        新的 risk 配置字典
    """
    risk_yaml_path = os.path.join(project_root, RISK_YAML_REL)
    existing = {}
    if os.path.exists(risk_yaml_path):
        try:
            with open(risk_yaml_path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("读取现有 risk.yaml 失败，将只保留自动生成字段", error=str(e))
            existing = {}

    risk = existing.get("risk", {})
    risk = risk if isinstance(risk, dict) else {}

    # 1. 汇总各策略占用上限
    account_ratio_caps: Dict[str, float] = {}
    source_configs: Dict[str, str] = {}
    for strategy_id, cfg in _STRATEGY_CAPS_CONFIG.items():
        strategy_dir = os.path.join(project_root, "strategies", strategy_id)
        collected = _collect_ratio_caps(strategy_dir, cfg)
        if collected is None:
            continue
        # 绝对额型（USDT）保持整数展示（如 new_coin: 150），比例型保留小数
        cap_value: Any
        if collected["type"] == "absolute":
            cap_value = int(collected["value"])
        else:
            cap_value = collected["value"]
        account_ratio_caps[strategy_id] = cap_value
        source_configs[strategy_id] = cfg["config_rel"]

    risk["account_ratio_caps"] = account_ratio_caps
    risk["generated_at"] = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")
    risk["source_configs"] = source_configs

    logger.info(
        "风控上限汇总完成",
        caps={k: v for k, v in account_ratio_caps.items()},
        missing=[s for s in _STRATEGY_CAPS_CONFIG if s not in source_configs],
    )
    return risk


def write_risk_config_atomic(project_root: str, risk: Dict[str, Any]) -> str:
    """
    原子写入 risk.yaml（先写临时文件再 rename）

    Returns:
        写入后的目标路径
    """
    risk_yaml_path = os.path.join(project_root, RISK_YAML_REL)
    os.makedirs(os.path.dirname(risk_yaml_path), exist_ok=True)

    data = {
        "risk": risk,
        # 顶部注释说明该文件为自动生成物
        "note": (
            "本文件 account_ratio_caps 由 generate_risk_config.py 自动生成，"
            "权威源为各策略 config.yaml，请勿手改 account_ratio_caps。"
        ),
    }

    dir_name = os.path.dirname(risk_yaml_path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix="risk.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, risk_yaml_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    logger.info("风控配置已自动生成", path=risk_yaml_path)
    return risk_yaml_path


def main() -> int:
    project_root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else _REPO_ROOT
    risk = build_risk_config(project_root)
    write_risk_config_atomic(project_root, risk)
    print(f"✅ 已生成 {os.path.join(project_root, RISK_YAML_REL)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())