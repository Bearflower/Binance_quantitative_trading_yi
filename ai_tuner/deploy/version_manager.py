"""
版本管理器
负责读取和管理 tuning_overrides 版本文件

提供版本号读取、生成等操作，供 EffectTracker 和 WeeklyTuningJob 使用。
"""

import os
from datetime import datetime
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger()


class VersionManager:
    """
    版本管理器

    管理策略 tuning_overrides 目录下的版本文件：
    - .active：指向当前生效版本的指针文件
    - V{YYYYMMDD}：版本目录，包含 AI 调优的覆盖层参数
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化版本管理器

        Args:
            config: 系统配置字典（用于推导项目根目录）
        """
        self.config = config
        # 项目根目录：从 ai_tuner/config.yaml 所在目录的父目录推导
        # config 可能不包含 project_root，从当前文件位置推导
        self.project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        logger.debug("版本管理器初始化", project_root=self.project_root)

    def _get_strategy_dir(self, strategy_config_path: str) -> str:
        """
        根据策略配置文件路径获取策略目录路径

        Args:
            strategy_config_path: 策略配置文件路径（相对于项目根目录）
                如 "strategies/btc_eth/config.yaml"

        Returns:
            策略目录的绝对路径
        """
        relative_dir = os.path.dirname(strategy_config_path)
        return os.path.join(self.project_root, relative_dir)

    def _get_tuning_overrides_dir(self, strategy_config_path: str) -> str:
        """
        获取策略的 tuning_overrides 目录路径

        Args:
            strategy_config_path: 策略配置文件路径

        Returns:
            tuning_overrides 目录的绝对路径
        """
        strategy_dir = self._get_strategy_dir(strategy_config_path)
        return os.path.join(strategy_dir, "tuning_overrides")

    def get_active_version(self, strategy_config_path: str) -> Optional[str]:
        """
        读取当前生效的版本号

        从策略的 tuning_overrides/.active 文件中读取版本号。

        Args:
            strategy_config_path: 策略配置文件路径（如 "strategies/btc_eth/config.yaml"）

        Returns:
            版本号字符串（如 "V20260804"），文件不存在或读取失败返回 None
        """
        overrides_dir = self._get_tuning_overrides_dir(strategy_config_path)
        active_file = os.path.join(overrides_dir, ".active")

        try:
            if not os.path.exists(active_file):
                logger.debug(
                    "active 文件不存在",
                    strategy_config_path=strategy_config_path,
                    active_file=active_file,
                )
                return None

            with open(active_file, "r", encoding="utf-8") as f:
                version = f.read().strip()

            if not version:
                logger.warning(
                    "active 文件为空",
                    strategy_config_path=strategy_config_path,
                )
                return None

            logger.debug(
                "读取 active 版本号",
                strategy_config_path=strategy_config_path,
                version=version,
            )
            return version

        except Exception as e:
            logger.error(
                "读取 active 文件异常",
                strategy_config_path=strategy_config_path,
                error=str(e),
            )
            return None

    def get_latest_version_number(self, strategy_config_path: str) -> int:
        """
        获取最新版本号数值

        扫描 tuning_overrides 目录下的 V{YYYYMMDD} 目录，找出最大版本号。

        Args:
            strategy_config_path: 策略配置文件路径

        Returns:
            最新版本号数值（如 20260804），无历史版本时返回 0
        """
        overrides_dir = self._get_tuning_overrides_dir(strategy_config_path)

        try:
            if not os.path.exists(overrides_dir):
                return 0

            max_version = 0
            for item in os.listdir(overrides_dir):
                # 去掉 .yaml 扩展名，适配文件格式（如 V20260801.yaml → V20260801）
                basename = item[:-5] if item.endswith(".yaml") else item
                if basename.startswith("V") and len(basename) == 9:
                    try:
                        num = int(basename[1:])
                        max_version = max(max_version, num)
                    except ValueError:
                        continue

            return max_version

        except Exception as e:
            logger.error(
                "获取最新版本号异常",
                strategy_config_path=strategy_config_path,
                error=str(e),
            )
            return 0

    def generate_new_version(self, strategy_config_path: str) -> str:
        """
        生成新版本号

        格式为 V{YYYYMMDD}，如 "V20260811"。
        如果当天已存在版本号，则追加后缀 "V20260811_01"。
        兼容旧格式 "V20260811-1"（使用 - 分隔符的旧版本）。

        Args:
            strategy_config_path: 策略配置文件路径

        Returns:
            新版本号字符串
        """
        today = datetime.now().strftime("%Y%m%d")
        base_version = f"V{today}"

        # 检查当天是否已存在版本号
        overrides_dir = self._get_tuning_overrides_dir(strategy_config_path)

        try:
            if os.path.exists(overrides_dir):
                suffix = 0
                for item in os.listdir(overrides_dir):
                    if item.startswith(base_version):
                        # 去掉 .yaml 扩展名（如 V20260811_01.yaml → V20260811_01）
                        basename = item[:-5] if item.endswith(".yaml") else item
                        rest = basename[len(base_version):]
                        if rest == "":
                            suffix = max(suffix, 1)
                        elif rest.startswith("_") or rest.startswith("-"):
                            try:
                                num = int(rest[1:])
                                suffix = max(suffix, num + 1)
                            except ValueError:
                                continue

                if suffix > 0:
                    new_version = f"{base_version}_{suffix:02d}"
                else:
                    new_version = base_version
            else:
                new_version = base_version

            logger.debug(
                "生成新版本号",
                strategy_config_path=strategy_config_path,
                new_version=new_version,
            )
            return new_version

        except Exception as e:
            logger.error(
                "生成新版本号异常，使用默认格式",
                strategy_config_path=strategy_config_path,
                error=str(e),
            )
            return base_version