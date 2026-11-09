-- Existing database migration for evidence-backed, insert-only income ownership.
CREATE SCHEMA IF NOT EXISTS trading;
ALTER TABLE IF EXISTS btc_eth.condition_orders ADD COLUMN IF NOT EXISTS account_id TEXT;

CREATE TABLE IF NOT EXISTS trading.order_strategy_evidence (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    order_id BIGINT NOT NULL,
    strategy TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT,
    evidence JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol, order_id),
    CHECK (source_kind IN ('trade_record', 'pm_algo_link')),
    CHECK (strategy <> '')
);

CREATE TABLE IF NOT EXISTS trading.position_flat_baseline (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    flat_at TIMESTAMPTZ NOT NULL,
    exchange_qty NUMERIC(20,8) NOT NULL DEFAULT 0,
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol, flat_at),
    CHECK (exchange_qty = 0)
);

CREATE TABLE IF NOT EXISTS trading.derived_income_attribution (
    account_id TEXT NOT NULL,
    income_type TEXT NOT NULL,
    tran_id BIGINT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    income NUMERIC(20,8) NOT NULL,
    asset TEXT NOT NULL,
    event_time_utc TIMESTAMPTZ NOT NULL,
    method TEXT NOT NULL,
    evidence JSONB NOT NULL,
    algorithm_version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, income_type, tran_id),
    CHECK (income_type IN ('REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE')),
    CHECK (asset = 'USDT'),
    CHECK (method IN ('exact_order', 'verified_position_interval')),
    CHECK (algorithm_version > 0)
);

CREATE INDEX IF NOT EXISTS idx_derived_income_window
    ON trading.derived_income_attribution(account_id, event_time_utc);

GRANT SELECT, INSERT ON trading.order_strategy_evidence TO trading_user;
GRANT SELECT, INSERT ON trading.position_flat_baseline TO trading_user;
GRANT SELECT, INSERT ON trading.derived_income_attribution TO trading_user;

CREATE OR REPLACE FUNCTION trading.reject_attribution_evidence_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'attribution evidence is insert-only';
END;
$$;
DROP TRIGGER IF EXISTS order_evidence_insert_only ON trading.order_strategy_evidence;
CREATE TRIGGER order_evidence_insert_only BEFORE UPDATE OR DELETE ON trading.order_strategy_evidence
FOR EACH ROW EXECUTE FUNCTION trading.reject_attribution_evidence_mutation();
DROP TRIGGER IF EXISTS flat_baseline_insert_only ON trading.position_flat_baseline;
CREATE TRIGGER flat_baseline_insert_only BEFORE UPDATE OR DELETE ON trading.position_flat_baseline
FOR EACH ROW EXECUTE FUNCTION trading.reject_attribution_evidence_mutation();
DROP TRIGGER IF EXISTS derived_income_insert_only ON trading.derived_income_attribution;
CREATE TRIGGER derived_income_insert_only BEFORE UPDATE OR DELETE ON trading.derived_income_attribution
FOR EACH ROW EXECUTE FUNCTION trading.reject_attribution_evidence_mutation();
