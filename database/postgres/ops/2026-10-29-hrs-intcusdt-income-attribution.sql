-- 一次性归属审计：INTCUSDT HRS 平仓的第二笔 REALIZED_PNL。
-- 执行前已只读核对 Binance userTrades 与 um/income；不会修改交易或盈亏金额。
-- 仅在 15-hrs-income-attribution.sql 建表后执行。重复运行幂等。
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM trading.trade_records
        WHERE id = 4063 AND strategy = 'hrs' AND symbol = 'INTCUSDT'
          AND order_id::text = '1888631446' AND side = 'SELL'
          AND realized_pnl = 2.93020000
    ) THEN
        RAISE EXCEPTION 'HRS INTCUSDT 原始持仓证据与已审核数据不符';
    END IF;
    IF EXISTS (
        SELECT 1 FROM trading.trade_records
        WHERE symbol = 'INTCUSDT' AND strategy <> 'hrs'
          AND executed_at >= '2026-10-01' AND executed_at < '2026-11-01'
    ) THEN
        RAISE EXCEPTION 'INTCUSDT 本月存在其他策略记录，停止自动归属';
    END IF;
    IF EXISTS (
        SELECT 1 FROM trading.hrs_income_attribution
        WHERE account_id = 'default' AND tran_id = 10466172947012
          AND (strategy <> 'hrs' OR symbol <> 'INTCUSDT'
               OR income <> 0.79500000 OR event_time_ms <> 1792677785898)
    ) THEN
        RAISE EXCEPTION '已有归属记录与本次审核结果冲突';
    END IF;
END $$;

INSERT INTO trading.hrs_income_attribution
    (account_id, tran_id, strategy, symbol, income, event_time_ms, evidence, reviewed_by)
VALUES (
    'default', 10466172947012, 'hrs', 'INTCUSDT', 0.79500000, 1792677785898,
    '{"source":"Binance PM userTrades + REALIZED_PNL income, read-only checked 2026-10-29",
      "hrs_trade_record_id":4063,
      "entry":{"order_id":1888631446,"trade_id":70028739,"side":"SELL","qty":"0.61","realized_pnl":"0"},
      "close_fills":[{"order_id":1999360506,"trade_id":72885485,"side":"BUY","qty":"0.46","realized_pnl":"2.93020000"},
                     {"order_id":2000822589,"trade_id":72947012,"side":"BUY","qty":"0.15","realized_pnl":"0.79500000"}],
      "quantity_check":"0.46 + 0.15 = 0.61",
      "income_trade_id":72947012}'::jsonb,
    'codex:exchange-fill-review-2026-10-29'
)
ON CONFLICT (account_id, tran_id) DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM trading.hrs_income_attribution
        WHERE account_id = 'default' AND tran_id = 10466172947012
          AND strategy = 'hrs' AND symbol = 'INTCUSDT'
          AND income = 0.79500000 AND event_time_ms = 1792677785898
    ) THEN
        RAISE EXCEPTION 'INTCUSDT 归属记录未按审核值写入';
    END IF;
END $$;

COMMIT;
