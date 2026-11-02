-- 只读经济账使用的人工复核逐事件归属表；不自动填充。
-- 管理员写入前必须核对交易所收入事件和持仓/订单证据。
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
    PRIMARY KEY (account_id, income_type, tran_id),
    CHECK (income_type IN ('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE')),
    CHECK (asset = 'USDT')
);

GRANT SELECT ON trading.reviewed_income_attribution TO trading_user;
