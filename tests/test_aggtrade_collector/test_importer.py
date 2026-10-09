"""样本文件导入测试：SHA-256 强制校验（需求 §2.1 指纹口径）。"""
import hashlib
import json

import pytest

from services.aggtrade_collector.importer import load_sample_trades, verify_sha256


def _sample():
    return [
        {"aggTradeId": 1, "price": "100.5", "quantity": "1",
         "firstTradeId": 1, "lastTradeId": 1, "trade_time_ms": 1000,
         "is_buyer_maker": False},
        {"aggTradeId": 2, "price": "100.6", "quantity": "2",
         "firstTradeId": 2, "lastTradeId": 2, "trade_time_ms": 1500,
         "is_buyer_maker": True},
    ]


@pytest.fixture()
def sample_file(tmp_path):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(_sample()), encoding="utf-8")
    return path


def test_verify_sha256_ok(sample_file):
    actual = hashlib.sha256(sample_file.read_bytes()).hexdigest()
    assert verify_sha256(sample_file, actual) == actual


def test_verify_sha256_mismatch_raises(sample_file):
    with pytest.raises(ValueError, match="指纹不符"):
        verify_sha256(sample_file, "0" * 64)


def test_load_normalizes_and_stamps_import_time(sample_file):
    actual = hashlib.sha256(sample_file.read_bytes()).hexdigest()
    trades = load_sample_trades(sample_file, actual, "ETHUSDT", received_at_ms=999)
    assert len(trades) == 2
    assert trades[0]["symbol"] == "ETHUSDT"
    assert trades[0]["is_buyer_maker"] == 0
    assert trades[1]["is_buyer_maker"] == 1
    assert all(t["received_at_ms"] == 999 for t in trades)


@pytest.mark.parametrize("content", ["[]", "{}"])
def test_load_empty_or_non_list_rejected(sample_file, content):
    """空数组或非数组 JSON 在指纹校验通过后仍须拒绝（格式守卫）。"""
    sample_file.write_text(content, encoding="utf-8")
    actual = hashlib.sha256(sample_file.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="为空或格式非法"):
        load_sample_trades(sample_file, actual, "ETHUSDT", received_at_ms=1)
