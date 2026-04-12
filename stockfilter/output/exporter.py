"""
数据导出模块
负责导出筛选结果和交易记录到 CSV/Excel
"""

import pandas as pd
import os
from datetime import datetime
from typing import List, Dict, Optional

from utils.logger import get_logger

logger = get_logger()


class DataExporter:
    """数据导出器"""

    def __init__(self, export_dir: str = "output"):
        """
        初始化导出器
        
        Args:
            export_dir: 导出目录
        """
        self.export_dir = export_dir
        if not os.path.exists(export_dir):
            os.makedirs(export_dir, exist_ok=True)

    def export_scan_results(self, results: List[Dict], scan_date: Optional[str] = None) -> str:
        """
        导出筛选结果到 CSV
        
        Args:
            results: 筛选结果列表
            scan_date: 扫描日期
        
        Returns:
            导出文件路径
        """
        if not results:
            logger.warning("筛选结果为空，跳过导出")
            return ""
        
        if scan_date is None:
            scan_date = datetime.now().strftime('%Y-%m-%d')
        
        filename = f"scan_results_{scan_date}.csv"
        filepath = os.path.join(self.export_dir, filename)
        
        df = pd.DataFrame(results)
        
        if 'surge_date' in df.columns:
            df['surge_date'] = pd.to_datetime(df['surge_date']).dt.strftime('%Y-%m-%d')
        
        df.to_csv(filepath, index=False, encoding='utf-8-sig')
        
        logger.info(f"筛选结果已导出：{filepath}（共 {len(df)} 条）")
        return filepath

    def export_trade_history(self, trades: List[Dict]) -> str:
        """
        导出交易记录到 CSV
        
        Args:
            trades: 交易记录列表
        
        Returns:
            导出文件路径
        """
        if not trades:
            logger.warning("交易记录为空，跳过导出")
            return ""
        
        today = datetime.now().strftime('%Y-%m-%d')
        filename = f"trade_history_{today}.csv"
        filepath = os.path.join(self.export_dir, filename)
        
        df = pd.DataFrame(trades)
        
        for date_col in ['entry_date', 'exit_date']:
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col]).dt.strftime('%Y-%m-%d')
        
        df.to_csv(filepath, index=False, encoding='utf-8-sig')
        
        logger.info(f"交易记录已导出：{filepath}（共 {len(df)} 条）")
        return filepath

    def export_to_excel(self, results: List[Dict], filename: str) -> str:
        """
        导出到 Excel（多 sheet）
        
        Args:
            results: 数据列表
            filename: 文件名
        
        Returns:
            导出文件路径
        """
        if not results:
            return ""
        
        filepath = os.path.join(self.export_dir, filename)
        
        with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
            df = pd.DataFrame(results)
            df.to_excel(writer, sheet_name='筛选结果', index=False)
        
        logger.info(f"Excel 导出完成：{filepath}")
        return filepath
