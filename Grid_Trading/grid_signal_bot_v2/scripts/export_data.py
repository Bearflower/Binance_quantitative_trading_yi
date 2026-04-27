"""
从服务器数据库导出历史K线数据
使用SSH连接到服务器，执行查询并下载数据
"""

import os
import sys
import json
from datetime import datetime, timedelta
import subprocess

# 添加项目根目录到路径
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
logger = logging.getLogger(__name__)


def export_via_ssh_tunnel(
    server_ip: str = "43.156.242.184",
    server_user: str = "root",
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    days: int = 90,
    output_file: str = "historical_klines.json"
):
    """
    通过SSH隧道导出数据
    
    Args:
        server_ip: 服务器IP
        server_user: 服务器用户名
        symbol: 交易对
        interval: 时间间隔
        days: 天数
        output_file: 输出文件
    """
    logger.info("=" * 60)
    logger.info("📊 从服务器导出历史K线数据")
    logger.info("=" * 60)
    logger.info(f"交易对: {symbol}")
    logger.info(f"时间间隔: {interval}")
    logger.info(f"天数: {days}")
    logger.info(f"输出文件: {output_file}")
    logger.info("=" * 60)
    
    # 计算时间戳
    end_timestamp = int(datetime.now().timestamp())
    start_timestamp = end_timestamp - days * 86400
    
    logger.info(f"开始时间: {datetime.fromtimestamp(start_timestamp)}")
    logger.info(f"结束时间: {datetime.fromtimestamp(end_timestamp)}")
    
    # SQL查询
    sql_query = f"""
SELECT json_agg(
    json_build_object(
        'open_time', EXTRACT(EPOCH FROM open_time) * 1000,
        'open_price', open_price,
        'high_price', high_price,
        'low_price', low_price,
        'close_price', close_price,
        'volume', volume,
        'close_time', EXTRACT(EPOCH FROM close_time) * 1000
    )
)
FROM klines
WHERE symbol = '{symbol}'
  AND interval = '{interval}'
  AND EXTRACT(EPOCH FROM open_time) >= {start_timestamp}
  AND EXTRACT(EPOCH FROM open_time) <= {end_timestamp}
ORDER BY open_time ASC;
"""
    
    # 在服务器上执行查询
    logger.info("\n📡 正在从服务器数据库导出数据...")
    
    # 使用SSH执行命令
    ssh_command = [
        "ssh",
        f"{server_user}@{server_ip}",
        f"docker exec common_service_postgres psql -U binance -d binance_data -t -A -c \"{sql_query}\""
    ]
    
    try:
        result = subprocess.run(
            ssh_command,
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode != 0:
            logger.error(f"SSH命令失败: {result.stderr}")
            logger.info("\n尝试使用备用方法...")
            return export_via_local_query(symbol, interval, days, output_file)
        
        # 解析JSON结果
        json_str = result.stdout.strip()
        if not json_str or json_str == 'null':
            logger.error("查询结果为空")
            return False
        
        klines = json.loads(json_str)
        
        if not klines:
            logger.error("没有获取到数据")
            return False
        
        # 保存到文件
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(klines, f, indent=2)
        
        logger.info(f"\n✅ 数据导出成功！")
        logger.info(f"文件路径: {output_file}")
        logger.info(f"K线数量: {len(klines)}")
        logger.info(f"文件大小: {os.path.getsize(output_file) / 1024:.2f} KB")
        
        return True
        
    except subprocess.TimeoutExpired:
        logger.error("查询超时")
        return False
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析失败: {e}")
        logger.error(f"原始数据: {result.stdout[:500]}")
        return False
    except Exception as e:
        logger.error(f"导出失败: {e}")
        return False


def export_via_local_query(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    days: int = 90,
    output_file: str = "historical_klines.json"
):
    """
    使用本地数据（备用方案）
    
    Args:
        symbol: 交易对
        interval: 时间间隔
        days: 天数
        output_file: 输出文件
    """
    logger.info("\n📂 使用本地数据...")
    
    # 检查是否有本地数据文件
    local_files = [
        "historical_klines_BTCUSDT_1h.json",
        "historical_klines.json",
        "backtest_report.json"
    ]
    
    for local_file in local_files:
        if os.path.exists(local_file):
            logger.info(f"找到本地数据文件: {local_file}")
            
            with open(local_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 如果是回测报告，提取K线数据
            if 'signals' in data:
                logger.info("从回测报告中提取数据...")
                klines = []
                for signal in data['signals']:
                    klines.append({
                        'open_time': int(datetime.strptime(signal['time'], '%Y-%m-%d %H:%M').timestamp() * 1000),
                        'close_price': signal['price']
                    })
                
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(klines, f, indent=2)
                
                logger.info(f"✅ 提取了 {len(klines)} 根K线数据")
                return True
            else:
                # 直接使用本地数据
                logger.info(f"使用本地数据: {len(data)} 根K线")
                return True
    
    logger.error("没有找到可用的本地数据")
    return False


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='从服务器导出历史K线数据')
    parser.add_argument('--server-ip', type=str, default='43.156.242.184', help='服务器IP')
    parser.add_argument('--server-user', type=str, default='root', help='服务器用户名')
    parser.add_argument('--symbol', type=str, default='BTCUSDT', help='交易对')
    parser.add_argument('--interval', type=str, default='1h', help='时间间隔')
    parser.add_argument('--days', type=int, default=90, help='天数')
    parser.add_argument('--output', type=str, default='historical_klines.json', help='输出文件')
    
    args = parser.parse_args()
    
    success = export_via_ssh_tunnel(
        server_ip=args.server_ip,
        server_user=args.server_user,
        symbol=args.symbol,
        interval=args.interval,
        days=args.days,
        output_file=args.output
    )
    
    if not success:
        logger.error("\n❌ 数据导出失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
