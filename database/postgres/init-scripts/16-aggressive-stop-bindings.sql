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
    PRIMARY KEY (account_id, symbol, algo_id)
);

CREATE INDEX IF NOT EXISTS idx_aggressive_stop_bindings_episode
    ON trading.aggressive_stop_bindings(account_id, strategy, symbol, episode_entry_order_id);

GRANT SELECT, INSERT ON trading.aggressive_stop_bindings TO trading_user;
