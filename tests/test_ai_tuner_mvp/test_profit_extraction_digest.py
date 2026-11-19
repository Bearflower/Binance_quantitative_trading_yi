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
