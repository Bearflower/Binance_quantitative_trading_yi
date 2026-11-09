from pathlib import Path

from dashboard.backend.services.exchange_ledger import ExchangeLedgerDashboardMixin


class Service(ExchangeLedgerDashboardMixin):
    _STRATEGY_NAME_MAP = {"hrs": "HRS"}
    _STRATEGY_EMOJI_MAP = {}


def test_partial_projection_keeps_final_null_and_observed_value():
    report = {"strategy_attribution_complete": False,
              "by_strategy": {"hrs": {"net_usdt": "-2.4"}},
              "report_id": "r1", "unattributed_usdt_total": "-0.8"}
    row = Service()._strategy_projection(report, "hrs")
    assert row["total_pnl"] is None
    assert row["observed_pnl"] == "-2.4"
    assert row["win_rate"] is None


def test_frontend_labels_partial_subtotal():
    source = (Path(__file__).resolve().parents[1] / "dashboard/frontend/js/main.js").read_text()
    assert "已归属部分" in source
    assert "strategy.total_pnl == null ? '待归属'" not in source
