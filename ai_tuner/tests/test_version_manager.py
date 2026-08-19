"""
测试版本管理器（VersionManager）

覆盖用例：
- 首次生成版本号：当天无版本文件 → 返回 V20260811
- 同一天第二次生成：当天已有 V20260811_01 → 返回 V20260811_02
- 版本号格式兼容：当天已有 V20260811-1（旧格式）→ 正确识别为已有版本，返回 V20260811_02
- 覆盖层目录不存在：目录路径无效 → 创建目录，返回基础版本号
- 非法后缀字符：目录中有 V20260811_abc.yaml → 跳过非法后缀，返回 V20260811_01
- 获取最新版本号：返回数值最大的版本号
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.deploy.version_manager import VersionManager


@pytest.fixture
def config():
    """基础配置"""
    return {}


@pytest.fixture
def version_manager(config):
    """创建 VersionManager 实例"""
    return VersionManager(config)


@pytest.fixture
def temp_override_dir():
    """创建临时覆盖层目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


# ============================================================
# 测试用例 1：首次生成版本号
# ============================================================


class TestFirstVersionGeneration:
    """测试首次生成版本号"""

    @patch("ai_tuner.deploy.version_manager.datetime")
    def test_first_version(self, mock_dt, version_manager, temp_override_dir):
        """验证当天无版本文件时返回基础版本号 V{YYYYMMDD}"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)

        # 使用相对路径，VersionManager 会拼接 project_root + 相对路径
        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.generate_new_version(strategy_config_path)
        assert version == "V20260811"


# ============================================================
# 测试用例 2：同一天第二次生成
# ============================================================


class TestSecondVersionSameDay:
    """测试同一天第二次生成版本号"""

    @patch("ai_tuner.deploy.version_manager.datetime")
    def test_second_version_with_suffix(self, mock_dt, version_manager, temp_override_dir):
        """验证当天已有 V20260811_01 时返回 V20260811_02"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)

        # 构造覆盖层目录（相对于 project_root）
        override_dir = os.path.join(temp_override_dir, "strategies", "btc_eth", "tuning_overrides")
        os.makedirs(override_dir, exist_ok=True)

        # 创建已存在的版本文件
        for v in ["V20260811.yaml", "V20260811_01.yaml"]:
            with open(os.path.join(override_dir, v), "w") as f:
                f.write("dummy: config\n")

        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.generate_new_version(strategy_config_path)
        assert version == "V20260811_02"


# ============================================================
# 测试用例 3：版本号格式兼容
# ============================================================


class TestVersionFormatCompatibility:
    """测试版本号格式兼容"""

    @patch("ai_tuner.deploy.version_manager.datetime")
    def test_old_format_dash(self, mock_dt, version_manager, temp_override_dir):
        """验证旧格式 V20260811-1 被正确识别"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)

        override_dir = os.path.join(temp_override_dir, "strategies", "btc_eth", "tuning_overrides")
        os.makedirs(override_dir, exist_ok=True)

        # 创建旧格式版本文件（使用 - 分隔符）
        for v in ["V20260811.yaml", "V20260811-1.yaml"]:
            with open(os.path.join(override_dir, v), "w") as f:
                f.write("dummy: config\n")

        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.generate_new_version(strategy_config_path)
        # 旧格式 -1 也被识别为已有版本，新版本应为 _02
        assert version == "V20260811_02"


# ============================================================
# 测试用例 4：覆盖层目录不存在
# ============================================================


class TestOverrideDirNotExists:
    """测试覆盖层目录不存在的情况"""

    @patch("ai_tuner.deploy.version_manager.datetime")
    def test_dir_not_exists(self, mock_dt, version_manager, temp_override_dir):
        """验证目录不存在时返回基础版本号"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)

        # 不创建任何目录，覆盖层目录不存在
        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.generate_new_version(strategy_config_path)
        # 目录不存在，返回基础版本号
        assert version == "V20260811"


# ============================================================
# 测试用例 5：非法后缀字符
# ============================================================


class TestInvalidSuffixCharacters:
    """测试非法后缀字符"""

    @patch("ai_tuner.deploy.version_manager.datetime")
    def test_skip_invalid_suffix(self, mock_dt, version_manager, temp_override_dir):
        """验证 V20260811_abc.yaml 被跳过，返回 V20260811_01"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 10, 0, 0)

        override_dir = os.path.join(temp_override_dir, "strategies", "btc_eth", "tuning_overrides")
        os.makedirs(override_dir, exist_ok=True)

        # 创建基础版本 + 非法后缀版本
        for v in ["V20260811.yaml", "V20260811_abc.yaml"]:
            with open(os.path.join(override_dir, v), "w") as f:
                f.write("dummy: config\n")

        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.generate_new_version(strategy_config_path)
        # _abc 是非法后缀，被跳过，新版本应为 _01
        assert version == "V20260811_01"


# ============================================================
# 测试用例 6：获取最新版本号
# ============================================================


class TestLatestVersionNumber:
    """测试获取最新版本号"""

    def test_get_latest_version_number(self, version_manager, temp_override_dir):
        """验证返回数值最大的版本号"""
        override_dir = os.path.join(temp_override_dir, "strategies", "btc_eth", "tuning_overrides")
        os.makedirs(override_dir, exist_ok=True)

        # 创建多个版本文件
        for v in ["V20260801.yaml", "V20260805.yaml", "V20260810.yaml", "V20260803.yaml"]:
            with open(os.path.join(override_dir, v), "w") as f:
                f.write("dummy: config\n")

        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        max_version = version_manager.get_latest_version_number(strategy_config_path)
        assert max_version == 20260810


# ============================================================
# 测试用例 7：get_active_version 测试
# ============================================================


class TestGetActiveVersion:
    """测试 get_active_version 方法"""

    def test_active_version_exists(self, version_manager, temp_override_dir):
        """验证 .active 文件存在时返回版本号"""
        override_dir = os.path.join(temp_override_dir, "strategies", "btc_eth", "tuning_overrides")
        os.makedirs(override_dir, exist_ok=True)

        # 创建 .active 文件
        with open(os.path.join(override_dir, ".active"), "w") as f:
            f.write("V20260804\n")

        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.get_active_version(strategy_config_path)
        assert version == "V20260804"

    def test_active_version_not_exists(self, version_manager, temp_override_dir):
        """验证 .active 文件不存在时返回 None"""
        # 不创建 .active 文件，也不创建覆盖层目录
        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.get_active_version(strategy_config_path)
        assert version is None

    def test_active_version_empty(self, version_manager, temp_override_dir):
        """验证 .active 文件为空时返回 None"""
        override_dir = os.path.join(temp_override_dir, "strategies", "btc_eth", "tuning_overrides")
        os.makedirs(override_dir, exist_ok=True)

        # 创建空的 .active 文件
        with open(os.path.join(override_dir, ".active"), "w") as f:
            f.write("")

        version_manager.project_root = temp_override_dir
        strategy_config_path = "strategies/btc_eth/config.yaml"

        version = version_manager.get_active_version(strategy_config_path)
        assert version is None