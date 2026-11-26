-- 迁移管理员执行。同一 UID 的切换证据独立于旧版每日 PM 快照。
BEGIN;
DO $$ BEGIN
    IF current_user <> 'trading_migration_admin' THEN
        RAISE EXCEPTION 'run as trading_migration_admin';
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS trading.account_mode_equity (
    account_id text NOT NULL CHECK (btrim(account_id) <> ''),
    cutover_ms bigint NOT NULL CHECK (cutover_ms > 0),
    observation text NOT NULL CHECK (observation IN ('pm_final', 'classic_open', 'classic_daily')),
    snapshot_date date,
    observed_at timestamptz NOT NULL,
    total_equity numeric NOT NULL,
    available_balance numeric NOT NULL,
    open_positions integer NOT NULL CHECK (open_positions >= 0),
    evidence_ref text,
    exchange_uid_sha256 text,
    pm_anchor text GENERATED ALWAYS AS (CASE WHEN observation='classic_open' THEN 'pm_final' END) STORED,
    CHECK ((observation = 'classic_daily' AND exchange_uid_sha256 IS NULL)
        OR (observation <> 'classic_daily' AND exchange_uid_sha256 IS NOT NULL
            AND exchange_uid_sha256 ~ '^[0-9a-f]{64}$')),
    UNIQUE (account_id, cutover_ms, observation, exchange_uid_sha256),
    FOREIGN KEY (account_id, cutover_ms, pm_anchor, exchange_uid_sha256)
        REFERENCES trading.account_mode_equity (account_id, cutover_ms, observation, exchange_uid_sha256),
    CHECK ((observation = 'classic_daily' AND snapshot_date IS NOT NULL)
        OR (observation <> 'classic_daily' AND snapshot_date IS NULL AND open_positions = 0
            AND evidence_ref IS NOT NULL AND btrim(evidence_ref) <> '')),
    CHECK ((observation = 'pm_final') = (observed_at < to_timestamp(cutover_ms / 1000.0))),
    UNIQUE NULLS NOT DISTINCT (account_id, cutover_ms, observation, snapshot_date)
);
COMMENT ON COLUMN trading.account_mode_equity.total_equity IS
    'pm_final: PAPI accountEquity (USD valuation); classic_open/classic_daily: single-asset FAPI totalMarginBalance (USDT)';
COMMIT;
