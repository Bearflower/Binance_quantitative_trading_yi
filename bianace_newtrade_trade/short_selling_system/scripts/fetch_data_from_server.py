#!/usr/bin/env python3
"""
从服务器获取回测数据
通过 SSH 从 43.156.242.184 服务器获取已下载的币安数据
"""

import paramiko
import scp
import json
from pathlib import Path
import argparse


def fetch_data_from_server(
    server_ip: str = '43.156.242.184',
    username: str = 'root',
    remote_path: str = '/root/short_selling_system/data/backtest_data.json',
    local_path: str = 'data/backtest_data.json',
    key_path: str = None
):
    """
    从服务器获取数据
    
    Args:
        server_ip: 服务器 IP
        username: SSH 用户名
        remote_path: 服务器上的文件路径
        local_path: 本地保存路径
        key_path: SSH 私钥路径（默认 ~/.ssh/id_rsa）
    """
    
    if key_path is None:
        key_path = str(Path.home() / '.ssh' / 'id_rsa')
    
    print("=" * 80)
    print("从服务器获取回测数据")
    print("=" * 80)
    print(f"服务器：{server_ip}")
    print(f"用户：{username}")
    print(f"远程路径：{remote_path}")
    print(f"本地路径：{local_path}")
    print("=" * 80)
    
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        print(f"\n正在连接到 {server_ip}...")
        ssh.connect(
            hostname=server_ip,
            username=username,
            key_filename=key_path,
            timeout=10
        )
        
        print("✅ SSH 连接成功")
        
        sftp = ssh.open_sftp()
        
        print(f"正在获取文件：{remote_path}")
        
        local_path_obj = Path(local_path)
        local_path_obj.parent.mkdir(parents=True, exist_ok=True)
        
        sftp.get(remote_path, local_path)
        
        print(f"✅ 文件下载成功：{local_path}")
        
        with open(local_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        total_klines = sum(
            len(d.get('1d', [])) + len(d.get('4h', [])) + len(d.get('1h', []))
            for d in data.values()
        )
        
        print(f"\n数据统计:")
        print(f"  币种数量：{len(data)}")
        print(f"  总 K 线数：{total_klines} 条")
        
        for symbol in list(data.keys())[:3]:
            symbol_data = data[symbol]
            print(f"  {symbol}:")
            print(f"    1d: {len(symbol_data.get('1d', []))} 条")
            print(f"    4h: {len(symbol_data.get('4h', []))} 条")
            print(f"    1h: {len(symbol_data.get('1h', []))} 条")
        
        if len(data) > 3:
            print(f"  ... 还有 {len(data) - 3} 个币种")
        
        sftp.close()
        ssh.close()
        
        print("\n" + "=" * 80)
        print("✅ 数据获取完成！")
        print("=" * 80)
        
        return True
        
    except FileNotFoundError:
        print(f"\n❌ 错误：SSH 私钥文件不存在：{key_path}")
        print("请确保已配置 SSH 密钥：")
        print("  ssh-keygen -t rsa -b 4096")
        print("  ssh-copy-id root@43.156.242.184")
        return False
        
    except paramiko.AuthenticationException:
        print(f"\n❌ 错误：SSH 认证失败")
        print("请检查 SSH 密钥配置")
        return False
        
    except paramiko.SSHException as e:
        print(f"\n❌ 错误：SSH 连接失败：{e}")
        return False
        
    except FileNotFoundError:
        print(f"\n❌ 错误：服务器上的文件不存在：{remote_path}")
        print("请先在服务器上运行数据获取脚本")
        return False
        
    except Exception as e:
        print(f"\n❌ 错误：{e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='从服务器获取回测数据')
    parser.add_argument('--server', type=str, default='43.156.242.184')
    parser.add_argument('--user', type=str, default='root')
    parser.add_argument('--remote-path', type=str, default='/root/short_selling_system/data/backtest_data.json')
    parser.add_argument('--output', type=str, default='data/backtest_data.json')
    parser.add_argument('--key', type=str, default=None, help='SSH 私钥路径')
    
    args = parser.parse_args()
    
    success = fetch_data_from_server(
        server_ip=args.server,
        username=args.user,
        remote_path=args.remote_path,
        local_path=args.output,
        key_path=args.key
    )
    
    if success:
        print(f"\n下一步:")
        print(f"  python3 scripts/run_short_backtest.py --data {args.output}")
    else:
        print(f"\n数据获取失败，请检查:")
        print(f"  1. SSH 密钥是否配置")
        print(f"  2. 服务器上是否有数据文件")
        print(f"  3. 网络连接是否正常")


if __name__ == '__main__':
    main()
