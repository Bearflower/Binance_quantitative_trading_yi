"""实例配置解析测试（§1 实例参数化）

逐键断言：
1. 缺省 instance 时逐键等于生产默认值
2. execution_mode=shadow_batch 时两开关被强制 false
3. 环境变量覆盖 config.yaml（ENV > config.yaml > 生产默认）
"""
import os
import pytest

from shared.instance_config import (
    parse_instance_config,
    InstanceConfig,
)


# ---------------------------------------------------------------------------
# 1. 缺省 instance：逐键等于生产默认值
# ---------------------------------------------------------------------------
def test_default_values_production_original():
    """原版 btc_eth 缺省 instance 时，所有键等于原版生产默认"""
    config: dict = {}  # 没有顶层 instance 键
    inst = parse_instance_config(
        config,
        default_id="btc_eth",
        default_capital_strategy_id="btc_eth",
        default_circuit_breaker_pool="mtpcs",
    )
    assert inst.id == "btc_eth"
    assert inst.execution_mode == "live"
    assert inst.capital_strategy_id == "btc_eth"
    assert inst.circuit_breaker_pool == "mtpcs"
    assert inst.state_reporting_enabled is True
    assert inst.real_ordering_enabled is True


def test_default_values_production_aggressive():
    """激进版 btc_eth_aggressive 缺省 instance 时，所有键等于激进版生产默认"""
    config: dict = {}
    inst = parse_instance_config(
        config,
        default_id="btc_eth_aggressive",
        default_capital_strategy_id="btc_eth_aggressive",
        default_circuit_breaker_pool="mtpcs",
    )
    assert inst.id == "btc_eth_aggressive"
    assert inst.execution_mode == "live"
    assert inst.capital_strategy_id == "btc_eth_aggressive"
    assert inst.circuit_breaker_pool == "mtpcs"
    assert inst.state_reporting_enabled is True
    assert inst.real_ordering_enabled is True


# ---------------------------------------------------------------------------
# 2. execution_mode=shadow_batch 强制关闭两开关
# ---------------------------------------------------------------------------
def test_shadow_batch_forces_switches_off():
    """execution_mode=shadow_batch 时，real_ordering_enabled 和
    state_reporting_enabled 必须为 False，无论 config.yaml 写什么"""
    config = {
        "instance": {
            "id": "mtcps_xau_shadow",
            "execution_mode": "shadow_batch",
            "state_reporting_enabled": True,   # 故意写 True
            "real_ordering_enabled": True,      # 故意写 True
        }
    }
    inst = parse_instance_config(config, default_id="btc_eth")
    assert inst.execution_mode == "shadow_batch"
    assert inst.real_ordering_enabled is False
    assert inst.state_reporting_enabled is False


def test_shadow_batch_env_override():
    """环境变量 EXECUTION_MODE=shadow_batch 同样强制关闭两开关"""
    config = {
        "instance": {
            "id": "btc_eth",
            "execution_mode": "live",           # config 写 live
            "state_reporting_enabled": True,
            "real_ordering_enabled": True,
        }
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("EXECUTION_MODE", "shadow_batch")
        inst = parse_instance_config(config, default_id="btc_eth")
    assert inst.execution_mode == "shadow_batch"
    assert inst.real_ordering_enabled is False
    assert inst.state_reporting_enabled is False


# ---------------------------------------------------------------------------
# 3. 环境变量优先级（ENV > config.yaml > 生产默认）
# ---------------------------------------------------------------------------
def test_env_overrides_config():
    """INSTANCE_ID env 覆盖 config.yaml"""
    config = {"instance": {"id": "from_config"}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("INSTANCE_ID", "from_env")
        inst = parse_instance_config(config, default_id="default")
    assert inst.id == "from_env"


def test_config_overrides_default():
    """config.yaml 覆盖生产默认"""
    config = {
        "instance": {
            "id": "from_config",
            "capital_strategy_id": "custom_strategy",
            "circuit_breaker_pool": "custom_pool",
        }
    }
    inst = parse_instance_config(config, default_id="default_id")
    assert inst.id == "from_config"
    assert inst.capital_strategy_id == "custom_strategy"
    assert inst.circuit_breaker_pool == "custom_pool"


def test_env_bool_values():
    """ENV 中布尔值支持 true/false/1/0/yes/no"""
    config: dict = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("STATE_REPORTING_ENABLED", "false")
        mp.setenv("REAL_ORDERING_ENABLED", "0")
        inst = parse_instance_config(config, default_id="btc_eth")
    assert inst.state_reporting_enabled is False
    assert inst.real_ordering_enabled is False
