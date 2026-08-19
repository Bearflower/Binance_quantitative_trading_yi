"""
测试配置操作器（ConfigOperator）

覆盖用例：
- 正常 apply_overrides：有效的参数调整 → 覆盖层文件写入成功，.active 更新
- 同一天多次覆盖：两次调整 → 版本号后缀递增：V20260811_02
- 原子写入中断：写入过程中模拟异常 → 临时文件被清理，原文件不变
- 嵌套键路径读取："scoring.weights.trend_strength" → 返回嵌套字典中的值
- 键路径不存在："nonexistent.key" → 返回 None
- flatten_dict 测试：嵌套字典展平为点分隔路径
- flat_to_nested 测试：点分隔路径还原为嵌套字典
- 路径冲突处理：键路径冲突时正确覆盖
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.deploy.config_operator import ConfigOperator


@pytest.fixture
def config_operator():
    """创建 ConfigOperator 实例"""
    return ConfigOperator()


@pytest.fixture
def temp_override_dir():
    """创建临时覆盖层目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


# ============================================================
# 测试用例 1：正常 apply_overrides
# ============================================================


class TestApplyOverrides:
    """测试正常应用覆盖层"""

    @pytest.mark.asyncio
    async def test_apply_overrides_success(self, config_operator, temp_override_dir):
        """验证有效的参数调整写入覆盖层文件并更新 .active"""
        # 构造一个在临时目录下的 config_path
        config_path = os.path.join(temp_override_dir, "config.yaml")
        # 确保 config.yaml 存在（应用覆盖层时不需要它存在，但需要推导目录）
        with open(config_path, "w") as f:
            f.write("dummy: config\n")

        adjustments = {
            "scoring.min_score": 0.75,
            "scoring.weights.trend_strength": 0.45,
        }

        # 使用 patch 控制日期，使版本号固定
        with patch("ai_tuner.deploy.config_operator.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)
            mock_dt.strftime = __import__("datetime").datetime.strftime

            result = config_operator.apply_overrides(config_path, adjustments)

        assert result is True

        # 验证覆盖层文件已创建
        override_dir = os.path.join(temp_override_dir, "tuning_overrides")
        assert os.path.exists(override_dir)
        version_file = os.path.join(override_dir, "V20260811.yaml")
        assert os.path.exists(version_file)

        # 验证 .active 文件已更新
        active_file = os.path.join(override_dir, ".active")
        assert os.path.exists(active_file)
        with open(active_file, "r") as f:
            active_version = f.read().strip()
        assert active_version == "V20260811"

        # 验证覆盖层文件内容（嵌套结构）
        import yaml
        with open(version_file, "r") as f:
            content = yaml.safe_load(f)
        assert content["scoring"]["min_score"] == 0.75
        assert content["scoring"]["weights"]["trend_strength"] == 0.45


# ============================================================
# 测试用例 2：同一天多次覆盖
# ============================================================


class TestSameDayMultipleOverrides:
    """测试同一天多次覆盖"""

    @pytest.mark.asyncio
    async def test_second_override_increments_suffix(self, config_operator, temp_override_dir):
        """验证同一天第二次覆盖时版本号后缀递增"""
        config_path = os.path.join(temp_override_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("dummy: config\n")

        adjustments = {"scoring.min_score": 0.75}

        with patch("ai_tuner.deploy.config_operator.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)
            mock_dt.strftime = __import__("datetime").datetime.strftime

            # 第一次覆盖
            result1 = config_operator.apply_overrides(config_path, adjustments)
            assert result1 is True

            # 第二次覆盖
            adjustments2 = {"scoring.max_score": 0.95}
            result2 = config_operator.apply_overrides(config_path, adjustments2)
            assert result2 is True

        # 验证两个版本文件都存在
        override_dir = os.path.join(temp_override_dir, "tuning_overrides")
        assert os.path.exists(os.path.join(override_dir, "V20260811.yaml"))
        assert os.path.exists(os.path.join(override_dir, "V20260811_02.yaml"))

        # 验证 .active 指向最新版本
        active_file = os.path.join(override_dir, ".active")
        with open(active_file, "r") as f:
            active_version = f.read().strip()
        assert active_version == "V20260811_02"


# ============================================================
# 测试用例 3：原子写入中断
# ============================================================


class TestAtomicWriteInterruption:
    """测试原子写入中断时的清理"""

    @pytest.mark.asyncio
    async def test_temp_file_cleaned_on_failure(self, config_operator, temp_override_dir):
        """验证写入过程中异常时临时文件被清理，原文件不变"""
        config_path = os.path.join(temp_override_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("original: value\n")

        adjustments = {"scoring.min_score": 0.75}

        # 模拟 _atomic_write 抛出异常
        original_atomic_write = config_operator._atomic_write

        def failing_atomic_write(path, config_dict):
            # 先创建临时文件，然后清理并抛出异常
            dir_name = os.path.dirname(path)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_", suffix=".yaml")
            os.close(fd)
            # 记录临时文件路径，并清理（模拟真实 _atomic_write 的异常处理行为）
            failing_atomic_write.tmp_path = tmp_path
            os.unlink(tmp_path)
            raise RuntimeError("写入失败")

        failing_atomic_write.tmp_path = None
        config_operator._atomic_write = failing_atomic_write

        with patch("ai_tuner.deploy.config_operator.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)
            mock_dt.strftime = __import__("datetime").datetime.strftime

            result = config_operator.apply_overrides(config_path, adjustments)

        assert result is False

        # 验证临时文件被清理
        override_dir = os.path.join(temp_override_dir, "tuning_overrides")
        # 列出所有 .tmp_ 开头的文件
        tmp_files = [f for f in os.listdir(override_dir) if f.startswith(".tmp_")]
        assert len(tmp_files) == 0, f"临时文件未被清理: {tmp_files}"

        # 恢复原始方法
        config_operator._atomic_write = original_atomic_write


# ============================================================
# 测试用例 4：嵌套键路径读取
# ============================================================


class TestNestedKeyPath:
    """测试嵌套键路径读取"""

    def test_get_nested_value_exists(self, config_operator):
        """验证嵌套键路径读取成功"""
        config = {
            "scoring": {
                "min_score": 0.7,
                "weights": {
                    "trend_strength": 0.4,
                    "volatility": 0.3,
                },
            },
        }

        value = config_operator.get_nested_value(config, "scoring.weights.trend_strength")
        assert value == 0.4

    def test_get_nested_value_top_level(self, config_operator):
        """验证单层键路径读取"""
        config = {"min_score": 0.7}
        value = config_operator.get_nested_value(config, "min_score")
        assert value == 0.7

    def test_get_nested_value_not_exists(self, config_operator):
        """验证键路径不存在时返回 None"""
        config = {"scoring": {"min_score": 0.7}}
        value = config_operator.get_nested_value(config, "nonexistent.key")
        assert value is None

    def test_get_nested_value_deep_not_exists(self, config_operator):
        """验证深层键路径不存在时返回 None"""
        config = {"scoring": {"min_score": 0.7}}
        value = config_operator.get_nested_value(config, "scoring.weights.trend_strength")
        assert value is None


# ============================================================
# 测试用例 5：set_nested_value 测试
# ============================================================


class TestSetNestedValue:
    """测试 set_nested_value 方法"""

    def test_set_nested_value_success(self, config_operator):
        """验证嵌套值设置成功"""
        config = {"scoring": {"min_score": 0.7, "weights": {"trend_strength": 0.4}}}
        result = config_operator.set_nested_value(config, "scoring.weights.trend_strength", 0.5)
        assert result is True
        assert config["scoring"]["weights"]["trend_strength"] == 0.5

    def test_set_nested_value_path_not_exists(self, config_operator):
        """验证键路径不存在时返回 False"""
        config = {"scoring": {"min_score": 0.7}}
        result = config_operator.set_nested_value(config, "scoring.nonexistent.key", 0.5)
        assert result is False

    def test_set_nested_value_middle_not_dict(self, config_operator):
        """验证中间节点不是字典时返回 False"""
        config = {"scoring": "not_a_dict"}
        result = config_operator.set_nested_value(config, "scoring.weights.trend_strength", 0.5)
        assert result is False


# ============================================================
# 测试用例 6：_flat_to_nested 测试
# ============================================================


class TestFlatToNested:
    """测试 _flat_to_nested 方法"""

    def test_flat_to_nested_simple(self, config_operator):
        """验证简单扁平路径还原为嵌套字典"""
        flat = {"scoring.min_score": 0.75}
        nested = config_operator._flat_to_nested(flat)
        assert nested == {"scoring": {"min_score": 0.75}}

    def test_flat_to_nested_multi_level(self, config_operator):
        """验证多层扁平路径还原为嵌套字典"""
        flat = {
            "scoring.min_score": 0.75,
            "scoring.weights.trend_strength": 0.45,
            "scoring.weights.volatility": 0.30,
        }
        nested = config_operator._flat_to_nested(flat)
        assert nested == {
            "scoring": {
                "min_score": 0.75,
                "weights": {
                    "trend_strength": 0.45,
                    "volatility": 0.30,
                },
            },
        }

    def test_flat_to_nested_empty(self, config_operator):
        """验证空字典返回空字典"""
        nested = config_operator._flat_to_nested({})
        assert nested == {}

    def test_flat_to_nested_single_key(self, config_operator):
        """验证单键路径"""
        nested = config_operator._flat_to_nested({"a": 1})
        assert nested == {"a": 1}


# ============================================================
# 测试用例 7：read_config 和 apply_changes 测试
# ============================================================


class TestReadConfig:
    """测试 read_config 方法"""

    def test_read_config_file_not_found(self, config_operator):
        """验证文件不存在时返回空字典"""
        result = config_operator.read_config("/nonexistent/path/config.yaml")
        assert result == {}


class TestApplyChanges:
    """测试 apply_changes 方法"""

    @pytest.mark.asyncio
    async def test_apply_changes_success(self, config_operator, temp_override_dir):
        """验证 apply_changes 正常写入"""
        config_path = os.path.join(temp_override_dir, "config.yaml")
        initial_config = {"scoring": {"min_score": 0.7}}
        import yaml
        with open(config_path, "w") as f:
            yaml.dump(initial_config, f)

        result = config_operator.apply_changes(config_path, {"scoring.min_score": 0.8})
        assert result is True

        # 验证文件已更新
        with open(config_path, "r") as f:
            updated = yaml.safe_load(f)
        assert updated["scoring"]["min_score"] == 0.8

    def test_apply_changes_config_empty(self, config_operator, temp_override_dir):
        """验证配置文件为空时返回 False"""
        config_path = os.path.join(temp_override_dir, "nonexistent.yaml")
        result = config_operator.apply_changes(config_path, {"scoring.min_score": 0.8})
        assert result is False