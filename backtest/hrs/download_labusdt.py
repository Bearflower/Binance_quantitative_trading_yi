#!/usr/bin/env python3
"""
从服务器下载 LABUSDT K线数据到本地
通过SSH连接服务器，在服务器上调用币安API拉取数据，再下载到本地
"""
import subprocess
import base64
import json
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone


def download_labusdt_klines(days: int = 30):
    """
    从服务器下载LABUSDT的1h K线数据

    Args:
        days: 下载天数
    """
    server_ip = "43.156.242.184"
    server_user = "root"
    ssh_key = "/Users/yl/vscode/inspection_automation/docs/only.pem"

    print(f"通过服务器下载 LABUSDT 最近 {days} 天 K线数据...")

    # 在服务器上执行的Python脚本（服务器有网络访问Binance）
    python_script = f"""
import json
import urllib.request
import ssl

ssl._create_default_https_context = ssl._create_unverified_context

symbol = "LABUSDT"
days = {days}

# 计算时间范围
from datetime import datetime, timedelta, timezone
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(days=days)

start_ms = int(start_time.timestamp() * 1000)
end_ms = int(end_time.timestamp() * 1000)

all_klines = []
current_start = start_ms

while current_start < end_ms:
    ms_per_request = 3600000 * 1000  # 1000根1h K线
    current_end = min(current_start + ms_per_request, end_ms)

    url = (
        f"https://fapi.binance.com/fapi/v1/klines?"
        f"symbol={{symbol}}&interval=1h&startTime={{current_start}}"
        f"&endTime={{current_end}}&limit=1000"
    )

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            if not data:
                break
            all_klines.extend(data)
            current_start = data[-1][0] + 3600000  # 下一根K线
    except Exception as e:
        print(f"ERROR: {{e}}")
        break

# 输出为JSON
result = []
for k in all_klines:
    result.append({{
        "open_time": k[0],
        "open": k[1],
        "high": k[2],
        "low": k[3],
        "close": k[4],
        "volume": k[5],
        "close_time": k[6],
        "quote_volume": k[7],
        "trades": k[8],
    }})

print(f"KLINES_START")
print(json.dumps(result))
print(f"KLINES_END")
""".strip()

    # Base64编码
    encoded = base64.b64encode(python_script.encode()).decode()

    # SSH执行
    ssh_cmd = f"echo {encoded} | base64 -d | python3 -"
    ssh_command = f'ssh -i {ssh_key} -o StrictHostKeyChecking=no {server_user}@{server_ip} "{ssh_cmd}"'

    print("正在通过SSH下载数据...")
    result = subprocess.run(
        ssh_command, shell=True, capture_output=True, text=True, timeout=120
    )

    if result.returncode != 0:
        print(f"SSH执行失败: {result.stderr}")
        return None

    output = result.stdout

    # 提取JSON数据
    if "KLINES_START" not in output:
        print(f"输出格式异常: {output[:500]}")
        return None

    json_start = output.index("KLINES_START") + len("KLINES_START")
    json_end = output.index("KLINES_END")
    json_str = output[json_start:json_end].strip()

    data = []
    try:
        raw = json.loads(json_str)
        for k in raw:
            data.append({
                "open_time": datetime.fromtimestamp(k["open_time"] / 1000, tz=timezone.utc),
                "open": float(k["open"]),
                "high": float(k["high"]),
                "low": float(k["low"]),
                "close": float(k["close"]),
                "volume": float(k["volume"]),
                "quote_volume": float(k.get("quote_volume", 0)),
            })
    except json.JSONDecodeError as e:
        print(f"JSON解析失败: {e}")
        print(f"原始数据: {json_str[:500]}")
        return None

    if not data:
        print("未获取到数据")
        return None

    df = pd.DataFrame(data)
    df.sort_values("open_time", inplace=True)

    # 保存
    output_dir = Path(__file__).parent / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "labusdt_1h.csv"
    df.to_csv(output_file, index=False)

    print(f"✅ 成功下载 {len(df)} 根1h K线")
    print(f"   时间范围: {df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}")
    print(f"   保存路径: {output_file}")

    return df


if __name__ == "__main__":
    df = download_labusdt_klines(days=30)