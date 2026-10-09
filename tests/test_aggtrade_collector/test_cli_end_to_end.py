"""CLI 端到端：真实 20 分钟固定指纹样本落 tmp 库，验证 M0 退出口径与清单划分。"""
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from services.aggtrade_collector.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = PROJECT_ROOT / "ethusdt_aggtrades_20261007_0950_1010.json"
SAMPLE_SHA = "63e375fb1cb7586c03c303682cab6fde389c94165cbbb67735f05064a04ec2fc"
TZ = timezone(timedelta(hours=8))


def _point(h, m, s):
    return int(datetime(2026, 10, 7, h, m, s, tzinfo=TZ).timestamp() * 1000)


@pytest.mark.skipif(not SAMPLE.exists(), reason="固定样本文件不在仓库中")
def test_import_sample_end_to_end(tmp_path):
    db_path = tmp_path / "data/aggtrades/ethusdt_aggtrades.sqlite"
    main(["--base-dir", str(tmp_path), "init-db"])
    main(["--base-dir", str(tmp_path), "import-sample", "--sample", str(SAMPLE)])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    agg = conn.execute("SELECT COUNT(*) c FROM agg_trades").fetchone()["c"]
    smp = conn.execute(
        "SELECT COUNT(*) c, SUM(trade_count) st FROM price_samples_1s").fetchone()
    assert agg == 164071
    # 09:50:01~10:09:59 共 1199 个已关闭决策点
    assert smp["c"] == 1199
    # 审计指纹两点（需求 §2.6，AC-24/31 物化口径一致性）
    for t, expect in [(_point(9, 57, 2), "2672.23"), (_point(10, 1, 11), "2630.00")]:
        price = conn.execute(
            "SELECT price FROM price_samples_1s WHERE sample_ms=?", (t,)).fetchone()
        assert price["price"] == expect
    # AC-15 逐笔时点精确复现（需求 §2.3：10:01:14.591 @2623.52，不经采样）
    cross = conn.execute(
        "SELECT COUNT(*) c FROM agg_trades "
        "WHERE trade_time_ms = ? AND price = '2623.52'",
        (_point(10, 1, 14) + 591,)).fetchone()
    assert cross["c"] >= 1
    conn.close()

    # 幂等：二次导入零新增
    main(["--base-dir", str(tmp_path), "import-sample", "--sample", str(SAMPLE)])
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0] == 164071
    conn.close()

    # 清单：开发段标记 + 验证段口径
    manifest_file = tmp_path / "data/aggtrades/sample_manifest.json"
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    source = data["development_segment"]["sources"][0]
    assert source["sha256"] == SAMPLE_SHA
    assert source["trade_rows_new"] == 164071
    assert "已参与选参" in data["development_segment"]["label"]
    assert data["validation_segment"]["start_rule_beijing"] == \
        "2026-10-08T00:00:00+08:00"
    assert data["validation_segment"]["collected_in_this_db"] is False
    assert data["db_stats"]["agg_trades"]["rows"] == 164071
