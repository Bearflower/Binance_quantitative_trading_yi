-- 现有库的可选迁移；仅加列/建表，不回填历史账户或收入归属。
ALTER TABLE trading.trade_records
    ADD COLUMN IF NOT EXISTS account_id TEXT;

ALTER TABLE trading.trade_records
    ADD COLUMN IF NOT EXISTS pnl_source VARCHAR(32);

CREATE TABLE IF NOT EXISTS trading.reviewed_income_attribution (
    account_id      TEXT NOT NULL,
    tran_id         BIGINT NOT NULL,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    income_type     TEXT NOT NULL,
    income          NUMERIC(20,8) NOT NULL,
    asset           TEXT NOT NULL,
    event_time_utc  TIMESTAMPTZ NOT NULL,
    evidence        JSONB NOT NULL,
    reviewed_by     TEXT NOT NULL,
    reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, tran_id),
    CHECK (income_type IN ('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE')),
    CHECK (asset = 'USDT')
);

GRANT SELECT ON trading.reviewed_income_attribution TO trading_user;
