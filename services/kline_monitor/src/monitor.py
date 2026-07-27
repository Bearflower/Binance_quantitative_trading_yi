"""
K 线数据服务健康监控

独立运行的健康监控服务，定时检查 K 线服务的健康状态，
在检测到故障或恢复时通过飞书发送告警通知。

功能：
- 每 N 秒检查 K 线服务 /api/v1/health 端点
- 连续失败超过阈值时发送告警
- 服务恢复时发送恢复通知
- 启动时发送服务状态通知
- 状态持久化（跨重启保持状态）
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import structlog

# ── 日志配置 ──────────────────────────────────────────────────
logger = structlog.get_logger()

# ── 配置（从环境变量读取） ─────────────────────────────────────
KLINE_SERVICE_URL = os.getenv("KLINE_SERVICE_URL", "http://kline-service:8000")
HEALTH_ENDPOINT = f"{KLINE_SERVICE_URL.rstrip('/')}/api/v1/health"
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5分钟
CONSECUTIVE_FAILURES_THRESHOLD = int(os.getenv("CONSECUTIVE_FAILURES_THRESHOLD", "3"))  # 连续3次失败告警

# 飞书 webhook URL（优先使用专用 webhook，其次使用通用 webhook）
FEISHU_WEBHOOK_URL = (
    os.getenv("FEISHU_WEBHOOK_KLINE_MONITOR")
    or os.getenv("FEISHU_WEBHOOK")
    or ""
)

# 状态文件路径（持久化健康状态，防止容器重启后状态丢失）
STATE_FILE_PATH = os.getenv("STATE_FILE_PATH", "/app/data/monitor_state.json")

# 服务名称（用于通知）
SERVICE_NAME = os.getenv("MONITOR_SERVICE_NAME", "K 线数据服务")

# 告警级别配置
NOTIFY_ON_STARTUP = os.getenv("NOTIFY_ON_STARTUP", "true").lower() == "true"


class HealthMonitor:
    """K 线服务健康监控器"""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._consecutive_failures = 0
        self._last_state: Optional[bool] = None  # True=健康, False=不健康
        self._load_state()

    def _load_state(self):
        """从文件加载持久化状态"""
        state_file = Path(STATE_FILE_PATH)
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                self._consecutive_failures = data.get("consecutive_failures", 0)
                last_state_raw = data.get("last_healthy")
                if last_state_raw is not None:
                    self._last_state = last_state_raw
                logger.info(
                    "已加载持久化状态",
                    consecutive_failures=self._consecutive_failures,
                    last_healthy=self._last_state,
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("状态文件解析失败，使用默认状态", error=str(e))

    def _save_state(self):
        """保存状态到文件"""
        state_file = Path(STATE_FILE_PATH)
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "consecutive_failures": self._consecutive_failures,
                "last_healthy": self._last_state,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            logger.warning("状态文件保存失败", error=str(e))

    async def _init_session(self):
        """初始化 HTTP 会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        """关闭 HTTP 会话"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def check_health(self) -> bool:
        """检查 K 线服务健康状态

        Returns:
            True 表示健康，False 表示不健康
        """
        await self._init_session()
        try:
            async with self.session.get(HEALTH_ENDPOINT, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    status = data.get("status", "")
                    if status == "healthy":
                        return True
                    else:
                        logger.warning("健康检查返回非 healthy 状态", status=status, data=data)
                        return False
                else:
                    logger.warning("健康检查 HTTP 状态异常", status=resp.status)
                    return False
        except asyncio.TimeoutError:
            logger.warning("健康检查超时", endpoint=HEALTH_ENDPOINT)
            return False
        except aiohttp.ClientError as e:
            logger.warning("健康检查连接失败", endpoint=HEALTH_ENDPOINT, error=str(e))
            return False
        except Exception as e:
            logger.error("健康检查异常", error=str(e), exc_info=True)
            return False

    async def send_feishu_notification(self, message: str, level: str = "warning"):
        """发送飞书通知

        Args:
            message: 通知内容
            level: 级别 (info/warning/error)
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("未配置飞书 webhook URL，跳过通知")
            return False

        await self._init_session()

        level_emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}
        emoji = level_emoji.get(level, "ℹ️")
        formatted_message = f"{emoji} {message}"

        payload = {
            "msg_type": "text",
            "content": {"text": formatted_message},
        }

        for attempt in range(3):
            try:
                async with self.session.post(FEISHU_WEBHOOK_URL, json=payload) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("code") == 0:
                        logger.info("飞书通知发送成功")
                        return True
                    else:
                        logger.warning(
                            "飞书通知发送失败",
                            status=resp.status,
                            code=data.get("code"),
                            attempt=attempt + 1,
                        )
                        if attempt < 2:
                            await asyncio.sleep(5)
            except aiohttp.ClientError as e:
                logger.error("飞书webhook连接失败", error=str(e), attempt=attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(5)

        logger.error("飞书通知发送失败，已耗尽重试次数")
        return False

    async def send_startup_notification(self):
        """发送服务启动通知"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        message = (
            f"【{SERVICE_NAME} 健康监控已启动】\n"
            f"检查间隔: {CHECK_INTERVAL_SECONDS}秒\n"
            f"告警阈值: 连续{CONSECUTIVE_FAILURES_THRESHOLD}次失败\n"
            f"检查端点: {HEALTH_ENDPOINT}\n"
            f"启动时间: {now} (UTC)"
        )
        await self.send_feishu_notification(message, level="info")

    async def send_failure_alert(self, consecutive_failures: int):
        """发送故障告警

        Args:
            consecutive_failures: 连续失败次数
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if consecutive_failures == CONSECUTIVE_FAILURES_THRESHOLD:
            # 首次达到阈值，发送告警
            message = (
                f"【{SERVICE_NAME} 故障告警】\n"
                f"状态: 服务不可用\n"
                f"连续失败: {consecutive_failures}次\n"
                f"检查端点: {HEALTH_ENDPOINT}\n"
                f"建议: 请检查 K 线服务容器状态\n"
                f"时间: {now} (UTC)"
            )
            await self.send_feishu_notification(message, level="error")
        elif consecutive_failures > CONSECUTIVE_FAILURES_THRESHOLD:
            # 持续故障，每10次发送一次提醒（避免刷屏）
            if consecutive_failures % 10 == 0:
                duration_minutes = (
                    consecutive_failures * CHECK_INTERVAL_SECONDS // 60
                )
                message = (
                    f"【{SERVICE_NAME} 持续故障】\n"
                    f"状态: 服务持续不可用\n"
                    f"连续失败: {consecutive_failures}次\n"
                    f"已持续: 约{duration_minutes}分钟\n"
                    f"检查端点: {HEALTH_ENDPOINT}\n"
                    f"时间: {now} (UTC)"
                )
                await self.send_feishu_notification(message, level="error")

    async def send_recovery_notification(self, duration_minutes: int):
        """发送恢复通知

        Args:
            duration_minutes: 故障持续时间（分钟）
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        message = (
            f"【{SERVICE_NAME} 已恢复】\n"
            f"状态: 服务恢复正常\n"
            f"故障时长: 约{duration_minutes}分钟\n"
            f"检查端点: {HEALTH_ENDPOINT}\n"
            f"恢复时间: {now} (UTC)"
        )
        await self.send_feishu_notification(message, level="info")

    async def run(self):
        """主监控循环"""
        logger.info(
            "K 线服务健康监控启动",
            endpoint=HEALTH_ENDPOINT,
            check_interval=CHECK_INTERVAL_SECONDS,
            failure_threshold=CONSECUTIVE_FAILURES_THRESHOLD,
            webhook_configured=bool(FEISHU_WEBHOOK_URL),
        )

        # 启动通知
        if NOTIFY_ON_STARTUP:
            await self.send_startup_notification()

        while True:
            try:
                is_healthy = await self.check_health()

                if is_healthy:
                    if self._last_state is False:
                        # 从故障恢复
                        duration_minutes = (
                            self._consecutive_failures * CHECK_INTERVAL_SECONDS // 60
                        )
                        logger.info(
                            "K 线服务已恢复",
                            downtime_minutes=duration_minutes,
                        )
                        await self.send_recovery_notification(duration_minutes)

                    self._consecutive_failures = 0
                    self._last_state = True
                else:
                    self._consecutive_failures += 1
                    self._last_state = False

                    logger.warning(
                        "K 线服务健康检查失败",
                        consecutive_failures=self._consecutive_failures,
                        threshold=CONSECUTIVE_FAILURES_THRESHOLD,
                    )

                    # 达到阈值时发送告警
                    if self._consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD:
                        await self.send_failure_alert(self._consecutive_failures)

                # 持久化状态
                self._save_state()

                # 等待下一次检查
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                logger.info("监控任务被取消")
                break
            except Exception as e:
                logger.error("监控循环异常", error=str(e), exc_info=True)
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main():
    """主入口"""
    # 验证必要配置
    if not KLINE_SERVICE_URL:
        logger.error("KLINE_SERVICE_URL 未配置")
        sys.exit(1)

    monitor = HealthMonitor()
    try:
        await monitor.run()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    finally:
        await monitor.close()


if __name__ == "__main__":
    asyncio.run(main())