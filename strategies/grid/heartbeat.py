"""小时出口进程的心跳线程：把不可变会话状态转嫁给实时进程（§2.7/§3.5）。

- 宿主在小时出口进程内（daemon 线程），不落在 realtime 主进程；不改小时调度。
- 心跳线程持有独立 sqlite 连接（不跨线程共享出口连接），只转发状态、不在此
  计算指标或判定发送成功。
- 存储故障计入 failure_count 并打日志，不向调用线程抛异常。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from .realtime.reference_store import (SessionState, connect,
                                       write_heartbeat)

logger = logging.getLogger(__name__)


class HeartbeatThread:
    """周期发布 session 心跳的 daemon 线程。"""

    def __init__(self, state_getter: Callable[[], SessionState],
                 db_path: str, interval_seconds: float,
                 busy_timeout_ms: int,
                 now_ms: Callable[[], int]):
        """
        Args:
            state_getter: 返回出口当前不可变状态快照（出口主线程更新）
            db_path: grid_realtime.sqlite3 绝对路径（与实时进程同库）
            interval_seconds: 发布周期（reference_sync.heartbeat_seconds）
            busy_timeout_ms: 锁等待上限
            now_ms: 当前毫秒时间（注入，便于测试）
        """
        self._state_getter = state_getter
        self._db_path = db_path
        self._interval = interval_seconds
        self._busy_timeout = busy_timeout_ms
        self._now_ms = now_ms
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.failure_count = 0

    def start(self) -> None:
        """启动 daemon 线程（重复调用安全）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="grid-heartbeat", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """停止并等待线程退出。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        conn = connect(self._db_path, self._busy_timeout)
        try:
            while not self._stop.wait(self._interval):
                self._publish_once(conn)
        finally:
            conn.close()

    def _publish_once(self, conn) -> None:
        try:
            ok = write_heartbeat(conn, self._state_getter(), self._now_ms())
            if not ok:
                self.failure_count += 1
                logger.warning("心跳未写入：会话行不存在或已关闭")
        except Exception as exc:  # 心跳故障不杀出口进程
            self.failure_count += 1
            logger.warning("心跳发布失败：%s", exc)
