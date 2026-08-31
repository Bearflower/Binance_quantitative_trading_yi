"""
配置读写操作
负责安全地读取和写入策略 YAML 配置文件

提供两种写入模式：
1. apply_changes() — 直接写入 config.yaml（用于资金分配等非 AI 调优写入）
2. apply_overrides() — 写入 tuning_overrides 覆盖层（用于 AI 调优参数变更）

安全措施：
- 写入前先备份（通过 rollback_manager）
- 原子写入：先写临时文件，再用 os.rename 替换（防止写入中断损坏配置）
- 支持嵌套键路径读写
"""

import os
import tempfile
from datetime import datetime
from typing import Any, Dict

import structlog
import yaml

from shared.utils import get_nested_value

logger = structlog.get_logger()


class ConfigOperator:
    """
    配置读写操作器

    提供安全的配置读写能力，支持嵌套键路径和原子替换。
    AI 调优参数写入 tuning_overrides 覆盖层，与基础配置隔离。
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
        应用参数变更到配置文件（直接写入 config.yaml）

        用于非 AI 调优写入（如资金分配 capital_limits 更新）。
        AI 调优参数变更请使用 apply_overrides()。

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

    def apply_overrides(
        self,
        config_path: str,
        adjustments: Dict[str, Any],
    ) -> bool:
        """
        应用 AI 调优参数到 tuning_overrides 覆盖层

        不修改 config.yaml 基础配置，而是写入独立的覆盖层文件。
        策略运行时通过 shared/config_loader.py 自动合并基础配置 + 覆盖层。

        流程：
        1. 生成版本号 V{YYYYMMDD}（同一天多次调用追加后缀，如 V20260811_02）
        2. 从 config_path 推导策略目录和覆盖层目录
        3. 将扁平参数路径转为嵌套字典结构
        4. 原子写入 tuning_overrides/V{version}.yaml
        5. 原子写入 .active 指向新版本

        回滚机制：
        - 写入覆盖层文件失败 → 不修改 .active，状态不变
        - 写入 .active 失败 → 删除已创建的覆盖层文件，回滚到前状态
        - 回滚通过修改 .active 指向旧版本即可，无需删除覆盖层文件

        Args:
            config_path: 策略 config.yaml 路径（用于推导目录）
                         例如 "strategies/btc_eth/config.yaml"
            adjustments: 参数调整，格式为 {param_path: new_value}
                         例如 {"scoring.min_score": 0.75, "scoring.weights.trend_strength": 0.45}

        Returns:
            是否成功
        """
        try:
            # 推导策略目录和覆盖层目录
            strategy_dir = os.path.dirname(config_path)
            override_dir = os.path.join(strategy_dir, "tuning_overrides")

            # 创建覆盖层目录（如果不存在）
            os.makedirs(override_dir, exist_ok=True)
            # 确保目录可写（volume 挂载可能导致权限问题，如宿主 UID 501 vs 容器 UID 1000）
            try:
                # 先尝试 chmod 目录本身
                st = os.stat(override_dir)
                current_mode = st.st_mode & 0o777
                if current_mode != 0o777:
                    os.chmod(override_dir, 0o777)
                    logger.info("覆盖层目录权限已修复", override_dir=override_dir, mode=oct(current_mode))
            except PermissionError:
                logger.warning(
                    "无法修改覆盖层目录权限（容器用户非目录所有者），"
                    "请确保部署时使用 chmod 777 预置目录权限",
                    override_dir=override_dir,
                    owner_uid=os.stat(override_dir).st_uid,
                    container_uid=os.getuid(),
                )
            except Exception as e:
                logger.warning("修改覆盖层目录权限异常", override_dir=override_dir, error=str(e))

            # 生成版本号
            version = self._generate_version(override_dir)

            # 将扁平参数路径转为嵌套字典结构
            override_config = self._flat_to_nested(adjustments)

            # 写入覆盖层文件
            override_path = os.path.join(override_dir, f"{version}.yaml")
            self._atomic_write(override_path, override_config)

            # 备份当前 .active（如果存在）
            active_path = os.path.join(override_dir, ".active")
            old_active = None
            if os.path.exists(active_path):
                try:
                    with open(active_path, "r", encoding="utf-8") as f:
                        old_active = f.read().strip()
                except Exception:
                    pass

            # 写入 .active
            try:
                self._atomic_write_text(active_path, version)
            except Exception:
                # 回滚：删除已创建的覆盖层文件
                if os.path.exists(override_path):
                    os.unlink(override_path)
                logger.error(
                    ".active 写入失败，已回滚覆盖层文件",
                    override_path=override_path,
                    active_path=active_path,
                )
                return False

            logger.info(
                "AI 调优覆盖层已应用",
                strategy_dir=strategy_dir,
                version=version,
                override_path=override_path,
                changes_count=len(adjustments),
                previous_active=old_active,
            )
            return True

        except Exception as e:
            logger.error(
                "应用覆盖层异常",
                config_path=config_path,
                error=str(e),
            )
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
        return get_nested_value(config, key_path)

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

    def _atomic_write_text(self, file_path: str, content: str) -> None:
        """
        原子写入文本文件

        先写入临时文件，再用 os.rename 原子替换。

        Args:
            file_path: 目标文件路径
            content: 文本内容
        """
        dir_name = os.path.dirname(file_path)
        fd, tmp_path = tempfile.mkstemp(
            dir=dir_name,
            prefix=".tmp_",
            suffix=".txt",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.write("\n")
            os.rename(tmp_path, file_path)
            logger.debug("文本文件原子写入完成", file_path=file_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _generate_version(self, override_dir: str) -> str:
        """
        生成覆盖层版本号

        格式：V{YYYYMMDD}
        同一天多次调用自动追加后缀：V{YYYYMMDD}_02, V{YYYYMMDD}_03, ...

        Args:
            override_dir: 覆盖层目录路径

        Returns:
            版本号字符串，如 "V20260811" 或 "V20260811_02"
        """
        today = datetime.now().strftime("%Y%m%d")
        base_version = f"V{today}"
        version = base_version
        counter = 2

        # 检查是否已存在同版本文件，存在则递增后缀
        while os.path.exists(os.path.join(override_dir, f"{version}.yaml")):
            version = f"{base_version}_{counter:02d}"
            counter += 1

        return version

    @staticmethod
    def _flat_to_nested(adjustments: Dict[str, Any]) -> Dict[str, Any]:
        """
        将扁平参数路径转为嵌套字典结构

        输入:  {"scoring.min_score": 0.75, "scoring.weights.trend_strength": 0.45}
        输出:  {"scoring": {"min_score": 0.75, "weights": {"trend_strength": 0.45}}}

        Args:
            adjustments: 扁平参数路径字典，key 为点分隔路径

        Returns:
            嵌套字典结构
        """
        result: Dict[str, Any] = {}
        for param_path, value in adjustments.items():
            keys = param_path.split(".")
            current = result
            # 遍历到倒数第二级
            for key in keys[:-1]:
                if key not in current:
                    current[key] = {}
                elif not isinstance(current[key], dict):
                    # 路径冲突（中间节点已存在且不是字典），跳过
                    logger.warning(
                        "参数路径冲突，中间节点不是字典",
                        param_path=param_path,
                        conflicting_key=key,
                    )
                    break
                current = current[key]
            else:
                # 设置最后一层的值
                last_key = keys[-1]
                current[last_key] = value

        return result