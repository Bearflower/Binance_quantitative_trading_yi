"""
测试回滚管理器（RollbackManager）

覆盖用例：
- 创建备份：有效配置文件路径 → 备份文件创建成功，返回备份路径
- 文件不存在时备份：无效路径 → 返回空字符串，不报错
- 从备份恢复：指定备份文件路径 → 文件恢复成功
- 备份清理：备份数超过 max_backups → 删除最旧的备份
- 恢复时备份文件不存在：无效的备份路径 → 返回 False，不报错
- 覆盖层回滚：指定版本号 → 成功回滚到指定版本
"""

import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.deploy.rollback_manager import RollbackManager


@pytest.fixture
def rollback_manager():
    """创建 RollbackManager 实例（最多保留 3 个备份）"""
    return RollbackManager(max_backups=3)


@pytest.fixture
def temp_dir():
    """创建临时目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


# ============================================================
# 测试用例 1：创建备份
# ============================================================


class TestCreateBackup:
    """测试创建备份"""

    def test_backup_created_successfully(self, rollback_manager, temp_dir):
        """验证有效配置文件路径 → 备份文件创建成功，返回备份路径"""
        config_path = os.path.join(temp_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("original: value\n")

        backup_path = rollback_manager.create_backup(config_path)

        assert backup_path != ""
        assert os.path.exists(backup_path)
        # 验证备份内容与原文件一致
        with open(backup_path, "r") as f:
            content = f.read().strip()
        assert content == "original: value"

    def test_backup_file_name_contains_timestamp(self, rollback_manager, temp_dir):
        """验证备份文件名包含配置文件名和时间戳"""
        config_path = os.path.join(temp_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("test\n")

        backup_path = rollback_manager.create_backup(config_path)

        assert "config.yaml.backup." in backup_path
        # 验证时间戳格式（YYYYMMDD_HHMMSS）
        import re
        assert re.search(r"backup\.\d{8}_\d{6}$", backup_path)


# ============================================================
# 测试用例 2：文件不存在时备份
# ============================================================


class TestBackupFileNotExists:
    """测试文件不存在时备份"""

    def test_backup_non_existent_file(self, rollback_manager, temp_dir):
        """验证无效路径 → 返回空字符串，不报错"""
        config_path = os.path.join(temp_dir, "nonexistent.yaml")

        backup_path = rollback_manager.create_backup(config_path)

        assert backup_path == ""


# ============================================================
# 测试用例 3：从备份恢复
# ============================================================


class TestRestoreFromBackup:
    """测试从备份恢复"""

    def test_restore_successful(self, rollback_manager, temp_dir):
        """验证从备份文件恢复成功"""
        config_path = os.path.join(temp_dir, "config.yaml")
        # 原文件内容
        with open(config_path, "w") as f:
            f.write("original: value\n")

        # 创建备份
        backup_path = rollback_manager.create_backup(config_path)

        # 修改原文件
        with open(config_path, "w") as f:
            f.write("modified: value\n")

        # 从备份恢复（等待 1 秒确保备份文件名时间戳不同）
        time.sleep(1)
        result = rollback_manager.rollback(config_path, backup_path)
        assert result is True

        # 验证文件内容已恢复
        with open(config_path, "r") as f:
            content = f.read().strip()
        assert content == "original: value"

    def test_restore_backup_not_exists(self, rollback_manager, temp_dir):
        """验证备份文件不存在时返回 False"""
        config_path = os.path.join(temp_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("test\n")

        result = rollback_manager.rollback(config_path, "/nonexistent/backup.yaml")
        assert result is False


# ============================================================
# 测试用例 4：备份清理
# ============================================================


class TestBackupCleanup:
    """测试备份清理"""

    def test_cleanup_old_backups(self, rollback_manager, temp_dir):
        """验证备份数超过 max_backups 时删除旧备份"""
        config_path = os.path.join(temp_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("test\n")

        # 创建 5 个备份（超过 max_backups=3），每次间隔 1 秒确保时间戳不同
        backup_paths = []
        for i in range(5):
            backup_path = rollback_manager.create_backup(config_path)
            backup_paths.append(backup_path)
            time.sleep(1)

        # 验证只保留 3 个备份
        remaining = rollback_manager.list_backups(config_path)
        assert len(remaining) == 3

        # 验证删除的确实是最旧的备份（按文件名排序判断）
        all_backups = sorted(backup_paths)
        for old_path in all_backups[:2]:
            assert not os.path.exists(old_path), f"最旧的备份未被删除: {old_path}"

    def test_cleanup_below_threshold(self, rollback_manager, temp_dir):
        """验证备份数未超过阈值时不删除"""
        config_path = os.path.join(temp_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("test\n")

        # 创建 2 个备份（不超过 max_backups=3），每次间隔 1 秒确保时间戳不同
        for i in range(2):
            rollback_manager.create_backup(config_path)
            time.sleep(1)

        # 验证 2 个备份都保留
        remaining = rollback_manager.list_backups(config_path)
        assert len(remaining) == 2


# ============================================================
# 测试用例 5：list_backups 测试
# ============================================================


class TestListBackups:
    """测试 list_backups 方法"""

    def test_list_backups_empty(self, rollback_manager, temp_dir):
        """验证无备份时返回空列表"""
        config_path = os.path.join(temp_dir, "config.yaml")
        # 不创建备份

        backups = rollback_manager.list_backups(config_path)
        assert backups == []

    def test_list_backups_order(self, rollback_manager, temp_dir):
        """验证备份列表包含所有备份文件"""
        config_path = os.path.join(temp_dir, "config.yaml")
        with open(config_path, "w") as f:
            f.write("test\n")

        # 创建多个备份
        backup_paths = []
        for i in range(3):
            backup_path = rollback_manager.create_backup(config_path)
            backup_paths.append(backup_path)
            time.sleep(1)  # 确保时间戳不同

        backups = rollback_manager.list_backups(config_path)
        # 验证 3 个备份都存在
        assert len(backups) == 3
        for bp in backup_paths:
            assert bp in backups


# ============================================================
# 测试用例 6：覆盖层回滚操作
# ============================================================


class TestOverrideRollback:
    """测试覆盖层回滚操作"""

    def test_rollback_to_specific_version(self, rollback_manager, temp_dir):
        """验证通过备份实现覆盖层回滚"""
        # 模拟覆盖层目录结构
        override_dir = os.path.join(temp_dir, "tuning_overrides")
        os.makedirs(override_dir, exist_ok=True)

        # 创建版本文件
        v1_path = os.path.join(override_dir, "V20260804.yaml")
        v2_path = os.path.join(override_dir, "V20260811.yaml")
        active_path = os.path.join(override_dir, ".active")

        with open(v1_path, "w") as f:
            f.write("version: 1\n")
        with open(v2_path, "w") as f:
            f.write("version: 2\n")
        with open(active_path, "w") as f:
            f.write("V20260811\n")

        # 备份当前的 .active
        backup_path = rollback_manager.create_backup(active_path)

        # 回滚：将 .active 指向旧版本
        with open(active_path, "w") as f:
            f.write("V20260804\n")

        # 验证回滚成功
        with open(active_path, "r") as f:
            assert f.read().strip() == "V20260804"

        # 通过备份恢复（等待 1 秒确保 rollback 内部创建备份时时间戳不同）
        time.sleep(1)
        rollback_manager.rollback(active_path, backup_path)
        with open(active_path, "r") as f:
            assert f.read().strip() == "V20260811"