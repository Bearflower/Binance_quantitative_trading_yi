"""
新币检测模块
监控新上线的币种，计算上线时间
"""
import asyncio
import json
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import structlog

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager


logger = structlog.get_logger()


class ListingDetector:
    """新币上线检测器

    功能：
    - 监控新上线的交易对
    - 计算上线时长
    - 持久化已知币种列表
    - 追踪新币检测记录和OI排名数据
    """

    def __init__(
        self,
        binance_api: BinanceClient,
        db: DatabaseManager,
        config: Dict[str, Any] = None
    ):
        """
        初始化检测器

        Args:
            binance_api: Binance API 客户端
            db: 数据库管理器
            config: 配置字典
        """
        self.binance_api = binance_api
        self.db = db
        self.config = config or {}

        # 延迟加载已知币种（在首次使用时加载）
        self._known_symbols_loaded = False
        self.known_symbols = set()

        # 新币检测记录：追踪首次发现时间和OI
        self._new_coin_records_loaded = False
        self._new_coin_records = {}  # {symbol: {detected_at: str, first_oi: float}}

        # OI排名缓存（单周期内复用）
        self._oi_cache = None
        self._oi_cache_time = None

        # 下架币种缓存（避免重复请求4108错误）
        self._delisted_symbols = set()

        logger.info(
            "新币检测器初始化完成",
            known_symbols_count=len(self.known_symbols)
        )

    def _is_valid_symbol(self, symbol: str) -> bool:
        """
        检查交易对是否为有效的永续合约

        根据配置中的 exclude_patterns 过滤非永续合约（如季度/次季合约）。
        例如 ETHUSDT_261225 包含 "_" 后缀，应被过滤。

        Args:
            symbol: 交易对符号

        Returns:
            True 表示是有效永续合约，False 表示应被过滤
        """
        detector_config = self.config.get('detector', {})
        exclude_patterns = detector_config.get('exclude_patterns', ['_'])
        for pattern in exclude_patterns:
            if pattern in symbol:
                return False
        return True

    async def _load_known_symbols(self):
        """从数据库加载已知币种"""
        if self._known_symbols_loaded:
            return

        try:
            # 尝试从数据库获取已知币种
            state = await self.db.fetch_one(
                "SELECT state_data FROM strategy_states WHERE strategy_name = $1 AND state_key = $2",
                'new_coin',
                'known_symbols'
            )

            if state:
                sd = state.get('state_data', {})
                if isinstance(sd, str):
                    try:
                        sd = json.loads(sd)
                    except json.JSONDecodeError:
                        # DB记录损坏时，清空已知币种列表，让自然检测流程重新积累
                        # 避免调用 _initialize_known_symbols() 导致全量覆盖新币
                        logger.warning("已知币种数据JSON解析失败，清空列表等待重新积累")
                        self.known_symbols = set()
                        self._known_symbols_loaded = True
                        return
                symbols = sd.get('symbols', [])
                if symbols:
                    self.known_symbols = set(symbols)
                    logger.info(f"从数据库加载已知币种: {len(self.known_symbols)} 个")
                else:
                    # DB记录为空时，清空列表等待自然检测积累，不调用 _initialize_known_symbols()
                    # 避免将全部当前币种加入已知列表导致新币丢失
                    logger.info("数据库记录为空，清空列表等待自然积累")
                    self.known_symbols = set()
            else:
                logger.info("首次运行，初始化已知币种列表")
                await self._initialize_known_symbols()

            self._known_symbols_loaded = True

        except Exception as e:
            logger.error(f"加载已知币种失败: {e}")
            # 初始化为空集合
            self.known_symbols = set()
            self._known_symbols_loaded = True

    async def _load_new_coin_records(self):
        """从数据库加载新币检测记录"""
        if self._new_coin_records_loaded:
            return

        try:
            state = await self.db.fetch_one(
                "SELECT state_data FROM strategy_states WHERE strategy_name = $1 AND state_key = $2",
                'new_coin',
                'new_coin_records'
            )

            if state:
                sd = state.get('state_data', {})
                if isinstance(sd, str):
                    try:
                        sd = json.loads(sd)
                    except json.JSONDecodeError:
                        self._new_coin_records = {}
                        self._new_coin_records_loaded = True
                        return
                self._new_coin_records = sd.get('coins', {})
                logger.info(f"从数据库加载新币检测记录: {len(self._new_coin_records)} 条")

            self._new_coin_records_loaded = True

        except Exception as e:
            logger.error(f"加载新币检测记录失败: {e}")
            self._new_coin_records = {}
            self._new_coin_records_loaded = True

    async def _save_new_coin_records(self):
        """保存新币检测记录到数据库"""
        try:
            # 清理过期记录（超过配置的最大上线时间窗口）
            detector_config = self.config.get('detector', {})
            max_hours = detector_config.get('max_listing_hours')
            if max_hours is None:
                logger.error("配置缺失: detector.max_listing_hours，跳过过期记录清理")
                return
            cutoff = datetime.now(timezone.utc)
            expired = []
            for symbol, record in self._new_coin_records.items():
                try:
                    detected_at = datetime.fromisoformat(record['detected_at'])
                    if detected_at.tzinfo is None:
                        detected_at = detected_at.replace(tzinfo=timezone.utc)
                    if (cutoff - detected_at).total_seconds() / 3600 > max_hours:
                        expired.append(symbol)
                except (ValueError, TypeError, KeyError):
                    logger.warning(f"新币记录格式异常,标记为过期: {symbol}")
                    expired.append(symbol)
            for symbol in expired:
                del self._new_coin_records[symbol]

            state_data = {
                'coins': self._new_coin_records,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }

            await self.db.execute(
                """
                INSERT INTO strategy_states (strategy_name, state_key, state_data, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (strategy_name, state_key)
                DO UPDATE SET state_data = $3, updated_at = $4
                """,
                'new_coin',
                'new_coin_records',
                json.dumps(state_data),
                datetime.now(timezone.utc).replace(tzinfo=None)
            )

            logger.debug(f"保存新币检测记录: {len(self._new_coin_records)} 条")

        except Exception as e:
            logger.error(f"保存新币检测记录失败: {e}")

    async def _initialize_known_symbols(self):
        """初始化已知币种列表"""
        try:
            # 获取交易所信息
            exchange_info = await self.binance_api._request(
                "GET",
                "/fapi/v1/exchangeInfo",
                signed=False
            )

            # 提取所有交易对（过滤非永续合约，如季度/次季合约）
            symbols = {
                s['symbol'] for s in exchange_info.get('symbols', [])
                if self._is_valid_symbol(s['symbol'])
            }
            self.known_symbols = symbols

            # 保存到数据库
            await self._save_known_symbols()

            logger.info(f"初始化已知币种完成: {len(self.known_symbols)} 个")

        except Exception as e:
            logger.error(f"初始化已知币种失败: {e}")
            self.known_symbols = set()

    async def _save_known_symbols(self):
        """保存已知币种到数据库"""
        try:
            state_data = {
                'symbols': list(self.known_symbols),
                'updated_at': datetime.now(timezone.utc).isoformat()
            }

            # 使用 UPSERT 保存
            await self.db.execute(
                """
                INSERT INTO strategy_states (strategy_name, state_key, state_data, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (strategy_name, state_key)
                DO UPDATE SET state_data = $3, updated_at = $4
                """,
                'new_coin',
                'known_symbols',
                json.dumps(state_data),
                datetime.now(timezone.utc).replace(tzinfo=None)
            )

            logger.debug(f"保存已知币种: {len(self.known_symbols)} 个")

        except Exception as e:
            logger.error(f"保存已知币种失败: {e}")

    async def detect_new_listings(self) -> List[Dict[str, Any]]:
        """
        检测新上线的币种

        Returns:
            新币信息列表，每个元素包含：
            - symbol: 交易对
            - base_asset: 基础资产
            - quote_asset: 报价资产
            - listing_time: 上线时间
            - listing_hours: 上线时长（小时）
            - status: 状态
        """
        try:
            # 确保已知币种已加载
            await self._load_known_symbols()
            await self._load_new_coin_records()

            # 获取交易所信息
            exchange_info = await self.binance_api._request(
                "GET",
                "/fapi/v1/exchangeInfo",
                signed=False
            )

            # 提取当前所有交易对（过滤非永续合约，如季度/次季合约）
            current_symbols = {
                s['symbol'] for s in exchange_info.get('symbols', [])
                if self._is_valid_symbol(s['symbol'])
            }

            # 找出新币
            new_symbols = current_symbols - self.known_symbols

            if new_symbols:
                logger.info(f"检测到 {len(new_symbols)} 个新币: {new_symbols}")
                # 仅将已存在的币种标记为已知，新币保留以便下周期重新检测（等待K线数据就绪）
                self.known_symbols = current_symbols - new_symbols
            else:
                self.known_symbols = current_symbols

            await self._save_known_symbols()

            # 记录新币检测时间和首次OI
            now_str = datetime.now(timezone.utc).isoformat()
            for symbol in new_symbols:
                if symbol not in self._new_coin_records:
                    # 预上市合约（onboardDate在未来）跳过OI获取，避免-4108错误
                    if self._is_premarket_coin(symbol, exchange_info):
                        oi_usd = 0.0
                        logger.info(f"预上市合约跳过OI获取: {symbol}, OI={oi_usd}")
                    else:
                        try:
                            oi_usd = await self._fetch_oi_for_symbol(symbol)
                        except Exception as e:
                            logger.warning(f"新币检测记录保存失败: {symbol}, 错误: {e}")
                            continue
                    self._new_coin_records[symbol] = {
                        'detected_at': now_str,
                        'first_oi': oi_usd
                    }
                    logger.info(f"新币检测记录已保存: {symbol}, OI={oi_usd}")

            if new_symbols:
                await self._save_new_coin_records()

            # 获取新币详细信息
            new_coins = []
            for symbol in new_symbols:
                coin_info = self._get_coin_info(symbol, exchange_info)
                if coin_info:
                    new_coins.append(coin_info)

            return new_coins

        except Exception as e:
            logger.error(f"检测新币失败: {e}")
            return []

    def _get_coin_info(
        self,
        symbol: str,
        exchange_info: Dict
    ) -> Optional[Dict[str, Any]]:
        """
        获取币种信息

        Args:
            symbol: 交易对
            exchange_info: 交易所信息

        Returns:
            币种信息字典
        """
        try:
            # 从交易所信息中查找币种详情
            for s in exchange_info.get('symbols', []):
                if s['symbol'] == symbol:
                    # 获取上线时间
                    listing_time = s.get('onboardDate', 0)
                    if listing_time:
                        listing_datetime = datetime.fromtimestamp(listing_time / 1000, tz=timezone.utc)
                        listing_hours = (datetime.now(timezone.utc) - listing_datetime).total_seconds() / 3600
                        if listing_hours < 0:
                            listing_hours = abs(listing_hours)
                            logger.info(
                                f"币种 {symbol} 尚未上线（预计上线时间: {listing_datetime}），"
                                f"约 {listing_hours:.1f} 小时后上线"
                            )
                    else:
                        listing_datetime = datetime.now(timezone.utc)
                        listing_hours = 0.0

                    coin_info = {
                        'symbol': symbol,
                        'base_asset': s.get('baseAsset', ''),
                        'quote_asset': s.get('quoteAsset', ''),
                        'listing_time': listing_datetime,
                        'listing_hours': listing_hours,
                        'status': s.get('status', '')
                    }

                    logger.info(
                        f"获取新币信息: {symbol}",
                        base_asset=coin_info['base_asset'],
                        listing_hours=listing_hours
                    )

                    return coin_info

            logger.warning(f"未找到币种信息: {symbol}")
            return None

        except Exception as e:
            logger.error(f"获取币种信息失败 {symbol}: {e}")
            return None

    def _is_premarket_coin(self, symbol: str, exchange_info: dict) -> bool:
        """
        检查币种是否为预上市合约（onboardDate在未来）

        Args:
            symbol: 交易对符号
            exchange_info: 交易所信息

        Returns:
            True 表示是预上市合约，不应请求交易数据
        """
        current_time_ms = datetime.now(timezone.utc).timestamp() * 1000
        for s in exchange_info.get('symbols', []):
            if s['symbol'] == symbol:
                onboard_date = s.get('onboardDate', 0)
                return bool(onboard_date and onboard_date > current_time_ms)
        return False

    async def _fetch_oi_for_symbol(self, symbol: str) -> float:
        """
        从币安API获取指定交易对的OI

        无效币种会被缓存到 _delisted_symbols，避免重复请求产生4108/-9999错误日志。

        Args:
            symbol: 交易对

        Returns:
            OI金额（美元），无效币种返回0.0
        """
        # 检查缓存，跳过已确认无效的币种
        if symbol in self._delisted_symbols:
            return 0.0

        try:
            data = await self.binance_api._request(
                "GET",
                "/fapi/v1/openInterest",
                params={'symbol': symbol},
                signed=False
            )
            return float(data.get('openInterest', 0))
        except Exception as e:
            err_msg = str(e)
            # 检测4108（已下架/交割）或-9999（未上线合约/废弃端点），缓存后静默跳过
            if '-4108' in err_msg or '-9999' in err_msg:
                self._delisted_symbols.add(symbol)
                logger.info(f"币种无效（无法获取合约数据），加入跳过列表: {symbol}")
            else:
                logger.warning(f"获取OI失败: {symbol}, 错误: {e}")
            return 0.0

    async def get_recent_coins_oi(self, limit: int = None) -> List[float]:
        """
        获取最近新币的 OI 列表（用于排名对比）

        通过币安API获取最近检测到的新币的当前OI值，
        用于评分引擎中的OI排名计算。结果在当前周期内缓存复用。

        Args:
            limit: 数量限制（None则从配置读取）

        Returns:
            OI 列表（美元金额）
        """
        try:
            # 从配置读取数量限制和缓存TTL
            detector_config = self.config.get('detector', {})
            if limit is None:
                limit = detector_config.get('oi_rank_limit')
            oi_cache_ttl = detector_config.get('oi_cache_ttl_seconds')
            if limit is None or oi_cache_ttl is None:
                logger.error("配置缺失: detector.oi_rank_limit 或 detector.oi_cache_ttl_seconds")
                return []

            # 检查缓存（同一周期内复用）
            if self._oi_cache is not None and self._oi_cache_time is not None:
                cache_age = (datetime.now(timezone.utc) - self._oi_cache_time).total_seconds()
                if cache_age < oi_cache_ttl:
                    logger.debug(f"使用缓存的OI排名数据: {len(self._oi_cache)} 个币种")
                    return self._oi_cache[:limit]

            # 保留无效币种缓存（不清空）：-4108（交割/结算中）和-9999（未知币种）均为永久状态
            # 已交割/结算的币种不会重新上线，新币种名称不同不会出现在缓存中
            # 保持缓存持久化可避免每周期重复查询产生-4108警告日志

            # 确保数据已加载
            await self._load_new_coin_records()

            if not self._new_coin_records:
                logger.debug("无新币检测记录，OI排名无数据对比")
                return []

            # 过滤有效期内的新币（在配置的时间窗口内）
            max_hours = detector_config.get('max_listing_hours')
            if max_hours is None:
                logger.error("配置缺失: detector.max_listing_hours")
                return []
            cutoff = datetime.now(timezone.utc)

            valid_symbols = []
            for symbol, record in self._new_coin_records.items():
                try:
                    detected_at = datetime.fromisoformat(record['detected_at'])
                    hours_since = (cutoff - detected_at).total_seconds() / 3600
                    if 0 < hours_since <= max_hours:
                        valid_symbols.append(symbol)
                except (ValueError, TypeError):
                    continue

            if not valid_symbols:
                logger.debug("无有效期内新币，OI排名无数据对比")
                return []

            # 并发获取所有有效新币的当前OI（跳过已下架币种）
            valid_symbols = [s for s in valid_symbols if s not in self._delisted_symbols]
            logger.info(f"获取 {len(valid_symbols)} 个最近新币的OI用于排名: {valid_symbols[:5]}")
            tasks = [self._fetch_oi_for_symbol(s) for s in valid_symbols]
            oi_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 过滤掉失败的结果
            oi_list = []
            for i, result in enumerate(oi_results):
                if isinstance(result, Exception):
                    logger.warning(f"获取 {valid_symbols[i]} OI失败: {result}")
                    continue
                if result > 0:
                    oi_list.append(result)

            # 更新缓存
            self._oi_cache = oi_list
            self._oi_cache_time = datetime.now(timezone.utc)

            logger.info(f"OI排名数据已更新: {len(oi_list)} 个币种")

            return oi_list[:limit]

        except Exception as e:
            logger.error(f"获取最近新币 OI 失败: {e}")
            return []

    def is_known_symbol(self, symbol: str) -> bool:
        """
        检查是否是已知币种

        Args:
            symbol: 交易对

        Returns:
            是否已知
        """
        return symbol in self.known_symbols
