"""
Dashboard 缓存管理
基于内存的 TTL 缓存实现
"""
from typing import Any, Optional, Dict, Tuple
from datetime import datetime, timedelta

import structlog


logger = structlog.get_logger()


class CacheService:
    """
    内存缓存服务（TTL）

    使用字典存储缓存数据，每个缓存项包含数据和过期时间。
    支持自动过期清理和手动清除。
    """

    def __init__(self, enabled: bool = True):
        """
        初始化缓存服务

        Args:
            enabled: 是否启用缓存（默认启用）
        """
        self.enabled = enabled
        self._cache: Dict[str, Tuple[Any, datetime]] = {}
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0
        }

        logger.info(
            "缓存服务初始化完成",
            enabled=enabled
        )

    def get(self, key: str) -> Optional[Any]:
        """
        获取缓存数据

        如果缓存不存在或已过期，返回 None。

        Args:
            key: 缓存键

        Returns:
            缓存数据，不存在或过期返回 None
        """
        if not self.enabled:
            self._stats["misses"] += 1
            return None

        if key not in self._cache:
            self._stats["misses"] += 1
            logger.debug("缓存未命中", key=key)
            return None

        data, expire_time = self._cache[key]

        # 检查是否过期
        if datetime.now() > expire_time:
            # 删除过期缓存
            del self._cache[key]
            self._stats["misses"] += 1
            logger.debug("缓存已过期", key=key)
            return None

        self._stats["hits"] += 1
        logger.debug(
            "缓存命中",
            key=key,
            remaining_seconds=(expire_time - datetime.now()).total_seconds()
        )
        return data

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """
        设置缓存数据

        Args:
            key: 缓存键
            value: 缓存数据
            ttl_seconds: 过期时间（秒）
        """
        if not self.enabled:
            logger.debug("缓存已禁用，跳过设置", key=key)
            return

        expire_time = datetime.now() + timedelta(seconds=ttl_seconds)
        self._cache[key] = (value, expire_time)
        self._stats["sets"] += 1

        logger.debug(
            "缓存已设置",
            key=key,
            ttl_seconds=ttl_seconds,
            expire_at=expire_time.isoformat()
        )

    def delete(self, key: str) -> bool:
        """
        删除缓存数据

        Args:
            key: 缓存键

        Returns:
            是否成功删除
        """
        if key in self._cache:
            del self._cache[key]
            self._stats["deletes"] += 1
            logger.debug("缓存已删除", key=key)
            return True
        return False

    def clear(self) -> None:
        """清空所有缓存"""
        count = len(self._cache)
        self._cache.clear()
        logger.info("缓存已清空", cleared_count=count)

    def clear_expired(self) -> int:
        """
        清除所有过期缓存

        Returns:
            清除的缓存数量
        """
        now = datetime.now()
        expired_keys = [
            key for key, (_, expire_time) in self._cache.items()
            if now > expire_time
        ]

        for key in expired_keys:
            del self._cache[key]

        if expired_keys:
            logger.info("过期缓存已清除", count=len(expired_keys))

        return len(expired_keys)

    def get_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息

        Returns:
            统计信息字典
        """
        total_requests = self._stats["hits"] + self._stats["misses"]
        hit_rate = (
            self._stats["hits"] / total_requests * 100
            if total_requests > 0 else 0
        )

        return {
            "enabled": self.enabled,
            "total_items": len(self._cache),
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "sets": self._stats["sets"],
            "deletes": self._stats["deletes"],
            "hit_rate": round(hit_rate, 2)
        }

    def get_or_set(
        self,
        key: str,
        factory: callable,
        ttl_seconds: int
    ) -> Any:
        """
        获取缓存数据，如果不存在则通过工厂函数创建并缓存

        Args:
            key: 缓存键
            factory: 数据工厂函数（无参函数，返回数据）
            ttl_seconds: 过期时间（秒）

        Returns:
            缓存数据或新创建的数据
        """
        # 尝试从缓存获取
        cached = self.get(key)
        if cached is not None:
            return cached

        # 调用工厂函数创建数据
        data = factory()

        # 写入缓存
        self.set(key, data, ttl_seconds)

        return data


# 全局缓存服务实例
cache_service = CacheService()
