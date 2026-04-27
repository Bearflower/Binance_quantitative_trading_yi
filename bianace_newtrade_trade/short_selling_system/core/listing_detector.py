"""
新币检测模块（增强版）

负责：
- 检测新上线的永续合约
- 过滤上线时间
- 二次评分机制
- 评分记录管理
"""

import time
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Set, Optional
from pathlib import Path

from .binance_client import binance_client
from config.settings import settings
from utils.logger import logger


def auto_register_kline_service(symbol: str) -> bool:
    """
    自动注册新币到 K 线服务
    
    Args:
        symbol: 交易对符号
        
    Returns:
        是否注册成功
    """
    try:
        # 定义采集周期（根据新币特点，采集更频繁的周期）
        intervals = ["1m", "5m", "15m", "1h", "4h"]
        
        # 注册 10 天（新币波动大，需要更密集的监控）
        duration_days = 10
        
        logger.info(f"📝 自动注册新币 {symbol} 到 K 线服务...")
        
        success = binance_client.register_new_symbol(
            symbol=symbol,
            intervals=intervals,
            duration_days=duration_days,
            priority="high"  # 新币优先级设为 high
        )
        
        if success:
            logger.info(f"✅ 新币 {symbol} 自动注册成功，将采集周期：{intervals}")
        else:
            logger.warning(f"⚠️ 新币 {symbol} 自动注册失败")
        
        return success
        
    except Exception as e:
        logger.error(f"❌ 自动注册新币 {symbol} 失败：{e}")
        return False


class NewListingDetector:
    """新币检测器（支持二次评分）"""
    
    def __init__(self, state_file: str = "data/processed_symbols.json"):
        """
        初始化新币检测器
        
        Args:
            state_file: 已处理币种状态文件
        """
        self.state_file = Path(state_file)
        self.processed_symbols: Dict[str, Dict[str, Any]] = {}
        
        # 加载状态
        self.load_state()
        
        logger.info("✅ 新币检测器初始化完成（支持二次评分）")
    
    def load_state(self) -> bool:
        """
        加载已处理币种状态（支持新旧格式兼容）
        
        Returns:
            是否加载成功
        """
        if not self.state_file.exists():
            logger.info("📂 状态文件不存在，创建新文件")
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            return True
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 兼容旧格式：如果是列表，转换为新格式
            processed_data = data.get('processed_symbols', {})
            
            if isinstance(processed_data, list):
                # 旧格式：["CFGUSDT", "XXXUSDT", ...]
                logger.info("📂 检测到旧格式，自动迁移中...")
                self.processed_symbols = {
                    symbol: {
                        "first_detected": datetime.now().isoformat(),
                        "listing_time": None,
                        "scoring_count": 0,
                        "last_scored": None,
                        "last_score": None,
                        "signal_generated": False,
                        "scoring_history": []
                    }
                    for symbol in processed_data
                }
                self.save_state()  # 保存为新格式
                logger.info(f"✅ 迁移完成，已处理 {len(self.processed_symbols)} 个币种")
            else:
                # 新格式：字典
                self.processed_symbols = processed_data
                logger.info(f"📂 加载状态成功，已处理 {len(self.processed_symbols)} 个币种")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ 加载状态异常：{e}")
            return False
    
    def save_state(self) -> bool:
        """
        保存已处理币种状态
        
        Returns:
            是否保存成功
        """
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(
                    {'processed_symbols': self.processed_symbols},
                    f,
                    indent=2,
                    ensure_ascii=False
                )
            
            logger.info("💾 保存状态成功")
            return True
            
        except Exception as e:
            logger.error(f"❌ 保存状态异常：{e}")
            return False
    
    def _get_symbol_listing_time(self, symbol: str) -> Optional[datetime]:
        """
        获取币种的上线时间
        
        Args:
            symbol: 币种符号
            
        Returns:
            上线时间，失败返回 None
        """
        try:
            symbol_info = binance_client.get_symbol_info(symbol)
            if not symbol_info:
                return None
            
            listing_time = symbol_info.get('onboardDate', 0) or symbol_info.get('listingTime', 0)
            if listing_time <= 0:
                return None
            
            return datetime.fromtimestamp(listing_time / 1000)
        except Exception as e:
            logger.error(f"❌ 获取 {symbol} 上线时间失败：{e}")
            return None
    
    def _get_hours_since_listing(self, listing_time: Optional[datetime]) -> Optional[float]:
        """
        计算上线至今的小时数
        
        Args:
            listing_time: 上线时间
            
        Returns:
            小时数，失败返回 None
        """
        if listing_time is None:
            return None
        
        try:
            hours_since = (datetime.now() - listing_time).total_seconds() / 3600
            return round(hours_since, 2)
        except Exception as e:
            logger.error(f"❌ 计算上线时长失败：{e}")
            return None
    
    def should_rescore(self, symbol: str) -> bool:
        """
        判断币种是否需要进行二次评分
        
        Args:
            symbol: 币种符号
            
        Returns:
            是否需要二次评分
        """
        if not settings.rescore_enabled:
            return False
        
        if symbol not in self.processed_symbols:
            return False  # 新币种，会在 detect_new_listings 中处理
        
        coin_data = self.processed_symbols[symbol]
        
        # 检查评分次数
        scoring_count = coin_data.get('scoring_count', 0)
        if scoring_count >= settings.max_rescore_attempts:
            return False  # 已达到最大评分次数
        
        # 检查上线时间
        listing_time_str = coin_data.get('listing_time')
        if not listing_time_str:
            # 没有上线时间记录，尝试获取
            listing_time = self._get_symbol_listing_time(symbol)
            if listing_time:
                coin_data['listing_time'] = listing_time.isoformat()
                listing_time_str = listing_time.isoformat()
        
        if not listing_time_str:
            logger.warning(f"⚠️  无法获取 {symbol} 的上线时间，跳过二次评分")
            return False
        
        try:
            listing_time = datetime.fromisoformat(listing_time_str)
            hours_since = self._get_hours_since_listing(listing_time)
            
            if hours_since is None or hours_since > settings.rescore_hours_limit:
                return False  # 超过二次评分时间窗口
            
            # 检查评分间隔
            last_scored_str = coin_data.get('last_scored')
            if last_scored_str:
                last_scored = datetime.fromisoformat(last_scored_str)
                minutes_since_last = (datetime.now() - last_scored).total_seconds() / 60
                
                if minutes_since_last < settings.rescore_interval_minutes:
                    return False  # 距离上次评分时间太短
            
            logger.info(
                f"🔄 {symbol} 满足二次评分条件："
                f"上线{hours_since:.1f}小时，"
                f"已评分{scoring_count}次"
            )
            return True
            
        except Exception as e:
            logger.error(f"❌ 判断 {symbol} 是否需要二次评分失败：{e}")
            return False
    
    def update_scoring_record(
        self,
        symbol: str,
        score: float,
        signal_generated: bool = False,
        scoring_details: Optional[Dict[str, Any]] = None
    ):
        """
        更新评分记录
        
        Args:
            symbol: 币种符号
            score: 综合评分
            signal_generated: 是否生成了信号
            scoring_details: 评分详情（可选）
        """
        if symbol not in self.processed_symbols:
            # 初始化新记录
            listing_time = self._get_symbol_listing_time(symbol)
            self.processed_symbols[symbol] = {
                "first_detected": datetime.now().isoformat(),
                "listing_time": listing_time.isoformat() if listing_time else None,
                "scoring_count": 0,
                "last_scored": None,
                "last_score": None,
                "signal_generated": False,
                "scoring_history": []
            }
        
        coin_data = self.processed_symbols[symbol]
        
        # 更新评分记录
        coin_data['scoring_count'] = coin_data.get('scoring_count', 0) + 1
        coin_data['last_scored'] = datetime.now().isoformat()
        coin_data['last_score'] = score
        coin_data['signal_generated'] = signal_generated or coin_data.get('signal_generated', False)
        
        # 添加到评分历史
        scoring_record = {
            "attempt": coin_data['scoring_count'],
            "timestamp": coin_data['last_scored'],
            "score": score,
            "signal_generated": signal_generated,
            "details": scoring_details or {}
        }
        
        if 'scoring_history' not in coin_data:
            coin_data['scoring_history'] = []
        
        coin_data['scoring_history'].append(scoring_record)
        
        # 保存状态
        self.save_state()
        
        logger.info(
            f"💾 更新 {symbol} 评分记录："
            f"第{coin_data['scoring_count']}次评分，"
            f"分数：{score}, "
            f"信号：{'✅' if signal_generated else '❌'}"
        )
    
    def get_rescore_candidates(self) -> List[str]:
        """
        获取待二次评分的币种列表
        
        Returns:
            币种符号列表
        """
        candidates = []
        
        for symbol in self.processed_symbols:
            if self.should_rescore(symbol):
                candidates.append(symbol)
        
        if candidates:
            logger.info(f"📋 找到 {len(candidates)} 个待二次评分的币种：{', '.join(candidates)}")
        
        return candidates
    
    def detect_new_listings(
        self,
        hours: int = 24
    ) -> List[Dict[str, Any]]:
        """
        检测新上市的永续合约（支持二次评分）
        
        Args:
            hours: 检测最近 N 小时上线的合约 (默认 24 小时)
            
        Returns:
            新币信息列表，每项包含：
            - symbol: 币种符号
            - listing_time: 上线时间
            - hours_since_listing: 上线至今小时数
            - is_rescore: 是否为二次评分
        """
        logger.info(f"🔍 开始检测新上市合约 (最近{hours}小时)...")
        
        # 获取交易所信息
        exchange_info = binance_client.get_exchange_info()
        
        if not exchange_info:
            logger.error("❌ 获取交易所信息失败")
            return []
        
        symbols = exchange_info.get('symbols', [])
        current_time = time.time()
        cutoff_time = current_time - (hours * 3600)
        
        new_listings = []
        
        for symbol_info in symbols:
            # 过滤永续合约且正在交易
            # 支持普通永续合约(PERPETUAL)和传统金融永续合约(TRADIFI_PERPETUAL)
            contract_type = symbol_info.get('contractType')
            is_perpetual = contract_type in ['PERPETUAL', 'TRADIFI_PERPETUAL']
            
            if (
                not is_perpetual or
                symbol_info.get('status') != 'TRADING'
            ):
                continue
            
            symbol = symbol_info.get('symbol')
            
            # 获取上线时间
            listing_time = symbol_info.get('onboardDate', 0) or symbol_info.get('listingTime', 0)
            
            # 跳过上线时间为 0 的币种（老币种）
            if listing_time <= 0:
                continue
            
            # 转换为秒级时间戳
            listing_timestamp = listing_time / 1000
            listing_datetime = datetime.fromtimestamp(listing_timestamp)
            hours_since = (current_time - listing_timestamp) / 3600
            
            # 检查是否已处理
            if symbol in self.processed_symbols:
                coin_data = self.processed_symbols[symbol]
                
                # 检查是否需要二次评分
                if self.should_rescore(symbol):
                    logger.info(
                        f"🔄 二次评分：{symbol}, "
                        f"上线时间：{listing_datetime}, "
                        f"距今：{hours_since:.1f}小时，"
                        f"已评分：{coin_data.get('scoring_count', 0)}次"
                    )
                    
                    new_listings.append({
                        'symbol': symbol,
                        'listing_time': listing_datetime,
                        'hours_since_listing': hours_since,
                        'is_rescore': True,
                        'scoring_count': coin_data.get('scoring_count', 0)
                    })
                continue
            
            # 新币种处理逻辑
            if listing_timestamp >= cutoff_time:
                logger.info(
                    f"🆕 发现新上市合约：{symbol}, "
                    f"上线时间：{listing_datetime}, "
                    f"距今：{hours_since:.1f}小时"
                )
                
                # 自动注册到 K 线服务
                auto_register_kline_service(symbol)
                
                new_listings.append({
                    'symbol': symbol,
                    'listing_time': listing_datetime,
                    'hours_since_listing': hours_since,
                    'is_rescore': False,
                })
                
                # 初始化记录
                self.processed_symbols[symbol] = {
                    "first_detected": datetime.now().isoformat(),
                    "listing_time": listing_datetime.isoformat(),
                    "scoring_count": 0,
                    "last_scored": None,
                    "last_score": None,
                    "signal_generated": False,
                    "scoring_history": []
                }
            else:
                # 超过时间范围，标记为已处理
                self.processed_symbols[symbol] = {
                    "first_detected": datetime.now().isoformat(),
                    "listing_time": listing_datetime.isoformat(),
                    "scoring_count": 0,
                    "last_scored": None,
                    "last_score": None,
                    "signal_generated": False,
                    "scoring_history": []
                }
        
        # 保存状态
        if new_listings:
            self.save_state()
        
        logger.info(f"✅ 检测到 {len(new_listings)} 个新上市合约")
        return new_listings
    
    def get_recent_listings(
        self,
        hours: int = 168
    ) -> List[Dict[str, Any]]:
        """
        获取最近 N 小时内上线的合约 (包括已处理的)
        
        Args:
            hours: 时间范围 (默认 168 小时 = 7 天)
            
        Returns:
            币种信息列表
        """
        logger.info(f"📊 获取最近{hours}小时内上线的合约...")
        
        exchange_info = binance_client.get_exchange_info()
        
        if not exchange_info:
            return []
        
        symbols = exchange_info.get('symbols', [])
        current_time = time.time()
        cutoff_time = current_time - (hours * 3600)
        
        recent_listings = []
        
        for symbol_info in symbols:
            # 支持普通永续合约(PERPETUAL)和传统金融永续合约(TRADIFI_PERPETUAL)
            contract_type = symbol_info.get('contractType')
            is_perpetual = contract_type in ['PERPETUAL', 'TRADIFI_PERPETUAL']
            
            if (
                not is_perpetual or
                symbol_info.get('status') != 'TRADING'
            ):
                continue
            
            # 币安使用 onboardDate 字段
            listing_time = symbol_info.get('onboardDate', 0) or symbol_info.get('listingTime', 0)
            
            if listing_time <= 0:
                continue
            
            listing_timestamp = listing_time / 1000
            
            if listing_timestamp >= cutoff_time:
                hours_since = (current_time - listing_timestamp) / 3600
                listing_datetime = datetime.fromtimestamp(listing_timestamp)
                
                recent_listings.append({
                    'symbol': symbol_info.get('symbol'),
                    'listing_time': listing_datetime,
                    'hours_since_listing': hours_since,
                })
        
        # 按上线时间排序
        recent_listings.sort(key=lambda x: x['hours_since_listing'])
        
        logger.info(f"✅ 找到 {len(recent_listings)} 个合约")
        return recent_listings
    
    def is_new_listing(self, symbol: str, hours: int = 168) -> bool:
        """
        判断币种是否为新品
        
        Args:
            symbol: 币种符号
            hours: 时间范围 (默认 168 小时 = 7 天)
            
        Returns:
            是否为新品
        """
        symbol_info = binance_client.get_symbol_info(symbol)
        
        if not symbol_info:
            return False
        
        # 币安使用 onboardDate 字段
        listing_time = symbol_info.get('onboardDate', 0) or symbol_info.get('listingTime', 0)
        
        if listing_time <= 0:
            return True
        
        current_time = time.time()
        listing_timestamp = listing_time / 1000
        hours_since = (current_time - listing_timestamp) / 3600
        
        return hours_since <= hours
    
    def get_scoring_history(self, symbol: str) -> List[Dict[str, Any]]:
        """
        获取币种的评分历史
        
        Args:
            symbol: 币种符号
            
        Returns:
            评分历史列表
        """
        if symbol not in self.processed_symbols:
            return []
        
        return self.processed_symbols[symbol].get('scoring_history', [])
    
    def clear_state(self):
        """
        清空已处理状态
        
        用于重新检测所有币种
        """
        self.processed_symbols.clear()
        self.save_state()
        logger.info("✅ 清空状态成功")


# 全局检测器实例
listing_detector = NewListingDetector()
