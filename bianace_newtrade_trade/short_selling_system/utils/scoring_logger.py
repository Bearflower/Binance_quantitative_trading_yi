"""
评分日志记录模块

负责：
- 记录每次评分的详细信息
- 保存评分报告到 JSON 文件
- 管理日志文件清理
"""

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from config.settings import settings
from utils.logger import logger


class ScoringLogger:
    """评分日志记录器"""
    
    def __init__(self, log_dir: Optional[str] = None):
        """
        初始化评分日志记录器
        
        Args:
            log_dir: 日志目录路径，默认使用配置文件中的设置
        """
        if log_dir:
            self.log_dir = Path(log_dir)
        else:
            self.log_dir = Path(settings.scoring_log_dir)
        
        # 确保目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"✅ 评分日志记录器初始化完成 (目录：{self.log_dir})")
    
    def _get_today_dir(self) -> Path:
        """
        获取今天的日志目录
        
        Returns:
            今天的日志目录路径
        """
        today = datetime.now().strftime('%Y-%m-%d')
        today_dir = self.log_dir / today
        today_dir.mkdir(parents=True, exist_ok=True)
        return today_dir
    
    def log_scoring_report(
        self,
        symbol: str,
        scoring_data: Dict[str, Any],
        scoring_attempt: int = 1
    ) -> Optional[Path]:
        """
        记录评分报告
        
        Args:
            symbol: 币种符号
            scoring_data: 评分数据，应包含：
                - listing_time: 上线时间
                - hours_since_listing: 上线至今小时数
                - scores: 各维度评分
                - total_score: 综合评分
                - signal_generated: 是否生成信号
                - missing_data: 缺失数据列表
            scoring_attempt: 第几次评分
            
        Returns:
            保存的文件路径，失败返回 None
        """
        try:
            # 生成文件名
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{symbol}_attempt{scoring_attempt}_{timestamp}_score_report.json"
            
            # 完整文件路径
            file_path = self._get_today_dir() / filename
            
            # 准备报告数据
            report_data = {
                "symbol": symbol,
                "scoring_timestamp": datetime.now().isoformat(),
                "scoring_attempt": scoring_attempt,
                **scoring_data
            }
            
            # 保存到文件
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(report_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"💾 评分报告已保存：{file_path}")
            return file_path
            
        except Exception as e:
            logger.error(f"❌ 保存评分报告失败：{e}")
            return None
    
    def get_scoring_reports(
        self,
        symbol: str,
        days: int = 7
    ) -> List[Path]:
        """
        获取某个币种的历史评分报告
        
        Args:
            symbol: 币种符号
            days: 查询最近 N 天
            
        Returns:
            报告文件路径列表
        """
        reports = []
        today = datetime.now()
        
        for i in range(days):
            date = today - timedelta(days=i)
            date_dir = self.log_dir / date.strftime('%Y-%m-%d')
            
            if not date_dir.exists():
                continue
            
            # 查找匹配的报告文件
            for file_path in date_dir.glob(f"{symbol}_*_score_report.json"):
                reports.append(file_path)
        
        # 按时间排序（最新的在前）
        reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        
        return reports
    
    def load_scoring_report(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """
        加载评分报告
        
        Args:
            file_path: 报告文件路径
            
        Returns:
            报告数据，失败返回 None
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ 加载评分报告失败：{e}")
            return None
    
    def get_latest_report(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取某个币种的最新评分报告
        
        Args:
            symbol: 币种符号
            
        Returns:
            最新报告数据，失败返回 None
        """
        reports = self.get_scoring_reports(symbol)
        
        if not reports:
            return None
        
        return self.load_scoring_report(reports[0])
    
    def cleanup_old_reports(self, retention_days: Optional[int] = None) -> int:
        """
        清理过期的评分报告
        
        Args:
            retention_days: 保留天数，默认使用配置文件中的设置
            
        Returns:
            清理的文件数量
        """
        if retention_days is None:
            retention_days = settings.scoring_report_retention_days
        
        cleaned_count = 0
        cutoff_date = datetime.now() - timedelta(days=retention_days)
        
        try:
            # 遍历日志目录
            for date_dir in self.log_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                
                try:
                    # 解析目录名（日期格式：YYYY-MM-DD）
                    dir_date = datetime.strptime(date_dir.name, '%Y-%m-%d')
                    
                    if dir_date < cutoff_date:
                        # 删除整个目录
                        shutil.rmtree(date_dir)
                        deleted_files = len(list(date_dir.glob('*.json')))
                        cleaned_count += deleted_files
                        logger.info(f"🗑️  清理过期目录：{date_dir.name} ({deleted_files} 个文件)")
                except ValueError:
                    # 不是日期格式的目录，跳过
                    continue
            
            logger.info(f"✅ 清理完成，共删除 {cleaned_count} 个过期报告")
            
        except Exception as e:
            logger.error(f"❌ 清理过期报告失败：{e}")
        
        return cleaned_count
    
    def get_all_reports_summary(
        self,
        days: int = 7
    ) -> Dict[str, Any]:
        """
        获取最近 N 天的评分报告摘要
        
        Args:
            days: 统计最近 N 天
            
        Returns:
            报告摘要统计
        """
        today = datetime.now()
        summary = {
            "total_reports": 0,
            "unique_symbols": set(),
            "average_score": 0,
            "signal_count": 0,
            "daily_count": {}
        }
        
        total_score = 0
        score_count = 0
        
        for i in range(days):
            date = today - timedelta(days=i)
            date_str = date.strftime('%Y-%m-%d')
            date_dir = self.log_dir / date_str
            
            if not date_dir.exists():
                summary["daily_count"][date_str] = 0
                continue
            
            daily_count = 0
            for file_path in date_dir.glob('*.json'):
                report = self.load_scoring_report(file_path)
                if not report:
                    continue
                
                daily_count += 1
                summary["total_reports"] += 1
                summary["unique_symbols"].add(report.get('symbol', ''))
                
                # 统计评分
                total_score_val = report.get('total_score')
                if total_score_val is not None:
                    total_score += total_score_val
                    score_count += 1
                
                # 统计信号
                if report.get('signal_generated'):
                    summary["signal_count"] += 1
            
            summary["daily_count"][date_str] = daily_count
        
        # 计算平均分
        if score_count > 0:
            summary["average_score"] = round(total_score / score_count, 2)
        
        # 转换集合为列表（JSON 序列化）
        summary["unique_symbols"] = list(summary["unique_symbols"])
        
        return summary


# 全局评分日志实例
scoring_logger = ScoringLogger()
