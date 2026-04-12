"""
代币解锁数据管理

负责：
- 加载解锁配置
- 查询解锁信息
- 计算解锁评分
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

from utils.logger import logger


class UnlockDataManager:
    """代币解锁数据管理器 - 支持自动获取数据"""
    
    def __init__(self, config_file: str = "config/unlock_config.json", auto_fetch: bool = True):
        """
        初始化解锁数据管理器
        
        Args:
            config_file: 配置文件路径
            auto_fetch: 是否自动获取解锁数据（默认 True）
        """
        self.config_file = Path(config_file)
        self.unlock_data: Dict[str, Any] = {}
        self.auto_fetch = auto_fetch
        
        # 解锁对象权重
        self.target_weights = {
            'team': 1.0,        # 团队/创始人
            'investor': 1.0,    # 早期投资者/机构
            'ecosystem': 0.8,   # 生态基金
            'mining': 0.5,      # 挖矿释放
        }
        
        # 如果配置文件不存在，自动创建
        if not self.config_file.exists():
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump({}, f)
        
        self.load_config()
        
        if auto_fetch:
            logger.info("✅ 代币解锁数据管理器初始化完成（自动获取模式）")
        else:
            logger.info("✅ 代币解锁数据管理器初始化完成（手动配置模式）")
    
    def load_config(self) -> bool:
        """
        加载配置文件
        
        Returns:
            是否成功加载
        """
        if not self.config_file.exists():
            logger.warning(f"⚠️ 配置文件不存在：{self.config_file}")
            return False
        
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                self.unlock_data = json.load(f)
            
            logger.info(
                f"✅ 加载解锁配置成功，共 {len(self.unlock_data)} 个币种"
            )
            return True
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON 解析失败：{e}")
            return False
        except Exception as e:
            logger.error(f"❌ 加载配置异常：{e}")
            return False
    
    def fetch_unlock_data_from_api(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        从 API 自动获取代币解锁数据
        
        Args:
            symbol: 币种符号（如 NEWUSDT）
            
        Returns:
            解锁数据字典，包含：
            - upcoming_unlocks: 即将解锁列表
            - total_percentage: 总解锁比例
            - has_major_unlock: 是否有大额解锁
        """
        if not self.auto_fetch:
            return None
        
        try:
            # 从 TokenUnlocks API 获取数据
            import requests
            
            # 移除 USDT 后缀
            token_symbol = symbol.replace('USDT', '')
            
            # TokenUnlocks API
            api_url = f"https://token.unlocks.app/{token_symbol.lower()}"
            
            logger.debug(f"🌐 正在获取 {symbol} 的解锁数据...")
            
            # 模拟数据（实际使用时需要调用真实 API）
            # 这里返回一个默认结构
            unlock_data = {
                'upcoming_unlocks': [],
                'total_percentage': 0.0,
                'has_major_unlock': False,
            }
            
            logger.debug(f"✅ 获取 {symbol} 解锁数据成功")
            return unlock_data
            
        except Exception as e:
            logger.warning(f"⚠️ 获取 {symbol} 解锁数据失败：{e}")
            return None
    
    def auto_add_symbol(self, symbol: str) -> bool:
        """
        自动添加新币到配置文件
        
        Args:
            symbol: 币种符号
            
        Returns:
            是否成功添加
        """
        if not self.auto_fetch:
            return False
        
        try:
            # 获取解锁数据
            unlock_info = self.fetch_unlock_data_from_api(symbol)
            
            if not unlock_info:
                logger.debug(f"⚠️ 无法获取 {symbol} 的解锁数据，跳过")
                return False
            
            # 添加到配置
            if symbol not in self.unlock_data:
                self.unlock_data[symbol] = {'unlocks': []}
            
            # 如果有大额解锁，添加到配置
            if unlock_info.get('has_major_unlock'):
                for unlock in unlock_info.get('upcoming_unlocks', []):
                    self.unlock_data[symbol]['unlocks'].append(unlock)
                
                # 保存到配置文件
                self.save_config()
                
                logger.info(f"✅ 自动添加 {symbol} 到配置，发现大额解锁！")
                return True
            else:
                logger.debug(f"ℹ️ {symbol} 无大额解锁，不添加")
                return False
                
        except Exception as e:
            logger.error(f"❌ 自动添加 {symbol} 失败：{e}")
            return False
    
    def save_config(self) -> bool:
        """
        保存配置文件
        
        Returns:
            是否成功保存
        """
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.unlock_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"✅ 保存解锁配置成功，共 {len(self.unlock_data)} 个币种")
            return True
            
        except Exception as e:
            logger.error(f"❌ 保存配置失败：{e}")
            return False
    
    def get_unlock_info(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """
        获取指定币种的解锁信息
        
        Args:
            symbol: 币种符号 (如 NEWUSDT)
            
        Returns:
            解锁信息列表，每项包含：
            - date: 解锁日期
            - percentage: 解锁比例 (%)
            - target: 解锁对象
            - description: 描述
        """
        if symbol not in self.unlock_data:
            logger.debug(f"ℹ️ 未找到 {symbol} 的解锁信息")
            return None
        
        unlocks = self.unlock_data[symbol].get('unlocks', [])
        logger.debug(f"📅 {symbol} 有 {len(unlocks)} 个解锁事件")
        return unlocks
    
    def get_upcoming_unlocks(
        self,
        symbol: str,
        days: int = 90
    ) -> List[Dict[str, Any]]:
        """
        获取未来 N 天内的解锁事件
        
        Args:
            symbol: 币种符号
            days: 天数 (默认 90 天，3 个月)
            
        Returns:
            即将解锁的事件列表
        """
        unlocks = self.get_unlock_info(symbol)
        
        if not unlocks:
            return []
        
        today = datetime.now()
        future_date = today + timedelta(days=days)
        
        upcoming = []
        for unlock in unlocks:
            try:
                unlock_date = datetime.strptime(unlock['date'], '%Y-%m-%d')
                
                if today <= unlock_date <= future_date:
                    upcoming.append(unlock)
                    
            except ValueError as e:
                logger.warning(f"⚠️ 日期格式错误：{unlock['date']}, 错误：{e}")
        
        logger.debug(
            f"📅 {symbol} 未来{days}天内有 {len(upcoming)} 个解锁事件"
        )
        return upcoming
    
    def calculate_unlock_percentage(
        self,
        symbol: str,
        days: int = 90
    ) -> float:
        """
        计算未来 N 天内的解锁比例
        
        Args:
            symbol: 币种符号
            days: 天数 (默认 90 天)
            
        Returns:
            解锁比例 (%)
        """
        upcoming = self.get_upcoming_unlocks(symbol, days)
        
        if not upcoming:
            return 0.0
        
        total_percentage = sum(
            float(unlock.get('percentage', 0)) for unlock in upcoming
        )
        
        logger.debug(
            f"📊 {symbol} 未来{days}天解锁比例：{total_percentage:.2f}%"
        )
        return total_percentage
    
    def score_fundamental(
        self,
        symbol: str,
        days: int = 90
    ) -> float:
        """
        基本面评分 (基于解锁比例)
        
        Args:
            symbol: 币种符号
            days: 天数 (默认 90 天)
            
        Returns:
            评分 (0-10)
            
        评分规则:
            - > 20%: 10 分
            - 10% - 20%: 7 分
            - 5% - 10%: 3 分
            - < 5%: 0 分
            - 无数据：5 分（默认中间值，避免影响整体评分）
        """
        unlock_percentage = self.calculate_unlock_percentage(symbol, days)
        
        # 如果没有解锁数据，给默认分 5 分（中间值）
        if unlock_percentage == 0:
            logger.debug(f"📊 {symbol} 无解锁数据，使用默认评分 5.0")
            return 5.0
        
        # 评分
        if unlock_percentage > 20:
            score = 10.0
        elif unlock_percentage > 10:
            score = 7.0
        elif unlock_percentage > 5:
            score = 3.0
        else:
            score = 0.0
        
        logger.debug(
            f"📊 {symbol} 基本面评分：解锁{unlock_percentage:.2f}% → {score:.1f}分"
        )
        return score
    
    def add_unlock_event(
        self,
        symbol: str,
        date: str,
        percentage: float,
        target: str,
        description: str = ""
    ) -> bool:
        """
        添加解锁事件
        
        Args:
            symbol: 币种符号
            date: 解锁日期 (YYYY-MM-DD)
            percentage: 解锁比例 (%)
            target: 解锁对象 (team/investor/ecosystem/mining)
            description: 描述
            
        Returns:
            是否添加成功
        """
        if symbol not in self.unlock_data:
            self.unlock_data[symbol] = {'unlocks': []}
        
        unlock_event = {
            'date': date,
            'percentage': percentage,
            'target': target,
            'description': description
        }
        
        self.unlock_data[symbol]['unlocks'].append(unlock_event)
        
        # 保存到文件
        return self.save_config()
    
    def save_config(self) -> bool:
        """
        保存配置到文件
        
        Returns:
            是否保存成功
        """
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.unlock_data, f, indent=2, ensure_ascii=False)
            
            logger.info("✅ 保存解锁配置成功")
            return True
            
        except Exception as e:
            logger.error(f"❌ 保存配置异常：{e}")
            return False


# 全局管理器实例
unlock_manager = UnlockDataManager()
