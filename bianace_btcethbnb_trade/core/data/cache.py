#!/usr/bin/env python3
"""
数据缓存管理模块（增强版）

功能：
1. 使用 cachetools 实现高性能缓存
2. 支持TTL（Time To Live）过期机制
3. 支持LRU（Least Recently Used）淘汰策略
4. 提供缓存统计和监控
5. 支持缓存预热和清理

版本: v2.0.0 (增强版)
更新时间: 2026-04-27
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable
from functools import wraps

try:
    from cachetools import TTLCache, LRUCache, cached
    from cachetools.keys import hashkey
    CACHE_AVAILABLE = True
except ImportError:
    CACHE_AVAILABLE = False
    logging.warning("cachetools 未安装，缓存功能将降级为简单实现")

logger = logging.getLogger(__name__)


class CacheStats:
    """缓存统计信息"""

    def __init__(self):
        """初始化统计信息"""
        self.hits = 0      # 缓存命中次数
        self.misses = 0    # 缓存未命中次数
        self.evictions = 0 # 缓存淘汰次数

    @property
    def total_requests(self) -> int:
        """总请求次数"""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """缓存命中率"""
        if self.total_requests == 0:
            return 0.0
        return self.hits / self.total_requests

    def record_hit(self):
        """记录缓存命中"""
        self.hits += 1

    def record_miss(self):
        """记录缓存未命中"""
        self.misses += 1

    def record_eviction(self):
        """记录缓存淘汰"""
        self.evictions += 1

    def reset(self):
        """重置统计信息"""
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'hits': self.hits,
            'misses': self.misses,
            'evictions': self.evictions,
            'total_requests': self.total_requests,
            'hit_rate': round(self.hit_rate, 4)
        }


class DataCache:
    """
    数据缓存管理类（增强版）

    使用 cachetools 实现高性能缓存，支持TTL过期和LRU淘汰策略。

    特性：
    1. TTL（Time To Live）自动过期
    2. LRU（Least Recently Used）淘汰策略
    3. 缓存统计和监控
    4. 线程安全
    """

    def __init__(
        self,
        maxsize: int = 100,
        ttl_seconds: int = 300,
        enable_stats: bool = True
    ):
        """
        初始化缓存管理器

        Args:
            maxsize: 最大缓存条目数（默认100）
            ttl_seconds: 缓存过期时间（秒，默认300秒即5分钟）
            enable_stats: 是否启用统计（默认True）
        """
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self.enable_stats = enable_stats

        # 缓存统计
        self.stats = CacheStats() if enable_stats else None

        # 使用 cachetools 的 TTLCache
        if CACHE_AVAILABLE:
            self._cache = TTLCache(maxsize=maxsize, ttl=ttl_seconds)
            logger.info(f"缓存管理器初始化完成（cachetools模式）：maxsize={maxsize}, ttl={ttl_seconds}秒")
        else:
            # 降级为简单实现
            self._cache: Dict[str, Dict[str, Any]] = {}
            self._timestamps: Dict[str, datetime] = {}
            logger.warning("缓存管理器初始化完成（简单模式）：缺少 cachetools 库")

    def is_valid(self, symbols: List[str] = None) -> bool:
        """
        检查缓存是否有效

        Args:
            symbols: 需要检查的交易对列表，None表示检查所有缓存

        Returns:
            缓存是否有效
        """
        if not self._cache:
            return False

        if CACHE_AVAILABLE:
            # cachetools 自动处理过期，只需检查是否存在
            if symbols:
                for symbol in symbols:
                    if symbol not in self._cache:
                        return False
            return len(self._cache) > 0
        else:
            # 简单实现需要手动检查过期
            now = datetime.now()
            for symbol in (symbols or list(self._cache.keys())):
                if symbol not in self._cache:
                    return False
                if symbol in self._timestamps:
                    if now - self._timestamps[symbol] > timedelta(seconds=self.ttl_seconds):
                        return False
            return True

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取单个交易对的缓存数据

        Args:
            symbol: 交易对

        Returns:
            缓存数据，如果不存在则返回None
        """
        try:
            if CACHE_AVAILABLE:
                if symbol in self._cache:
                    if self.enable_stats:
                        self.stats.record_hit()
                    return self._cache[symbol]
                else:
                    if self.enable_stats:
                        self.stats.record_miss()
                    return None
            else:
                # 简单实现
                if symbol in self._cache:
                    # 检查是否过期
                    if symbol in self._timestamps:
                        if datetime.now() - self._timestamps[symbol] > timedelta(seconds=self.ttl_seconds):
                            # 已过期
                            self.remove(symbol)
                            if self.enable_stats:
                                self.stats.record_miss()
                            return None

                    if self.enable_stats:
                        self.stats.record_hit()
                    return self._cache[symbol]
                else:
                    if self.enable_stats:
                        self.stats.record_miss()
                    return None
        except Exception as e:
            logger.error(f"获取缓存失败：{str(e)}")
            return None

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有缓存数据

        Returns:
            所有缓存数据
        """
        if CACHE_AVAILABLE:
            return dict(self._cache)
        else:
            # 过滤过期数据
            now = datetime.now()
            valid_data = {}
            for symbol, data in self._cache.items():
                if symbol in self._timestamps:
                    if now - self._timestamps[symbol] <= timedelta(seconds=self.ttl_seconds):
                        valid_data[symbol] = data
                else:
                    valid_data[symbol] = data
            return valid_data

    def set(self, symbol: str, data: Dict[str, Any]):
        """
        设置单个交易对的缓存数据

        Args:
            symbol: 交易对
            data: 数据
        """
        try:
            if CACHE_AVAILABLE:
                self._cache[symbol] = data
            else:
                # 简单实现
                self._cache[symbol] = data
                self._timestamps[symbol] = datetime.now()

            logger.debug(f"已缓存 {symbol} 数据")
        except Exception as e:
            logger.error(f"设置缓存失败：{str(e)}")

    def set_all(self, data: Dict[str, Dict[str, Any]]):
        """
        设置所有缓存数据

        Args:
            data: 数据字典
        """
        try:
            if CACHE_AVAILABLE:
                self._cache.update(data)
            else:
                now = datetime.now()
                self._cache.update(data)
                for symbol in data.keys():
                    self._timestamps[symbol] = now

            logger.info(f"已缓存 {len(data)} 个交易对的数据")
        except Exception as e:
            logger.error(f"批量设置缓存失败：{str(e)}")

    def clear(self):
        """清除所有缓存"""
        try:
            if CACHE_AVAILABLE:
                self._cache.clear()
            else:
                self._cache.clear()
                self._timestamps.clear()

            if self.enable_stats:
                self.stats.reset()

            logger.info("缓存已清除")
        except Exception as e:
            logger.error(f"清除缓存失败：{str(e)}")

    def remove(self, symbol: str):
        """
        移除单个交易对的缓存

        Args:
            symbol: 交易对
        """
        try:
            if symbol in self._cache:
                del self._cache[symbol]
                if not CACHE_AVAILABLE and symbol in self._timestamps:
                    del self._timestamps[symbol]
                logger.debug(f"已移除 {symbol} 缓存")
        except Exception as e:
            logger.error(f"移除缓存失败：{str(e)}")

    def update_timestamp(self):
        """更新缓存时间戳（仅简单模式需要）"""
        if not CACHE_AVAILABLE:
            now = datetime.now()
            for symbol in self._cache.keys():
                self._timestamps[symbol] = now

    def get_age(self, symbol: str = None) -> Optional[timedelta]:
        """
        获取缓存年龄

        Args:
            symbol: 交易对（可选，None表示获取最新缓存年龄）

        Returns:
            缓存年龄，如果缓存为空则返回None
        """
        if CACHE_AVAILABLE:
            # cachetools 不提供年龄信息
            return None
        else:
            if not self._timestamps:
                return None

            if symbol:
                if symbol in self._timestamps:
                    return datetime.now() - self._timestamps[symbol]
                return None
            else:
                # 返回最新的缓存时间
                if self._timestamps:
                    latest = max(self._timestamps.values())
                    return datetime.now() - latest
                return None

    def get_symbols(self) -> List[str]:
        """
        获取缓存中的所有交易对

        Returns:
            交易对列表
        """
        return list(self._cache.keys())

    def has_symbol(self, symbol: str) -> bool:
        """
        检查缓存中是否包含指定交易对

        Args:
            symbol: 交易对

        Returns:
            是否包含
        """
        return symbol in self._cache

    def get_stats(self) -> Optional[Dict[str, Any]]:
        """
        获取缓存统计信息

        Returns:
            统计信息字典，如果未启用统计则返回None
        """
        if self.enable_stats:
            stats_dict = self.stats.to_dict()
            stats_dict['cache_size'] = len(self._cache)
            stats_dict['max_size'] = self.maxsize
            stats_dict['ttl_seconds'] = self.ttl_seconds
            return stats_dict
        return None

    def get_size(self) -> int:
        """
        获取当前缓存大小

        Returns:
            缓存条目数
        """
        return len(self._cache)

    def is_empty(self) -> bool:
        """
        检查缓存是否为空

        Returns:
            是否为空
        """
        return len(self._cache) == 0


def cache_result(
    maxsize: int = 100,
    ttl_seconds: int = 300,
    key_func: Callable = None
):
    """
    缓存装饰器

    为函数结果添加缓存支持。

    Args:
        maxsize: 最大缓存条目数
        ttl_seconds: 缓存过期时间（秒）
        key_func: 自定义缓存键生成函数

    Example:
        @cache_result(maxsize=50, ttl_seconds=60)
        def get_klines(symbol: str, interval: str):
            # 获取K线数据
            return data
    """
    if not CACHE_AVAILABLE:
        # 如果 cachetools 不可用，返回原函数
        def decorator(func):
            return func
        return decorator

    def decorator(func):
        # 创建缓存实例
        cache = TTLCache(maxsize=maxsize, ttl=ttl_seconds)

        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存键
            if key_func:
                key = key_func(*args, **kwargs)
            else:
                key = hashkey(*args, **kwargs)

            # 尝试从缓存获取
            try:
                return cache[key]
            except KeyError:
                # 缓存未命中，执行函数
                result = func(*args, **kwargs)
                cache[key] = result
                return result

        # 添加缓存管理方法
        wrapper.cache = cache
        wrapper.cache_clear = cache.clear
        wrapper.cache_info = lambda: {'size': len(cache), 'maxsize': maxsize, 'ttl': ttl_seconds}

        return wrapper

    return decorator
