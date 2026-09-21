#!/usr/bin/env python3
"""多币种动量轮动 - 受限池日线数据拉取（本地编排器）

本地 Mac 无法直连币安 API（主网 IP 受限），因此本脚本：
1. 将 download_klines_on_server.py 用 scp 上传到生产服务器
2. 在服务器上执行，利用服务器走 Binance API 拉取受限池日线
3. 将结果（klines/ + universe.json）拉回本地 backtest/momentum/data/

这是标准做法：仅把目标数据下载回本地，回测始终在本机执行（符合项目规则）。

受限池构建（上线≥90天、24h成交额≥10M USDT、排除主流币/稳定币/杠杆币等）
的具体逻辑在 download_klines_on_server.py 中，本文件只负责编排与传输。

用法：
    python3 scripts/download_universe_data.py
"""
import os
import subprocess
import sys
from typing import List

# 服务器连接配置
SERVER = "root@43.156.242.184"
SSH_KEY = "/Users/yl/vscode/inspection_automation/docs/only.pem"

# 服务器临时路径
SERVER_WORK_DIR = "/tmp/momentum_universe"
SERVER_SCRIPT = os.path.join(SERVER_WORK_DIR, "download_klines_on_server.py")
SERVER_OUTPUT_DIR = os.path.join(SERVER_WORK_DIR, "data")

# 本地路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(SCRIPT_DIR, "download_klines_on_server.py")
LOCAL_DATA_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "data")
LOCAL_KLINES_DIR = os.path.join(LOCAL_DATA_DIR, "klines")
LOCAL_UNIVERSE = os.path.join(LOCAL_DATA_DIR, "universe.json")


def ssh_cmd(remote_cmd: str, timeout: int = 1800) -> tuple:
    """执行远程命令

    Args:
        remote_cmd: 服务器命令
        timeout: 超时（秒），日线下载较慢需放宽

    Returns:
        (stdout, returncode)
    """
    cmd = [
        "ssh", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        SERVER, remote_cmd,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.returncode
    except subprocess.TimeoutExpired:
        print("  [错误] 服务器命令超时")
        return "", 1
    except Exception as e:  # noqa: BLE001
        print(f"  [错误] 命令执行失败: {e}")
        return "", 1


def scp_upload(local: str, remote: str) -> bool:
    """上传本地文件到服务器"""
    cmd = [
        "scp", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        local, f"{SERVER}:{remote}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"  [错误] 上传失败: {e}")
        return False


def scp_download(remote: str, local: str, recursive: bool = False) -> bool:
    """从服务器下载文件/目录到本地"""
    cmd = [
        "scp", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
    ]
    if recursive:
        cmd.append("-r")
    cmd += [f"{SERVER}:{remote}", local]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"  [错误] 下载失败: {e}")
        return False


def ensure_local_dirs() -> None:
    """确保本地数据目录存在"""
    os.makedirs(LOCAL_KLINES_DIR, exist_ok=True)
    os.makedirs(LOCAL_DATA_DIR, exist_ok=True)


def main() -> int:
    """编排主流程"""
    print("=" * 62)
    print("受限池日线数据拉取（本地编排 → 服务器拉取 → 拉回本地）")
    print(f"本地数据目录: {LOCAL_DATA_DIR}")
    print("=" * 62)

    ensure_local_dirs()

    # 1. 在服务器建目录并清理旧结果
    out, rc = ssh_cmd(
        f"rm -rf {SERVER_WORK_DIR} && mkdir -p {SERVER_OUTPUT_DIR} {SERVER_WORK_DIR}"
    )
    if rc != 0:
        print("  [错误] 初始化服务器目录失败")
        return 1

    # 2. 上传服务器端下载脚本
    if not scp_upload(WORKER, SERVER_SCRIPT):
        print("  [错误] 上传服务器端脚本失败")
        return 1
    print("  [成功] 已上传服务器端下载脚本")

    # 3. 在服务器执行下载
    print("  [进行] 服务器拉取日线（可能耗时数分钟）...")
    out, rc = ssh_cmd(
        f"python3 {SERVER_SCRIPT} {SERVER_OUTPUT_DIR}", timeout=2400
    )
    print(out)
    if rc != 0:
        print("  [错误] 服务器下载失败，请查看上方日志")
        return 1

    # 4. 拉回 klines 与 universe.json
    if not scp_download(
        f"{SERVER_OUTPUT_DIR}/klines", LOCAL_KLINES_DIR, recursive=True
    ):
        print("  [错误] 拉回 K 线目录失败")
        return 1
    if not scp_download(f"{SERVER_OUTPUT_DIR}/universe.json", LOCAL_UNIVERSE):
        print("  [错误] 拉回 universe.json 失败")
        return 1

    # 5. 统计本地数据
    local_files = [
        f for f in os.listdir(LOCAL_KLINES_DIR) if f.endswith("_1d.csv")
    ]
    print("=" * 62)
    print(f"  [成功] 已拉回 {len(local_files)} 个币种日线 -> {LOCAL_KLINES_DIR}")
    print(f"  [成功] 受限池元数据 -> {LOCAL_UNIVERSE}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())