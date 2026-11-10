from shared.accounting.order_evidence import resolve_order_owner


def test_account_scoped_trade_record_is_exact_order_evidence():
    owner, evidence = resolve_order_owner(
        "pm-main", "SOLUSDT", "901",
        [{"account_id": "pm-main", "symbol": "SOLUSDT", "order_id": "901",
          "strategy": "btc_eth", "id": 7}], [], [],
    )
    assert owner == "btc_eth"
    assert evidence["source_kind"] == "trade_record"
    assert evidence["source_ids"] == ["7"]


def test_missing_account_and_conflicting_strategies_fail_closed():
    records = [{"account_id": None, "symbol": "SOLUSDT", "order_id": "901",
                "strategy": "btc_eth", "id": 7}]
    assert resolve_order_owner("pm-main", "SOLUSDT", "901", records, [], []) == (
        None, "order_owner_missing")
    records.extend([
        {"account_id": "pm-main", "symbol": "SOLUSDT", "order_id": "901",
         "strategy": "btc_eth", "id": 8},
        {"account_id": "pm-main", "symbol": "SOLUSDT", "order_id": "901",
         "strategy": "btc_eth_aggressive", "id": 9},
    ])
    assert resolve_order_owner("pm-main", "SOLUSDT", "901", records, [], []) == (
        None, "order_owner_ambiguous")


def test_pm_algo_requires_unique_exact_link_and_account_scoped_condition():
    links = [{"account_id": "pm-main", "symbol": "SOLUSDT", "actual_order_id": "901",
              "algo_id": "123", "payload_sha256": "abc"}]
    conditions = [{"account_id": "pm-main", "symbol": "SOLUSDT", "algo_id": "123",
                   "strategy_name": "btc_eth_aggressive"}]
    owner, evidence = resolve_order_owner("pm-main", "SOLUSDT", "901", [], links, conditions)
    assert owner == "btc_eth_aggressive"
    assert evidence["source_kind"] == "pm_algo_link"
    assert evidence["algo_id"] == "123"
    assert evidence["payload_sha256"] == "abc"
    conditions[0]["account_id"] = None
    assert resolve_order_owner("pm-main", "SOLUSDT", "901", [], links, conditions) == (
        None, "condition_owner_missing")


def test_conflicting_direct_and_algo_owner_is_rejected():
    records = [{"account_id": "pm-main", "symbol": "SOLUSDT", "order_id": "901",
                "strategy": "btc_eth", "id": 7}]
    links = [{"account_id": "pm-main", "symbol": "SOLUSDT", "actual_order_id": "901",
              "algo_id": "123", "payload_sha256": "abc"}]
    conditions = [{"account_id": "pm-main", "symbol": "SOLUSDT", "algo_id": "123",
                   "strategy_name": "btc_eth_aggressive"}]
    assert resolve_order_owner("pm-main", "SOLUSDT", "901", records, links, conditions) == (
        None, "order_owner_conflict")
