#!/usr/bin/env python3
"""通过paramiko连接SSH执行命令"""

import paramiko
import sys

def main():
    host = "47.99.141.133"
    username = "root"
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # 尝试使用默认的SSH密钥
        client.connect(host, username=username, timeout=30)
        
        # 执行查询命令
        cmd = """docker exec trading_system-postgres psql -U binance -d trading -c "SELECT symbol, COUNT(*) as trade_count, MAX(created_at) as last_trade_time FROM trade_records WHERE created_at >= NOW() - INTERVAL '7 days' GROUP BY symbol ORDER BY trade_count DESC;" """
        
        stdin, stdout, stderr = client.exec_command(cmd)
        
        print("过去7天交易统计：")
        print(stdout.read().decode())
        
        err = stderr.read().decode()
        if err:
            print("错误:", err)
        
        client.close()
        
    except Exception as e:
        print(f"连接失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
