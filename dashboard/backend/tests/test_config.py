#!/usr/bin/env python3
"""
配置加载测试脚本
验证所有配置项是否正确加载
"""
import sys
import os
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 设置工作目录
os.chdir(project_root)

from dashboard.backend.core.config import settings, config


def test_config_loading():
    """测试配置加载"""
    print("=" * 60)
    print("配置加载测试")
    print("=" * 60)
    
    # 测试应用配置
    print("\n【应用配置】")
    print(f"  版本号: {settings.app_version}")
    print(f"  时区偏移量: {settings.timezone_offset}")
    
    # 验证版本号不是默认值
    assert settings.app_version == "1.0.0", f"版本号应为 1.0.0，实际为 {settings.app_version}"
    assert settings.timezone_offset == 8, f"时区偏移量应为 8，实际为 {settings.timezone_offset}"
    
    # 测试数据库配置
    print("\n【数据库配置】")
    print(f"  主机: {settings.db_host}")
    print(f"  端口: {settings.db_port}")
    print(f"  数据库名: {settings.db_name}")
    print(f"  连接池大小: {settings.db_min_pool_size}-{settings.db_max_pool_size}")
    
    # 验证连接池配置
    assert settings.db_min_pool_size == 5, f"最小连接池应为 5，实际为 {settings.db_min_pool_size}"
    assert settings.db_max_pool_size == 20, f"最大连接池应为 20，实际为 {settings.db_max_pool_size}"
    
    # 测试缓存配置
    print("\n【缓存配置】")
    print(f"  启用状态: {settings.cache_enabled}")
    print(f"  日报缓存 TTL: {settings.cache_ttl_daily} 秒")
    print(f"  周报缓存 TTL: {settings.cache_ttl_weekly} 秒")
    print(f"  月报缓存 TTL: {settings.cache_ttl_monthly} 秒")
    print(f"  元数据缓存 TTL: {settings.cache_ttl_metadata} 秒")
    
    # 验证缓存配置（实时化后 TTL 已缩短）
    assert settings.cache_ttl_daily == 60, f"日报缓存 TTL 应为 60，实际为 {settings.cache_ttl_daily}"
    assert settings.cache_ttl_weekly == 180, f"周报缓存 TTL 应为 180，实际为 {settings.cache_ttl_weekly}"
    assert settings.cache_ttl_monthly == 300, f"月报缓存 TTL 应为 300，实际为 {settings.cache_ttl_monthly}"
    assert settings.cache_ttl_metadata == 86400, f"元数据缓存 TTL 应为 86400，实际为 {settings.cache_ttl_metadata}"
    
    # 测试配置文件加载
    print("\n【配置文件加载】")
    print(f"  配置文件键: {list(config.keys())}")
    
    # 验证配置文件结构
    assert "app" in config, "配置文件应包含 'app' 部分"
    assert "api" in config, "配置文件应包含 'api' 部分"
    assert "database" in config, "配置文件应包含 'database' 部分"
    assert "cache" in config, "配置文件应包含 'cache' 部分"
    
    print("\n" + "=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60)
    
    return True


if __name__ == "__main__":
    try:
        test_config_loading()
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
