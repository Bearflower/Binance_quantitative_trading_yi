"""
配置读写操作
负责安全地读取和写入策略 YAML 配置文件

安全措施：
- 写入前先备份（通过 rollback_manager）
- 原子写入：先写临时文件，再用 os.rename 替换（防止写入中断损坏配置）
- 支持嵌套键路径读写
"""

import os
import tempfile
from typing import Any, Dict

import structlog
import yaml

logger = structlog.get_logger()


class ConfigOperator:
    """
    配置读写操作器

    提供安全的配置读写能力，支持嵌套键路径和原子替换。
    """

    def __init__(self, rollback_manager=None):
        """
        初始化配置操作器

        Args:
            rollback_manager: RollbackManager 实例，用于备份管理
        """
        self.rollback_manager = rollback_manager

    def read_config(self, config_path: str) -> Dict[str, Any]:
        """
        读取 YAML 配置文件

        Args:
            config_path: 配置文件路径

        Returns:
            配置字典，文件不存在返回空字典
        """
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
                logger.debug("配置文件读取成功", config_path=config_path)
                return config
        except FileNotFoundError:
            logger.warning("配置文件不存在", config_path=config_path)
            return {}
        except Exception as e:
            logger.error("读取配置文件异常", config_path=config_path, error=str(e))
            return {}

    def apply_changes(
        self,
        config_path: str,
        adjustments: Dict[str, Any],
    ) -> bool:
        """
        应用参数变更到配置文件

        流程：
        1. 备份当前配置（通过 rollback_manager）
        2. 读取当前配置
        3. 应用变更
        4. 原子写入（临时文件 → rename）

        Args:
            config_path: 配置文件路径
            adjustments: 参数调整，格式为 {param_path: new_value} 或 {param_path: {"to": new_value}}

        Returns:
            是否成功
        """
        try:
            # 备份当前配置
            if self.rollback_manager:
                self.rollback_manager.create_backup(config_path)

            # 读取当前配置
            config = self.read_config(config_path)
            if not config:
                logger.error("配置文件为空，无法应用变更", config_path=config_path)
                return False

            # 应用变更
            changes_applied = 0
            for param_path, adjustment in adjustments.items():
                # 提取新值
                if isinstance(adjustment, dict):
                    new_value = adjustment.get("to")
                else:
                    new_value = adjustment

                if new_value is None:
                    logger.warning("参数缺少目标值", param_path=param_path)
                    continue

                # 设置嵌套值
                if self.set_nested_value(config, param_path, new_value):
                    changes_applied += 1
                    logger.info(
                        "参数变更已应用",
                        param_path=param_path,
                        new_value=new_value,
                    )

            if changes_applied == 0:
                logger.warning("没有参数被变更", config_path=config_path)
                return False

            # 原子写入
            self._atomic_write(config_path, config)
            logger.info(
                "配置文件已更新",
                config_path=config_path,
                changes_count=changes_applied,
            )
            return True

        except Exception as e:
            logger.error("应用配置变更异常", config_path=config_path, error=str(e))
            return False

    def get_nested_value(self, config: Dict[str, Any], key_path: str) -> Any:
        """
        按点分隔路径读取嵌套字典值

        Args:
            config: 配置字典
            key_path: 点分隔的键路径，如 "scoring.min_score"

        Returns:
            配置值，如果路径不存在返回 None
        """
        keys = key_path.split(".")
        current = config
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current

    def set_nested_value(self, config: Dict[str, Any], key_path: str, value: Any) -> bool:
        """
        按点分隔路径设置嵌套字典值

        Args:
            config: 配置字典（会被原地修改）
            key_path: 点分隔的键路径，如 "scoring.min_score"
            value: 要设置的值

        Returns:
            是否设置成功
        """
        keys = key_path.split(".")
        current = config

        # 遍历到倒数第二级
        for i, key in enumerate(keys[:-1]):
            if key not in current:
                logger.error("配置路径不存在", key_path=key_path, missing_key=key)
                return False
            current = current[key]
            if not isinstance(current, dict):
                logger.error("配置路径中间节点不是字典", key_path=key_path, node=key)
                return False

        # 设置最后一层的值
        last_key = keys[-1]
        if last_key in current:
            old_value = current[last_key]
            current[last_key] = value
            logger.debug(
                "嵌套值已设置",
                key_path=key_path,
                old_value=old_value,
                new_value=value,
            )
            return True
        else:
            logger.error("配置路径最后一层不存在", key_path=key_path, missing_key=last_key)
            return False

    def _atomic_write(self, config_path: str, config: Dict[str, Any]) -> None:
        """
        原子写入配置文件

        先写入临时文件，再用 os.rename 原子替换，防止写入中断损坏配置。

        Args:
            config_path: 目标配置文件路径
            config: 配置字典
        """
        dir_name = os.path.dirname(config_path)
        # 创建临时文件（与目标文件在同一目录，确保 rename 是原子操作）
        fd, tmp_path = tempfile.mkstemp(
            dir=dir_name,
            prefix=".tmp_",
            suffix=".yaml",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(
                    config,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            # 原子替换
            os.rename(tmp_path, config_path)
            logger.debug("配置文件原子写入完成", config_path=config_path)
        except Exception:
            # 清理临时文件
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise