#!/usr/bin/env python3
import requests
import json

url = "http://localhost:8766/api/v1/send"
data = {
    "project": "btc_eth",
    "message": "测试通知服务",
    "type": "text",
    "level": "info"
}

print(f"发送请求到：{url}")
print(f"数据：{json.dumps(data, ensure_ascii=False)}")

try:
    response = requests.post(url, json=data, timeout=10)
    print(f"状态码：{response.status_code}")
    print(f"响应：{response.text}")
except Exception as e:
    print(f"错误：{e}")
