"""HRS strategy schema bootstrap 回归防线。

覆盖修复 #1：把 _ensure_db_schema 内的裸 execute_ddl 调用封装成
_ensure_schema_exists + _execute_ddl_safe，使单条 DDL 失败不再中断
后续建表流程。
"""
from typing import List, Tuple

import pytest

from strategies.hrs.strategy import HRSStrategy


# ---------------------------------------------------------------------------
# 轻量 Fake DB：记录 execute_ddl / fetch_one 的调用，可注入异常
# ---------------------------------------------------------------------------

class _FakeDB:
    """测试用 Fake DatabaseManager。

    - execute_ddl：记录每次 sql，可按索引抛异常
    - fetch_one：可预先注入返回值，也可按索引抛异常
    - execute_transaction：_ensure_db_schema 不用，这里仅占位
    """

    def __init__(
        self,
        *,
        fetch_one_returns=None,
        fetch_one_raises_at=None,
        execute_ddl_raises_at=None,
    ):
        self.fetch_one_returns = list(fetch_one_returns or [])
        self.fetch_one_raises_at = set(fetch_one_raises_at or [])
        self.execute_ddl_raises_at = set(execute_ddl_raises_at or [])
        self.fetch_one_calls: List[Tuple[str, tuple]] = []
        self.execute_ddl_calls: List[str] = []

    async def fetch_one(self, query: str, *args):
        idx = len(self.fetch_one_calls)
        self.fetch_one_calls.append((query, args))
        if idx in self.fetch_one_raises_at:
            raise RuntimeError("模拟 fetch_one 失败（例如连接断开）")
        if self.fetch_one_returns:
            return self.fetch_one_returns.pop(0)
        return None

    async def execute_ddl(self, query: str, *args):
        idx = len(self.execute_ddl_calls)
        self.execute_ddl_calls.append(query)
        if idx in self.execute_ddl_raises_at:
            raise RuntimeError("模拟 execute_ddl 失败（例如 permission denied）")
        return "OK"

    async def execute_transaction(self, _queries):  # pragma: no cover - 本次改动未用到
        return True


def _make_strategy(db: _FakeDB) -> HRSStrategy:
    """只把 db 属性挂到策略实例上；其余依赖不初始化。

    HRSStrategy.__init__ 需要大量运行时依赖（Binance API 等），
    这里用 object.__new__ 跳过，只注入 db。
    """
    strategy = object.__new__(HRSStrategy)
    strategy.db = db
    return strategy


# ---------------------------------------------------------------------------
# 直接针对 _ensure_schema_exists / _execute_ddl_safe 的单元测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_schema_exists_when_already_present_does_not_call_execute_ddl():
    db = _FakeDB(fetch_one_returns=[{"exists_flag": 1}])
    strategy = _make_strategy(db)

    await strategy._ensure_schema_exists()

    assert db.execute_ddl_calls == []
    assert len(db.fetch_one_calls) == 1
    assert "information_schema.schemata" in db.fetch_one_calls[0][0]
    assert db.fetch_one_calls[0][1] == ("hrs",)


@pytest.mark.asyncio
async def test_ensure_schema_exists_when_missing_calls_create_once():
    db = _FakeDB(fetch_one_returns=[None])
    strategy = _make_strategy(db)

    await strategy._ensure_schema_exists()

    assert len(db.fetch_one_calls) == 1
    assert len(db.execute_ddl_calls) == 1
    assert "CREATE SCHEMA IF NOT EXISTS hrs" in db.execute_ddl_calls[0]


@pytest.mark.asyncio
async def test_ensure_schema_exists_fetch_one_falls_back_to_create_on_exception():
    """fetch_one 抛异常时也应该继续尝试 CREATE（覆盖 fallback 分支）。"""
    db = _FakeDB(fetch_one_raises_at={0})
    strategy = _make_strategy(db)

    # fetch_one 抛异常时 _ensure_schema_exists 不抛；继续尝试 CREATE
    await strategy._ensure_schema_exists()

    assert len(db.fetch_one_calls) == 1
    assert len(db.execute_ddl_calls) == 1
    assert "CREATE SCHEMA IF NOT EXISTS hrs" in db.execute_ddl_calls[0]


@pytest.mark.asyncio
async def test_execute_ddl_safe_returns_false_on_failure_and_logs_error(capsys):
    """_execute_ddl_safe 失败时返回 False 并记录 error 日志。"""
    db = _FakeDB(execute_ddl_raises_at={0})
    strategy = _make_strategy(db)

    # strategy.py 用的是 structlog.get_logger()，它走 stdout，caplog 捕获不到；
    # capsys 能截获测试运行期间的所有 stdout/stderr。
    result = await strategy._execute_ddl_safe("CREATE TABLE foo")

    captured = capsys.readouterr()
    assert result is False, "执行失败应返回 False"
    assert "幂等DDL执行失败" in captured.out + captured.err, "失败时应记录 error 日志"


@pytest.mark.asyncio
async def test_execute_ddl_safe_returns_true_on_success(capsys):
    """_execute_ddl_safe 成功时返回 True，不记录 error 日志。"""
    db = _FakeDB()
    strategy = _make_strategy(db)

    result = await strategy._execute_ddl_safe("CREATE TABLE foo")

    captured = capsys.readouterr()
    assert result is True, "执行成功应返回 True"
    assert "幂等DDL执行失败" not in captured.out + captured.err


# ---------------------------------------------------------------------------
# _ensure_db_schema 整体集成测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_db_schema_continues_after_one_ddl_failure():
    """中间某条 DDL 失败，后续 DDL 仍应被执行；整体不抛异常。

    这里让 execute_ddl 在第 1 次（hrs_meta 建表）抛异常。
    预期：
      - 所有 DDL 调用都被记录（至少 hrs_meta + hrs_positions + ... 都出现）
      - _ensure_db_schema 不抛（由外层 try/except 或内层 safe 兜底）
    """
    db = _FakeDB(
        # _ensure_schema_exists 会先 fetch_one，返回非空
        fetch_one_returns=[{"exists_flag": 1}],
        # 第一次 execute_ddl（hrs_meta）失败
        execute_ddl_raises_at={0},
    )
    strategy = _make_strategy(db)

    # 不抛异常
    await strategy._ensure_db_schema()

    called = db.execute_ddl_calls
    assert any("hrs_meta" in sql for sql in called), "hrs_meta 应被尝试执行"
    # 至少 hrs_positions 应继续尝试
    assert any("hrs_positions" in sql for sql in called), "hrs_positions 应继续被执行"
    # 后续诸如 hrs_signals / hrs_active_symbols 也应被尝试
    assert any("hrs_signals" in sql for sql in called), "hrs_signals 应继续被执行"


@pytest.mark.asyncio
async def test_ensure_db_schema_when_schema_missing_and_no_create_permission():
    """schema 不存在 + execute_ddl CREATE SCHEMA 无权限 → 后续幂等 DDL 继续尝试。"""
    db = _FakeDB(
        fetch_one_returns=[None],  # schema 缺失
        # 第一次 execute_ddl 是 CREATE SCHEMA（由 _ensure_schema_exists 内部发起）
        # 再让后续 hrs_meta 的 execute_ddl 也失败一次以模拟级联
        execute_ddl_raises_at={0, 1},
    )
    strategy = _make_strategy(db)

    await strategy._ensure_db_schema()  # 不抛

    called = db.execute_ddl_calls
    # CREATE SCHEMA 应被尝试过一次（即使抛了异常）
    assert called and "CREATE SCHEMA" in called[0]
    # 后续 DDL 仍被继续
    assert len(called) >= 3


@pytest.mark.asyncio
async def test_ensure_db_schema_when_fetch_one_raises_still_tries_create():
    """fetch_one 抛异常（信息不可知）→ 按 schema 缺失处理 → 仍尝试 CREATE。"""
    db = _FakeDB(fetch_one_raises_at={0})
    strategy = _make_strategy(db)

    await strategy._ensure_db_schema()  # 不抛

    assert len(db.execute_ddl_calls) >= 1
    assert "CREATE SCHEMA" in db.execute_ddl_calls[0]


@pytest.mark.asyncio
async def test_ensure_db_schema_all_successful_logs_info_and_zero_failures(capsys):
    """全部 DDL 成功 → 末尾应记 info "数据库Schema初始化完成"，且 _schema_ddl_failures 为 0。"""
    db = _FakeDB(fetch_one_returns=[{"exists_flag": 1}])
    strategy = _make_strategy(db)

    await strategy._ensure_db_schema()

    captured = capsys.readouterr()
    assert strategy._schema_ddl_failures == 0
    assert "数据库Schema初始化完成" in captured.out + captured.err
    assert "数据库Schema初始化存在未完成项" not in captured.out + captured.err


@pytest.mark.asyncio
async def test_ensure_db_schema_records_failure_count_on_error(capsys):
    """有 DDL 失败 → _schema_ddl_failures >= 1，且末尾记 error 分流日志。"""
    db = _FakeDB(
        fetch_one_returns=[{"exists_flag": 1}],
        execute_ddl_raises_at={0},  # 让 hrs_meta 建表失败
    )
    strategy = _make_strategy(db)

    await strategy._ensure_db_schema()

    captured = capsys.readouterr()
    assert strategy._schema_ddl_failures >= 1
    assert "数据库Schema初始化存在未完成项" in captured.out + captured.err
