"""
手动触发月度资金分配（临时脚本，仅运维用，不入库）

用法：在 ai-tuner 容器内执行：
    python /tmp/manual_allocation_trigger.py

复用 StratTuneAI 的组件初始化（config/db/notification/binance），
仅调用 monthly_job.run_monthly_allocation()，不启动 web 服务与调度器。
"""

import asyncio
import os
import sys

# 确保项目根目录在 sys.path 中
_project_root = "/app"
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from ai_tuner.main import StratTuneAI


async def main() -> None:
    """初始化系统组件并手动执行一次月度资金分配"""
    app = StratTuneAI()
    # 仅初始化 config（加载配置），不启动完整服务
    app.config = app.load_config()

    # 初始化数据库
    from shared.database import DatabaseManager

    db_cfg = app.config.get("database", {})
    app.db_manager = DatabaseManager(
        host=db_cfg.get("host", "localhost"),
        port=int(db_cfg.get("port", 5432)),
        database=db_cfg.get("database", ""),
        user=db_cfg.get("user", ""),
        password=db_cfg.get("password", ""),
    )
    await app.db_manager.connect()
    print("[手动触发] 数据库连接已建立")

    # 初始化通知客户端
    from shared.notification import NotificationClient

    app.notification_client = NotificationClient(
        service_url=os.getenv("NOTIFICATION_SERVICE_URL", ""),
        use_direct_webhook=True,
    )
    feishu_webhook = os.getenv(
        app.config.get("approval", {}).get("feishu_webhook_env", "FEISHU_WEBHOOK_TUNER"),
        "",
    )
    if feishu_webhook and not app.notification_client.has_webhook("tuner"):
        app.notification_client.register_webhook("tuner", feishu_webhook)

    # 初始化消息发送器
    from ai_tuner.notifier.messenger import Messenger

    app.messenger = Messenger(app.notification_client)

    # 初始化配置管理模块
    from ai_tuner.deploy.config_operator import ConfigOperator
    from ai_tuner.deploy.rollback_manager import RollbackManager

    app.rollback_manager = RollbackManager(
        max_backups=app.config.get("rollback", {}).get("max_backups", 10)
    )
    app.config_operator = ConfigOperator(rollback_manager=app.rollback_manager)

    # 初始化币安客户端
    from shared.binance_api import BinanceClient

    binance_api_key = os.getenv("BINANCE_API_KEY", "")
    binance_api_secret = os.getenv("BINANCE_API_SECRET", "")
    binance_testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    binance_client = None
    if binance_api_key and binance_api_secret:
        binance_client = BinanceClient(
            api_key=binance_api_key,
            api_secret=binance_api_secret,
            testnet=binance_testnet,
            use_unified_account=True,
        )
        print("[手动触发] 币安客户端已初始化")
    else:
        print("[手动触发] 警告: BINANCE_API_KEY 未配置，将使用配置值 total_capital")

    # 初始化月度分配任务
    from ai_tuner.allocation.monthly_job import MonthlyAllocationJob

    app.monthly_job = MonthlyAllocationJob(
        config=app.config,
        db_manager=app.db_manager,
        notification_client=app.notification_client,
        messenger=app.messenger,
        config_operator=app.config_operator,
        rollback_manager=app.rollback_manager,
        binance_client=binance_client,
    )

    # 执行月度资金分配
    result = await app.monthly_job.run_monthly_allocation()
    print("[手动触发] 月度资金分配结果:", result)

    # 关闭数据库连接
    await app.db_manager.disconnect()
    print("[手动触发] 数据库连接已关闭")


if __name__ == "__main__":
    asyncio.run(main())
