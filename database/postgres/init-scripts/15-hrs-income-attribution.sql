-- 审核通过的 HRS income 归属例外。仅用于自动金额/时间匹配无法证明归属的事件。
-- 月度分配读取时仍逐项核对 tran_id、symbol、income、event_time_ms；
-- 本表不自动填充，任何行都应有可复核的交易所成交与持仓证据。
CREATE TABLE IF NOT EXISTS trading.hrs_income_attribution (
    account_id      TEXT NOT NULL,
    tran_id         BIGINT NOT NULL,
    strategy        TEXT NOT NULL CHECK (strategy = 'hrs'),
    symbol          TEXT NOT NULL,
    income          NUMERIC(20, 8) NOT NULL,
    event_time_ms   BIGINT NOT NULL,
    evidence        JSONB NOT NULL,
    reviewed_by     TEXT NOT NULL,
    reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, tran_id)
);

GRANT SELECT ON trading.hrs_income_attribution TO trading_user;
