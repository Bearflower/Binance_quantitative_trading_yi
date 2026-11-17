-- HRS cashflow-adjusted equity and loss gates. Safe to apply repeatedly.
-- Existing account state is never replaced by schema initialization.
CREATE TABLE IF NOT EXISTS trading.hrs_loss_gate_state (
    account_id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    ledger_state JSONB NOT NULL,
    gate_state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS trading.hrs_risk_equity_checkpoints (
    account_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    equity NUMERIC NOT NULL CHECK (equity > 0 AND equity < 'Infinity'::numeric),
    units NUMERIC NOT NULL CHECK (units > 0 AND units < 'Infinity'::numeric),
    nav NUMERIC NOT NULL CHECK (nav > 0 AND nav < 'Infinity'::numeric),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, checkpoint_id),
    UNIQUE (account_id, segment_id, as_of)
);
CREATE TABLE IF NOT EXISTS trading.hrs_risk_ledger_events (
    account_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('income', 'cashflow')),
    event_id TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (account_id, event_kind, event_id)
);
CREATE TABLE IF NOT EXISTS trading.hrs_loss_gate_reset_audit (
    account_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    version BIGINT NOT NULL,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    evidence TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
    reset_at TIMESTAMPTZ NOT NULL,
    before_state JSONB NOT NULL,
    after_state JSONB NOT NULL,
    PRIMARY KEY (account_id, version)
);
-- Preserve compatibility with isolated test clusters that have no app role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_user') THEN
        GRANT SELECT, INSERT, UPDATE ON trading.hrs_loss_gate_state TO trading_user;
        GRANT SELECT, INSERT ON trading.hrs_risk_equity_checkpoints,
            trading.hrs_risk_ledger_events, trading.hrs_loss_gate_reset_audit TO trading_user;
    END IF;
END
$$;
