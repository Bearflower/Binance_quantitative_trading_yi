"""
数据缓存模块

负责：
- 内存缓存
- 文件缓存
- 缓存过期管理
"""

import time
import json
import hashlib
from pathlib import Path
from typing import Any, Optional, Dict
from functools import wraps

from utils.logger import logger


class DataCache:
    """数据缓存管理器"""
    
    def __init__(
        self,
        cache_dir: str = "data/cache",
        memory_ttl: int = 300,
        file_ttl: int = 3600
    ):
        """
        初始化缓存管理器
        
        Args:
            cache_dir: 文件缓存目录
            memory_ttl: 内存缓存过期时间 (秒，默认 5 分钟)
            file_ttl: 文件缓存过期时间 (秒，默认 1 小时)
        """
        self.cache_dir = Path(cache_dir)
        self.memory_ttl = memory_ttl
        self.file_ttl = file_ttl
        
        # 内存缓存
        self._memory_cache: Dict[str, Dict] = {}
        
        # 确保缓存目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(
            f"✅ 数据缓存管理器初始化完成 "
            f"(内存 TTL={memory_ttl}s, 文件 TTL={file_ttl}s)"
        )
    
    def _generate_key(self, prefix: str, data: Any) -> str:
        """
        生成缓存键
        
        Args:
            prefix: 键前缀
            data: 数据 (用于生成 hash)
            
        Returns:
            缓存键字符串
        """
        data_str = json.dumps(data, sort_keys=True)
        data_hash = hashlib.md5(data_str.encode()).hexdigest()
        return f"{prefix}_{data_hash}"
    
    def get_memory(self, key: str) -> Optional[Any]:
        """
        从内存缓存获取数据
        
        Args:
            key: 缓存键
            
        Returns:
            缓存数据，过期或不存在返回 None
        """
        if key not in self._memory_cache:
            return None
        
        cache_entry = self._memory_cache[key]
        timestamp = cache_entry.get('timestamp', 0)
        data = cache_entry.get('data')
        
        # 检查是否过期
        if (time.time() - timestamp) > self.memory_ttl:
            logger.debug(f"📦 内存缓存过期：{key}")
            del self._memory_cache[key]
            return None
        
        logger.debug(f"📦 命中内存缓存：{key}")
        return data
    
    def set_memory(self, key: str, data: Any):
        """
        设置内存缓存
        
        Args:
            key: 缓存键
            data: 缓存数据
        """
        self._memory_cache[key] = {
            'data': data,
            'timestamp': time.time()
        }
        logger.debug(f"💾 设置内存缓存：{key}")
    
    def get_file(self, key: str) -> Optional[Any]:
        """
        从文件缓存获取数据
        
        Args:
            key: 缓存键
            
        Returns:
            缓存数据，过期或不存在返回 None
        """
        file_path = self.cache_dir / f"{key}.json"
        
        if not file_path.exists():
            return None
        
        try:
            # 检查文件修改时间
            file_mtime = file_path.stat().st_mtime
            if (time.time() - file_mtime) > self.file_ttl:
                logger.debug(f"📦 文件缓存过期：{key}")
                file_path.unlink()  # 删除过期文件
                return None
            
            # 读取文件
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            logger.debug(f"📦 命中文件缓存：{key}")
            return data
            
        except Exception as e:
            logger.error(f"❌ 读取文件缓存失败：{e}")
            return None
    
    def set_file(self, key: str, data: Any):
        """
        设置文件缓存
        
        Args:
            key: 缓存键
            data: 缓存数据
        """
        file_path = self.cache_dir / f"{key}.json"
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            logger.debug(f"💾 设置文件缓存：{key}")
            
        except Exception as e:
            logger.error(f"❌ 写入文件缓存失败：{e}")
    
    def get(
        self,
        prefix: str,
        key_data: Any,
        use_memory: bool = True,
        use_file: bool = True
    ) -> Optional[Any]:
        """
        获取缓存数据
        
        Args:
            prefix: 键前缀
            key_data: 用于生成键的数据
            use_memory: 是否使用内存缓存
            use_file: 是否使用文件缓存
            
        Returns:
            缓存数据
        """
        key = self._generate_key(prefix, key_data)
        
        # 优先从内存缓存获取
        if use_memory:
            data = self.get_memory(key)
            if data is not None:
                return data
        
        # 从文件缓存获取
        if use_file:
            data = self.get_file(key)
            if data is not None:
                # 加载到内存缓存
                if use_memory:
                    self.set_memory(key, data)
                return data
        
        return None
    
    def set(
        self,
        prefix: str,
        key_data: Any,
        data: Any,
        use_memory: bool = True,
        use_file: bool = True
    ):
        """
        设置缓存数据
        
        Args:
            prefix: 键前缀
            key_data: 用于生成键的数据
            data: 缓存数据
            use_memory: 是否使用内存缓存
            use_file: 是否使用文件缓存
        """
        key = self._generate_key(prefix, key_data)
        
        if use_memory:
            self.set_memory(key, data)
        
        if use_file:
            self.set_file(key, data)
    
    def clear(self, prefix: str = None):
        """
        清空缓存
        
        Args:
            prefix: 可选的前缀过滤
        """
        # 清空内存缓存
        if prefix:
            keys_to_delete = [
                key for key in self._memory_cache.keys()
                if key.startswith(prefix)
            ]
            for key in keys_to_delete:
                del self._memory_cache[key]
            logger.info(f"🧹 清空内存缓存 (前缀={prefix})")
        else:
            self._memory_cache.clear()
            logger.info("🧹 清空所有内存缓存")
        
        # 清空文件缓存
        if prefix:
            for file_path in self.cache_dir.glob(f"{prefix}_*.json"):
                file_path.unlink()
            logger.info(f"🧹 清空文件缓存 (前缀={prefix})")
        else:
            for file_path in self.cache_dir.glob("*.json"):
                file_path.unlink()
            logger.info("🧹 清空所有文件缓存")
    
    def cleanup_expired(self):
        """
        清理所有过期缓存
        """
        count = 0
        
        # 清理内存缓存
        expired_keys = []
        for key, cache_entry in self._memory_cache.items():
            timestamp = cache_entry.get('timestamp', 0)
            if (time.time() - timestamp) > self.memory_ttl:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self._memory_cache[key]
            count += 1
        
        # 清理文件缓存
        for file_path in self.cache_dir.glob("*.json"):
            file_mtime = file_path.stat().st_mtime
            if (time.time() - file_mtime) > self.file_ttl:
                file_path.unlink()
                count += 1
        
        if count > 0:
            logger.info(f"🧹 清理 {count} 个过期缓存项")


# 缓存装饰器
def cached(
    prefix: str,
    ttl: int = 300,
    use_memory: bool = True,
    use_file: bool = False
):
    """
    缓存装饰器
    
    Args:
        prefix: 缓存键前缀
        ttl: 过期时间 (秒)
        use_memory: 是否使用内存缓存
        use_file: 是否使用文件缓存
        
    使用示例:
        @cached(prefix="oi_data", ttl=300)
        def get_open_interest(symbol: str):
            # ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存键
            cache = DataCache()
            key_data = {'args': args, 'kwargs': kwargs}
            
            # 尝试从缓存获取
            cached_data = cache.get(
                prefix,
                key_data,
                use_memory=use_memory,
                use_file=use_file
            )
            
            if cached_data is not None:
                return cached_data
            
            # 调用原函数
            result = func(*args, **kwargs)
            
            # 保存到缓存
            cache.set(
                prefix,
                key_data,
                result,
                use_memory=use_memory,
                use_file=use_file
            )
            
            return result
        
        return wrapper
    return decorator


# 全局缓存实例
data_cache = DataCache()
