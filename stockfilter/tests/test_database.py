"""
数据库模块单元测试
"""

import pytest
import os
import sqlite3
from data.database import DatabaseManager


@pytest.fixture
def test_db():
    """测试数据库 fixture"""
    db_path = "test_stock_scanner.db"
    db = DatabaseManager(db_path)
    yield db
    db.close()
    if os.path.exists(db_path):
        os.remove(db_path)


def test_database_creation(test_db):
    """测试数据库表创建"""
    assert test_db.conn is not None
    
    cursor = test_db.conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    
    assert 'stocks' in tables
    assert 'klines' in tables
    assert 'scan_results' in tables
    assert 'positions' in tables
    assert 'push_history' in tables


def test_insert_stocks(test_db):
    """测试插入股票列表"""
    stocks = [
        {'code': '600519', 'name': '贵州茅台', 'symbol': '600519.SH'},
        {'code': '000858', 'name': '五 粮 液', 'symbol': '000858.SZ'}
    ]
    
    test_db.insert_stocks(stocks)
    
    df = test_db.get_stock_list()
    assert len(df) == 2
    assert df.iloc[0]['code'] == '600519'
    assert df.iloc[0]['name'] == '贵州茅台'


def test_insert_klines(test_db):
    """测试插入 K 线数据"""
    stocks = [
        {'code': '600519', 'name': '贵州茅台', 'symbol': '600519.SH'}
    ]
    test_db.insert_stocks(stocks)
    
    klines = [
        {
            'code': '600519',
            'date': '2024-03-20',
            'open': 1600.0,
            'high': 1650.0,
            'low': 1590.0,
            'close': 1640.0,
            'volume': 100000,
            'amount': 164000000.0
        },
        {
            'code': '600519',
            'date': '2024-03-21',
            'open': 1640.0,
            'high': 1680.0,
            'low': 1630.0,
            'close': 1670.0,
            'volume': 120000,
            'amount': 200400000.0
        }
    ]
    
    test_db.insert_klines(klines)
    
    df = test_db.get_klines('600519')
    assert len(df) == 2
    assert df.iloc[0]['close'] == 1640.0
    assert df.iloc[1]['close'] == 1670.0


def test_get_latest_kline_date(test_db):
    """测试获取最新 K 线日期"""
    stocks = [{'code': '600519', 'name': '贵州茅台', 'symbol': '600519.SH'}]
    test_db.insert_stocks(stocks)
    
    klines = [
        {
            'code': '600519',
            'date': '2024-03-20',
            'open': 1600.0, 'high': 1650.0, 'low': 1590.0,
            'close': 1640.0, 'volume': 100000, 'amount': 164000000.0
        }
    ]
    test_db.insert_klines(klines)
    
    latest_date = test_db.get_latest_kline_date('600519')
    assert latest_date == '2024-03-20'


def test_insert_scan_result(test_db):
    """测试插入筛选结果"""
    result = {
        'code': '600519',
        'name': '贵州茅台',
        'score': 85.5,
        'surge_date': '2024-03-20',
        'support_level': 1600.0,
        'current_close': 1650.0,
        'drop_rate': 0.25,
        'min_vol_ratio': 0.45,
        'surge_price': 1640.0,
        'surge_volume_ratio': 2.1,
        'surge_pct': 0.065,
        'low_after_surge': 1620.0
    }
    
    test_db.insert_scan_result(result)
    
    results = test_db.get_scan_results()
    assert len(results) == 1
    assert results.iloc[0]['code'] == '600519'
    assert results.iloc[0]['score'] == 85.5


def test_push_record(test_db):
    """测试推送记录"""
    test_db.insert_push_record('600519', '2024-03-20', 'new_signal')
    
    has_pushed = test_db.has_pushed_today('600519', '2024-03-20')
    assert has_pushed == True
    
    has_pushed = test_db.has_pushed_today('600519', '2024-03-21')
    assert has_pushed == False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
