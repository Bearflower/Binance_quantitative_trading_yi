"""
测试标的注册管理器 SymbolRegistry
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta


class TestSymbolRegistry:
    """测试 SymbolRegistry"""

    @pytest.fixture(autouse=True)
    def reset_registry(self):
        """每次测试前重置注册表单例"""
        from services.kline_service.core.registry import SymbolRegistry
        # 重置单例状态
        registry = SymbolRegistry()
        registry._cache = {}
        registry._initialized = False
        # 确保 db_manager 是 Mock
        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            mock_db.get_connection = AsyncMock()
            yield
        # 测试后清理
        registry._cache = {}
        registry._initialized = False

    @pytest.fixture
    def registry(self):
        """获取注册表实例"""
        from services.kline_service.core.registry import SymbolRegistry
        return SymbolRegistry()

    @pytest.fixture
    def mock_conn(self):
        """模拟数据库连接"""
        conn = AsyncMock()
        # 默认 fetch_val 返回 False（表不存在）
        conn.fetch_val = AsyncMock(return_value=False)
        conn.fetch_all = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        return conn

    # ==================== initialize ====================

    @pytest.mark.asyncio
    async def test_initialize_from_database(self, registry, mock_conn, mock_registered_symbol_config):
        """测试 initialize 从数据库加载配置"""
        # 模拟数据库返回数据
        mock_conn.fetch_all.return_value = [
            {
                "id": 1,
                "symbol": "BTCUSDT",
                "intervals": ["15m", "1h", "4h"],
                "registered_at": datetime.now(),
                "expires_at": datetime.now() + timedelta(days=10),
                "duration_days": 10,
                "priority": "high",
                "status": "active",
                "created_by": "system",
                "updated_at": datetime.now(),
            }
        ]

        with patch("services.kline_service.core.registry.db_manager.get_connection",
                   return_value=AsyncMock().__aenter__.return_value):
            # 手动设置 mock_conn 作为上下文管理器返回值
            with patch("services.kline_service.core.registry.db_manager") as mock_db:
                cm = AsyncMock()
                cm.__aenter__.return_value = mock_conn
                mock_db.get_connection.return_value = cm

                await registry.initialize()

                assert registry._initialized is True
                assert "BTCUSDT" in registry._cache
                assert registry._cache["BTCUSDT"].symbol == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_initialize_already_initialized(self, registry):
        """测试 initialize 已初始化时跳过加载"""
        registry._initialized = True
        registry._cache["BTCUSDT"] = MagicMock()

        with patch("services.kline_service.core.registry.SymbolRegistry._load_from_database") as mock_load:
            await registry.initialize()

            # 不重新加载
            mock_load.assert_not_called()
            assert registry._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_database_error(self, registry):
        """测试 initialize 数据库异常"""
        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.side_effect = Exception("数据库连接失败")
            mock_db.get_connection.return_value = cm

            with pytest.raises(Exception, match="数据库连接失败"):
                await registry.initialize()

            assert registry._initialized is False

    # ==================== register ====================

    @pytest.mark.asyncio
    async def test_register_new_symbol(self, registry, mock_register_request, mock_conn):
        """测试注册新标的"""
        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.return_value = mock_conn
            # 模拟第一次检查 EXISTS 返回 False，表示不存在
            mock_conn.fetch_val = AsyncMock(return_value=False)
            mock_db.get_connection.return_value = cm

            config = await registry.register(mock_register_request)

            assert config.symbol == "BTCUSDT"
            assert config.status == "active"
            assert "BTCUSDT" in registry._cache

    @pytest.mark.asyncio
    async def test_register_existing_symbol(self, registry, mock_register_request, mock_conn, mock_registered_symbol_config):
        """测试注册已存在的标的（更新）"""
        # 先放入缓存
        registry._cache["BTCUSDT"] = mock_registered_symbol_config

        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.return_value = mock_conn
            mock_db.get_connection.return_value = cm

            config = await registry.register(mock_register_request)

            assert config.symbol == "BTCUSDT"
            # 已存在则更新，_update_registration 返回缓存中的同一对象
            assert config is mock_registered_symbol_config

    @pytest.mark.asyncio
    async def test_register_database_error(self, registry, mock_register_request):
        """测试注册时数据库写入失败"""
        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.side_effect = Exception("数据库写入失败")
            mock_db.get_connection.return_value = cm

            with pytest.raises(Exception, match="数据库写入失败"):
                await registry.register(mock_register_request)

            # 不应加入缓存
            assert "BTCUSDT" not in registry._cache

    # ==================== unregister ====================

    @pytest.mark.asyncio
    async def test_unregister_success(self, registry, mock_registered_symbol_config, mock_conn):
        """测试注销标的成功"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config

        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.return_value = mock_conn
            mock_db.get_connection.return_value = cm

            result = await registry.unregister("BTCUSDT")

            assert result is True
            assert registry._cache["BTCUSDT"].status == "cancelled"

    @pytest.mark.asyncio
    async def test_unregister_not_found(self, registry):
        """测试注销不存在的标的"""
        result = await registry.unregister("NONEXISTENT")
        assert result is False

    @pytest.mark.asyncio
    async def test_unregister_database_error(self, registry, mock_registered_symbol_config):
        """测试注销时数据库写入失败"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config

        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.side_effect = Exception("数据库写入失败")
            mock_db.get_connection.return_value = cm

            with pytest.raises(Exception, match="数据库写入失败"):
                await registry.unregister("BTCUSDT")

    # ==================== get_active_symbols ====================

    def test_get_active_symbols(self, registry, mock_registered_symbol_config):
        """测试获取活跃标的列表"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config

        active = registry.get_active_symbols()

        assert len(active) == 1
        assert active[0].symbol == "BTCUSDT"

    def test_get_active_symbols_empty(self, registry):
        """测试无活跃标的时返回空列表"""
        active = registry.get_active_symbols()
        assert active == []

    def test_get_active_symbols_excludes_expired(self, registry, mock_registered_symbol_config, mock_expired_config):
        """测试 get_active_symbols 排除过期标的"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config
        registry._cache["ETHUSDT"] = mock_expired_config

        active = registry.get_active_symbols()

        assert len(active) == 1
        assert active[0].symbol == "BTCUSDT"

    # ==================== cleanup_expired ====================

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, registry, mock_registered_symbol_config, mock_expired_config, mock_conn):
        """测试清理过期标的"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config
        registry._cache["ETHUSDT"] = mock_expired_config

        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.return_value = mock_conn
            mock_db.get_connection.return_value = cm

            cleaned = await registry.cleanup_expired()

            assert cleaned == 1
            assert registry._cache["ETHUSDT"].status == "expired"
            # BTCUSDT 未过期，状态不变
            assert registry._cache["BTCUSDT"].status == "active"

    @pytest.mark.asyncio
    async def test_cleanup_expired_none(self, registry, mock_registered_symbol_config, mock_conn):
        """测试无过期标的中"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config

        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.return_value = mock_conn
            mock_db.get_connection.return_value = cm

            cleaned = await registry.cleanup_expired()

            assert cleaned == 0

    # ==================== renew ====================

    @pytest.mark.asyncio
    async def test_renew_symbol(self, registry, mock_registered_symbol_config, mock_conn):
        """测试续期标的"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config

        with patch("services.kline_service.core.registry.db_manager") as mock_db:
            cm = AsyncMock()
            cm.__aenter__.return_value = mock_conn
            mock_db.get_connection.return_value = cm

            result = await registry.renew("BTCUSDT", 5)

            assert result is not None
            assert result.symbol == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_renew_not_found(self, registry):
        """测试续期不存在的标的"""
        result = await registry.renew("NONEXISTENT", 5)
        assert result is None

    # ==================== get_symbol_config ====================

    def test_get_symbol_config(self, registry, mock_registered_symbol_config):
        """测试获取指定标的配置"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config

        config = registry.get_symbol_config("BTCUSDT")
        assert config is not None
        assert config.symbol == "BTCUSDT"

    def test_get_symbol_config_not_found(self, registry):
        """测试获取不存在的标的配置"""
        config = registry.get_symbol_config("NONEXISTENT")
        assert config is None

    def test_get_symbol_config_expired(self, registry, mock_expired_config):
        """测试获取过期标的配置返回 None"""
        registry._cache["ETHUSDT"] = mock_expired_config

        config = registry.get_symbol_config("ETHUSDT")
        assert config is None

    # ==================== get_all_configs ====================

    def test_get_all_configs_active_only(self, registry, mock_registered_symbol_config, mock_expired_config):
        """测试 get_all_configs 只返回活跃的"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config
        registry._cache["ETHUSDT"] = mock_expired_config

        configs = registry.get_all_configs(include_inactive=False)
        assert len(configs) == 1

    def test_get_all_configs_include_inactive(self, registry, mock_registered_symbol_config, mock_expired_config):
        """测试 get_all_configs 包含非活跃的"""
        registry._cache["BTCUSDT"] = mock_registered_symbol_config
        registry._cache["ETHUSDT"] = mock_expired_config

        configs = registry.get_all_configs(include_inactive=True)
        assert len(configs) == 2