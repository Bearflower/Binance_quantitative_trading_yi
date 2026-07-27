"""
回滚管理
负责配置文件的备份、恢复和清理

每次应用参数变更前自动备份，支持从备份恢复。
备份文件命名格式：原文件名.backup.{timestamp}
"""

import os
import shutil
from datetime import datetime
from typing import List

import structlog

logger = structlog.get_logger()


class RollbackManager:
    """
    回滚管理器

    提供配置文件的备份、恢复和清理功能。
    每次应用变更前自动创建备份，支持手动回滚。
    """

    def __init__(self, max_backups: int = 10):
        """
        初始化回滚管理器

        Args:
            max_backups: 每个配置文件保留的最大备份数
        """
        self.max_backups = max_backups

    def create_backup(self, config_path: str) -> str:
        """
        创建配置文件备份

        备份文件命名格式：原文件名.backup.{timestamp}

        Args:
            config_path: 配置文件路径

        Returns:
            备份文件路径，如果备份失败返回空字符串
        """
        if not os.path.exists(config_path):
            logger.warning("配置文件不存在，无法备份", config_path=config_path)
            return ""

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{config_path}.backup.{timestamp}"
            shutil.copy2(config_path, backup_path)

            logger.info(
                "配置备份已创建",
                config_path=config_path,
                backup_path=backup_path,
            )

            # 清理旧备份
            self.cleanup_old_backups(config_path)

            return backup_path

        except Exception as e:
            logger.error("创建配置备份异常", config_path=config_path, error=str(e))
            return ""

    def rollback(self, config_path: str, backup_path: str) -> bool:
        """
        从备份恢复配置

        Args:
            config_path: 目标配置文件路径
            backup_path: 备份文件路径

        Returns:
            是否成功
        """
        if not os.path.exists(backup_path):
            logger.error("备份文件不存在", backup_path=backup_path)
            return False

        try:
            # 恢复前先备份当前配置（防止误操作）
            current_backup = self.create_backup(config_path)

            shutil.copy2(backup_path, config_path)

            logger.info(
                "配置已从备份恢复",
                config_path=config_path,
                backup_path=backup_path,
            )
            return True

        except Exception as e:
            logger.error("配置回滚异常", config_path=config_path, error=str(e))
            return False

    def list_backups(self, config_path: str) -> List[str]:
        """
        列出所有备份文件

        Args:
            config_path: 配置文件路径

        Returns:
            备份文件路径列表（按时间倒序）
        """
        dir_name = os.path.dirname(config_path)
        base_name = os.path.basename(config_path)

        if not os.path.exists(dir_name):
            return []

        backup_prefix = f"{base_name}.backup."
        backups = []
        for f in os.listdir(dir_name):
            if f.startswith(backup_prefix):
                full_path = os.path.join(dir_name, f)
                backups.append(full_path)

        # 按修改时间倒序
        backups.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return backups

    def cleanup_old_backups(self, config_path: str, keep_count: int = None) -> int:
        """
        清理旧备份文件

        保留最近 N 个备份，删除多余的。

        Args:
            config_path: 配置文件路径
            keep_count: 保留数量，默认使用实例配置

        Returns:
            删除的备份文件数量
        """
        keep = keep_count if keep_count is not None else self.max_backups
        backups = self.list_backups(config_path)

        if len(backups) <= keep:
            return 0

        deleted = 0
        for old_backup in backups[keep:]:
            try:
                os.unlink(old_backup)
                deleted += 1
                logger.debug("旧备份已清理", backup_path=old_backup)
            except Exception as e:
                logger.warning("清理旧备份失败", backup_path=old_backup, error=str(e))

        if deleted > 0:
            logger.info("旧备份清理完成", config_path=config_path, deleted_count=deleted)

        return deleted