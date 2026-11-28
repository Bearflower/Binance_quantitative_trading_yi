import asyncio
import json

from ai_tuner.allocation.profit_extraction_job import ProfitExtractionJob

CONFIG = {"profit_extraction": {
    "enabled": True,
    "initial_capital_usdt": 500,
    "extract_ratio": 0.5,
    "min_extract_usdt": 10,
}}


class FakeDB:
    def __init__(self, ath="666.77"):
        self.ath = ath

    async def execute_ddl(self, *_args, **_kwargs):
        return None

    async def execute(self, *_args, **_kwargs):
        return None

    async def fetch_one(self, *_args, **_kwargs):
        return {"value": self.ath}


class FakeBinance:
    def __init__(self, balance=None, error=None):
        self.balance = balance
        self.error = error

    async def get_account_info(self):
        if self.error:
            raise RuntimeError(self.error)
        return {"totalWalletBalance": str(self.balance)}


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def send(self, message, level="info", project=None):
        self.messages.append(message)
        return True


def _job(tmp_path, monkeypatch, *, balance=None, error=None, ath="666.77"):
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path))
    return ProfitExtractionJob(
        db_manager=FakeDB(ath),
        notification_client=FakeNotifier(),
        binance=FakeBinance(balance, error),
        config=CONFIG,
    )


def _published(tmp_path):
    return json.loads((tmp_path / "profit.json").read_text(encoding="utf-8"))


def test_not_new_high_keeps_not_triggered_status(tmp_path, monkeypatch):
    job = _job(tmp_path, monkeypatch, balance="600", ath="666.77")

    result = asyncio.run(job.run_daily_check())

    assert result == {"action": "skip", "reason": "not_new_high"}
    published = _published(tmp_path)
    assert published["status"] == "skip"
    assert published["summary"] == "未触发"
    assert "本次未触发" in published["text"]
    assert "600.00" in published["text"]
    assert "666.77" in published["text"]
    assert job.notification_client.messages == []


def test_new_high_publishes_extraction_advice(tmp_path, monkeypatch):
    job = _job(tmp_path, monkeypatch, balance="700", ath="666.77")

    result = asyncio.run(job.run_daily_check())

    assert result["action"] == "notified"
    assert result["extract_amount"] == 100.0
    published = _published(tmp_path)
    assert published["status"] == "warn"
    assert "已触发" in published["summary"]
    assert "💰 利润提取提醒" in published["text"]
    assert job.notification_client.messages == []


def test_fallback_sends_directly_when_digest_module_missing(tmp_path, monkeypatch):
    from ai_tuner.allocation import profit_extraction_job

    job = _job(tmp_path, monkeypatch, balance="600", ath="666.77")
    monkeypatch.setattr(profit_extraction_job, "digest_outbox", None)

    asyncio.run(job.run_daily_check())

    assert job.notification_client.messages
    assert "本次未触发" in job.notification_client.messages[0]


def test_check_error_publishes_error_status(tmp_path, monkeypatch):
    job = _job(tmp_path, monkeypatch, error="network down")

    result = asyncio.run(job.run_daily_check())

    assert result["action"] == "error"
    published = _published(tmp_path)
    assert published["status"] == "error"
    assert published["summary"] == "检查异常"
    assert "network down" in published["text"]


def test_cron_runs_before_digest():
    job = ProfitExtractionJob(FakeDB(), FakeNotifier(), FakeBinance(), CONFIG)
    trigger = job.get_cron_trigger()
    assert "hour='7'" in str(trigger)
    assert "minute='34'" in str(trigger)


def test_classic_opening_wallet_change_does_not_trigger_profit_or_overwrite_pm_peak(tmp_path, monkeypatch):
    from decimal import Decimal
    from unittest.mock import AsyncMock
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    job = _job(tmp_path, monkeypatch)
    job.binance.use_unified_account = False
    job.binance.get_account_info = AsyncMock(return_value={"totalMarginBalance": "2000", "totalWalletBalance": "3000"})
    async def fetch(_sql, key):
        return {"value": "1000"} if key == "ath_balance" else None
    job.db_manager.fetch_one = fetch
    job.db_manager.execute = AsyncMock()
    monkeypatch.setattr("shared.accounting.equity_bridge.read_anchors",
                        AsyncMock(return_value=(Decimal("1000"), Decimal("2000"))))
    monkeypatch.setattr("shared.accounting.equity_bridge.signed_spot_uid_sha256", AsyncMock(return_value="signed"))
    assert asyncio.run(job.run_daily_check()) == {"action": "skip", "reason": "not_new_high"}
    job.db_manager.execute.assert_not_awaited()
    job.binance.get_account_info.return_value["totalMarginBalance"] = "2100"
    result = asyncio.run(job.run_daily_check())
    assert result["profit"] == 1100 and result["extract_amount"] == 550
    assert job.db_manager.execute.await_args.args[1].startswith("ath_balance:classic:")


def test_classic_anchor_diagnostics_preserve_ath(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    monkeypatch.setattr("shared.accounting.equity_bridge.signed_spot_uid_sha256", AsyncMock(return_value="signed"))
    job = _job(tmp_path, monkeypatch)
    job.binance.use_unified_account = False
    job.binance.get_account_info = AsyncMock(return_value={"totalMarginBalance": "2100"})
    job.db_manager.execute = AsyncMock()
    job.db_manager.fetch_one = AsyncMock()
    row = {"observation": "classic_open", "total_equity": "2000", "exchange_uid_sha256": "signed"}
    for rows, expected in [([], ["pm_final", "classic_open"]), ([row], ["pm_final"])]:
        job.db_manager.fetch_all = AsyncMock(return_value=rows)
        result = asyncio.run(job.run_daily_check())
        assert result == {"action": "blocked", "reason": "classic_equity_anchor_missing", "missing": expected}
        published = _published(tmp_path)
        assert published["status"] == "warn"
        assert published["summary"] == "待补切换权益凭据"
        assert "PM 期末" in published["text"]
        assert ("经典期初" in published["text"]) == ("classic_open" in expected)
    for invalid in [dict(row, total_equity="0"), dict(row, exchange_uid_sha256="other")]:
        job.db_manager.fetch_all = AsyncMock(return_value=[invalid])
        assert asyncio.run(job.run_daily_check())["action"] == "error"
        assert _published(tmp_path)["summary"] == "检查异常"
    job.db_manager.execute.assert_not_awaited()
    job.db_manager.fetch_one.assert_not_awaited()
    assert job.notification_client.messages == []


def test_classic_anchor_identity_mismatch_remains_error(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    job = _job(tmp_path, monkeypatch)
    job.binance.use_unified_account = False
    job.binance.get_account_info = AsyncMock(return_value={"totalMarginBalance": "2100"})
    monkeypatch.setattr("shared.accounting.equity_bridge.signed_spot_uid_sha256",
                        AsyncMock(return_value="signed"))
    monkeypatch.setattr("shared.accounting.equity_bridge.read_anchors",
                        AsyncMock(side_effect=ValueError("切换权益锚点与 Binance UID 不符")))

    result = asyncio.run(job.run_daily_check())

    assert result["action"] == "error"
    assert _published(tmp_path)["status"] == "error"
