-- Prospective STOP provenance only. This table does not assert an executed exit.
CREATE TABLE IF NOT EXISTS trading.aggressive_stop_bindings (
    account_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    episode_entry_order_id BIGINT NOT NULL CHECK (episode_entry_order_id > 0),
    algo_id BIGINT NOT NULL CHECK (algo_id > 0),
    stop_side TEXT NOT NULL CHECK (stop_side IN ('BUY', 'SELL')),
    stop_quantity NUMERIC(28, 12) NOT NULL CHECK (stop_quantity > 0),
    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    audit_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (audit_state IN ('pending', 'verified', 'no_fill', 'review')),
    next_audit_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_audit_at TIMESTAMPTZ,
    last_observed_status TEXT,
    terminal_observations INTEGER NOT NULL DEFAULT 0 CHECK (terminal_observations >= 0),
    last_audit_error TEXT,
    PRIMARY KEY (account_id, symbol, algo_id),
    UNIQUE (account_id, symbol, algo_id, strategy, episode_entry_order_id, stop_side)
);

CREATE INDEX IF NOT EXISTS idx_aggressive_stop_bindings_episode
    ON trading.aggressive_stop_bindings(account_id, strategy, symbol, episode_entry_order_id);

GRANT SELECT, INSERT, UPDATE ON trading.aggressive_stop_bindings TO trading_user;

-- Only fully verified closed episodes may enter this immutable exit ledger.
CREATE TABLE IF NOT EXISTS trading.aggressive_stop_exits (
    account_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    episode_entry_order_id BIGINT NOT NULL CHECK (episode_entry_order_id > 0),
    algo_id BIGINT NOT NULL CHECK (algo_id > 0),
    order_id BIGINT NOT NULL CHECK (order_id > 0),
    stop_side TEXT NOT NULL CHECK (stop_side IN ('BUY', 'SELL')),
    quantity NUMERIC(28, 12) NOT NULL CHECK (quantity > 0),
    realized_pnl NUMERIC(28, 12) NOT NULL,
    commission NUMERIC(28, 12) NOT NULL CHECK (commission >= 0),
    net_pnl NUMERIC(28, 12) NOT NULL CHECK (net_pnl = realized_pnl - commission),
    entry_first_fill_time_ms BIGINT NOT NULL CHECK (entry_first_fill_time_ms > 0),
    last_fill_time_ms BIGINT NOT NULL CHECK (last_fill_time_ms > entry_first_fill_time_ms),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol, algo_id),
    UNIQUE (account_id, symbol, order_id),
    FOREIGN KEY (account_id, symbol, algo_id, strategy, episode_entry_order_id, stop_side)
        REFERENCES trading.aggressive_stop_bindings(
            account_id, symbol, algo_id, strategy, episode_entry_order_id, stop_side
        )
);

CREATE INDEX IF NOT EXISTS idx_aggressive_stop_exits_cooldown
    ON trading.aggressive_stop_exits(account_id, strategy, symbol, last_fill_time_ms DESC);

CREATE TABLE IF NOT EXISTS trading.aggressive_stop_exit_trades (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_id BIGINT NOT NULL CHECK (trade_id > 0),
    algo_id BIGINT NOT NULL,
    PRIMARY KEY (account_id, symbol, trade_id),
    FOREIGN KEY (account_id, symbol, algo_id)
        REFERENCES trading.aggressive_stop_exits(account_id, symbol, algo_id)
);

GRANT SELECT, INSERT ON trading.aggressive_stop_exits TO trading_user;
GRANT SELECT, INSERT ON trading.aggressive_stop_exit_trades TO trading_user;
