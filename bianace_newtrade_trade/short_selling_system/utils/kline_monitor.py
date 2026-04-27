#!/usr/bin/env python3
"""
K 线服务监控模块

功能：
1. 监控 K 线服务健康状态
2. 监控数据采集质量
3. 监控注册标的状态
4. 异常告警
"""

import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from utils.logger import logger


class KlineServiceMonitor:
    """K 线服务监控器"""
    
    def __init__(self, kline_service_url: str = "http://43.156.242.184:8765/api/v1"):
        """
        初始化监控器
        
        Args:
            kline_service_url: K 线服务 API 地址
        """
        self.base_url = kline_service_url
        self.last_check_time: Optional[datetime] = None
        self.consecutive_failures = 0
        self.max_failures = 3  # 最大连续失败次数
        
    def check_health(self) -> bool:
        """
        检查 K 线服务健康状态
        
        Returns:
            是否健康
        """
        try:
            response = requests.get(f"{self.base_url}/health", timeout=5)
            
            if response.status_code == 200:
                self.consecutive_failures = 0
                self.last_check_time = datetime.now()
                logger.debug("✅ K 线服务健康检查通过")
                return True
            else:
                logger.warning(f"⚠️ K 线服务健康检查失败：HTTP {response.status_code}")
                self._handle_failure()
                return False
                
        except Exception as e:
            logger.error(f"❌ K 线服务健康检查异常：{e}")
            self._handle_failure()
            return False
    
    def _handle_failure(self):
        """处理失败"""
        self.consecutive_failures += 1
        self.last_check_time = datetime.now()
        
        if self.consecutive_failures >= self.max_failures:
            logger.error(f"🚨 K 线服务连续失败 {self.consecutive_failures} 次，可能已宕机！")
            # 这里可以添加告警逻辑，如发送飞书通知
    
    def check_data_quality(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        max_age_minutes: int = 120
    ) -> Dict[str, Any]:
        """
        检查数据质量
        
        Args:
            symbol: 交易对
            interval: 周期
            max_age_minutes: 最大允许的数据年龄（分钟）
            
        Returns:
            质量检查结果
        """
        try:
            # 获取最新 K 线
            response = requests.get(
                f"{self.base_url}/klines/latest",
                params={"symbol": symbol, "interval": interval, "limit": 1},
                timeout=10
            )
            
            if response.status_code != 200:
                return {
                    "status": "error",
                    "message": f"HTTP {response.status_code}"
                }
            
            result = response.json()
            if result.get('code') != 0:
                return {
                    "status": "error",
                    "message": result.get('message')
                }
            
            klines = result.get('data', [])
            if not klines:
                return {
                    "status": "warning",
                    "message": "无数据"
                }
            
            # 检查数据年龄
            latest_kline = klines[0]
            close_time = latest_kline.get('close_time', 0) / 1000
            close_datetime = datetime.fromtimestamp(close_time)
            age = (datetime.now() - close_datetime).total_seconds() / 60
            
            if age > max_age_minutes:
                return {
                    "status": "warning",
                    "message": f"数据过期：{age:.1f} 分钟前",
                    "age_minutes": age,
                    "latest_close": close_datetime.isoformat()
                }
            
            return {
                "status": "ok",
                "message": "数据正常",
                "age_minutes": age,
                "latest_close": close_datetime.isoformat(),
                "open_price": latest_kline.get('open_price'),
                "close_price": latest_kline.get('close_price')
            }
            
        except Exception as e:
            logger.error(f"❌ 数据质量检查失败：{e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    def check_registered_symbols(self) -> Dict[str, Any]:
        """
        检查已注册标的状态
        
        Returns:
            检查结果
        """
        try:
            response = requests.get(
                f"{self.base_url}/register",
                timeout=10
            )
            
            if response.status_code != 200:
                return {
                    "status": "error",
                    "message": f"HTTP {response.status_code}"
                }
            
            result = response.json()
            if result.get('code') != 0:
                return {
                    "status": "error",
                    "message": result.get('message')
                }
            
            symbols = result.get('data', [])
            active_count = 0
            expiring_soon = []
            expired_count = 0
            
            now = datetime.now()
            for item in symbols:
                status = item.get('status', '')
                expires_at_str = item.get('expires_at', '')
                
                if status == 'active':
                    active_count += 1
                    
                    # 检查是否即将过期（3 天内）
                    try:
                        expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                        days_remaining = (expires_at - now).days
                        
                        if days_remaining <= 3:
                            expiring_soon.append({
                                "symbol": item.get('symbol'),
                                "expires_at": expires_at_str,
                                "days_remaining": days_remaining
                            })
                    except:
                        pass
                elif status == 'expired':
                    expired_count += 1
            
            return {
                "status": "ok",
                "total_registered": len(symbols),
                "active_count": active_count,
                "expired_count": expired_count,
                "expiring_soon": expiring_soon,
                "expiring_soon_count": len(expiring_soon)
            }
            
        except Exception as e:
            logger.error(f"❌ 检查已注册标的失败：{e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    def run_full_check(self) -> Dict[str, Any]:
        """
        执行完整检查
        
        Returns:
            完整检查结果
        """
        logger.info("🔍 开始 K 线服务全面检查...")
        
        result = {
            "timestamp": datetime.now().isoformat(),
            "health": self.check_health(),
            "data_quality": {},
            "registered_symbols": self.check_registered_symbols()
        }
        
        # 检查主要交易对的数据质量
        for symbol in ["BTCUSDT", "ETHUSDT", "BNBUSDT"]:
            for interval in ["15m", "1h"]:
                key = f"{symbol}_{interval}"
                result["data_quality"][key] = self.check_data_quality(symbol, interval)
        
        # 总结状态
        all_ok = result["health"]
        for dq in result["data_quality"].values():
            if dq.get("status") == "error":
                all_ok = False
                break
        
        result["overall_status"] = "ok" if all_ok else "warning"
        
        if all_ok:
            logger.info("✅ K 线服务全面检查通过")
        else:
            logger.warning("⚠️ K 线服务检查发现异常")
        
        return result


# 全局监控实例
kline_monitor = KlineServiceMonitor()


if __name__ == "__main__":
    # 测试监控功能
    import json
    
    print("=" * 80)
    print("K 线服务监控测试")
    print("=" * 80)
    
    result = kline_monitor.run_full_check()
    print(json.dumps(result, indent=2, ensure_ascii=False))
