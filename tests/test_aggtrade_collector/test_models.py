"""逐笔归一化与字段校验测试（真实性检查：字段映射/拼写/边界值）。"""
import pytest

from services.aggtrade_collector.models import (
    FILE_FIELD_MAP, REST_FIELD_MAP, insert_params, normalize_trade,
)


def rest_row(**overrides):
    row = {"a": 101, "p": "2686.72", "q": "0.009", "f": 1001, "l": 1001,
           "T": 1791337800290, "m": True}
    row.update(overrides)
    return row


def test_rest_field_mapping():
    """REST a/p/q/f/l/T/m 到库内字段映射正确，布尔 maker 落 0/1。"""
    trade = normalize_trade(rest_row(), REST_FIELD_MAP, "ETHUSDT", 123)
    assert trade == {
        "agg_trade_id": 101, "symbol": "ETHUSDT", "price": "2686.72",
        "quantity": "0.009", "first_trade_id": 1001, "last_trade_id": 1001,
        "trade_time_ms": 1791337800290, "is_buyer_maker": 1,
        "received_at_ms": 123,
    }


def test_file_field_mapping():
    """历史样本文件字段映射，false 落 0，received_at 取导入时刻。"""
    raw = {"aggTradeId": 1, "price": "10.5", "quantity": "2",
           "firstTradeId": 9, "lastTradeId": 9,
           "trade_time_ms": 5000, "is_buyer_maker": False}
    trade = normalize_trade(raw, FILE_FIELD_MAP, "ETHUSDT", 7)
    assert trade["agg_trade_id"] == 1
    assert trade["is_buyer_maker"] == 0
    assert trade["received_at_ms"] == 7


def test_insert_params_column_order():
    """入库参数列序与 SQL 固定一致。"""
    trade = normalize_trade(rest_row(), REST_FIELD_MAP, "ETHUSDT", 1)
    assert insert_params(trade) == (101, "ETHUSDT", "2686.72", "0.009", 1001,
                                   1001, 1791337800290, 1, 1)


@pytest.mark.parametrize("field,bad", [
    ("p", ""), ("p", "0"), ("p", "-1"), ("p", "abc"),
    ("q", "0"), ("T", 0), ("a", 0), ("a", 1.5),
])
def test_invalid_values_rejected(field, bad):
    """空/非正/非数字价格数量、非正时间、非整数 ID 一律拒绝。"""
    with pytest.raises(ValueError):
        normalize_trade(rest_row(**{field: bad}), REST_FIELD_MAP, "ETHUSDT", 1)


def test_missing_field_rejected():
    row = rest_row()
    del row["a"]
    with pytest.raises(ValueError):
        normalize_trade(row, REST_FIELD_MAP, "ETHUSDT", 1)


def test_first_id_greater_than_last_rejected():
    with pytest.raises(ValueError):
        normalize_trade(rest_row(f=2000, l=1000), REST_FIELD_MAP, "ETHUSDT", 1)


def test_bool_not_accepted_as_int():
    with pytest.raises(ValueError):
        normalize_trade(rest_row(a=True), REST_FIELD_MAP, "ETHUSDT", 1)


def test_numeric_string_ids_accepted():
    """币安部分字段可能以字符串到达：纯整数字符串接受并转 int。"""
    trade = normalize_trade(
        rest_row(a="101", f="1001", l="1001", T="1791337800290"),
        REST_FIELD_MAP, "ETHUSDT", 1)
    assert trade["agg_trade_id"] == 101
    assert trade["trade_time_ms"] == 1791337800290
    assert isinstance(trade["agg_trade_id"], int)


@pytest.mark.parametrize("flag", [0, 1])
def test_integer_maker_flag_accepted(flag):
    """is_buyer_maker 以 0/1 整数到达时同样接受。"""
    assert normalize_trade(rest_row(m=flag), REST_FIELD_MAP,
                          "ETHUSDT", 1)["is_buyer_maker"] == flag


def test_invalid_maker_flag_rejected():
    """非 bool 且非 0/1 的 maker 标志拒绝。"""
    with pytest.raises(ValueError):
        normalize_trade(rest_row(m=2), REST_FIELD_MAP, "ETHUSDT", 1)
