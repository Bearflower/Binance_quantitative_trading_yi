import hashlib
import json

import pytest

from strategies.btc_eth_aggressive.cooldown_reset import stop_cooldown_reset


@pytest.fixture
def approved(tmp_path, monkeypatch):
    reset = dict(account_id="default", strategy="btc_eth_aggressive",
                 symbol="BNBUSDT", last_fill_time_ms=1000, cutover_ms=2000,
                 approved_at_ms=3000, scope="one_historical_stop_cooldown",
                 reason="Holder explicitly requested resetting existing BNB cooldown")
    path = tmp_path / "reset.json"
    raw = json.dumps(reset).encode()
    path.write_bytes(raw)
    monkeypatch.setenv("AGGRESSIVE_COOLDOWN_RESET_MANIFEST", str(path))
    monkeypatch.setenv("AGGRESSIVE_COOLDOWN_RESET_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv("BINANCE_ACCOUNT_MODE", "classic")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "2000")
    return path


def check(**changes):
    args = dict(account="default", strategy="btc_eth_aggressive", symbol="BNBUSDT",
                last_fill_ms=1000, now_ms=4000, cooldown_ms=10000)
    return stop_cooldown_reset(**dict(args, **changes))


def test_only_authorized_historical_stop_is_reset(approved):
    assert check()


@pytest.mark.parametrize("changes", [
    dict(account="other"), dict(strategy="btc_eth"), dict(symbol="ETHUSDT"),
    dict(last_fill_ms=3500), dict(now_ms=2999), dict(now_ms=11000),
])
def test_new_stops_and_other_scopes_keep_their_cooldown(approved, changes):
    assert not check(**changes)


def test_tampered_receipt_fails_closed(approved):
    approved.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        check()


def test_no_configuration_preserves_normal_cooldown(monkeypatch):
    monkeypatch.delenv("AGGRESSIVE_COOLDOWN_RESET_MANIFEST", raising=False)
    monkeypatch.delenv("AGGRESSIVE_COOLDOWN_RESET_SHA256", raising=False)
    assert not check()
