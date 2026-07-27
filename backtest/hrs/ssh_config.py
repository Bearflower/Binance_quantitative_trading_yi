"""
HRS 回测系统 SSH 连接配置

统一管理所有模块的 SSH 连接参数，避免分散硬编码。
遵循 DRY 原则，修改 SSH 配置时只需修改本文件。
"""

# SSH 连接配置
SERVER_IP = "43.156.242.184"
SERVER_USER = "root"
SSH_KEY = "/Users/yl/vscode/inspection_automation/docs/only.pem"