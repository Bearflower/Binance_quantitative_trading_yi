-- ============================================
-- D1 只读采集与不可变原始事实表（幂等）
--   trading.execution_fact_fill    —— userTrades 成交明细
--   trading.execution_fact_income  —— um/income 收入流水
--   trading.execution_fact_cursor  —— 采集游标（断点恢复）
--   trading.execution_fact_raw     —— 原始响应留痕（脱敏后）
--
--   幂等键语义：
--     fill   : PRIMARY KEY (account_id, symbol, trade_id)
--     income : PRIMARY KEY (account_id, tran_id)
--     cursor : UNIQUE NULLS NOT DISTINCT (source, account_id, income_type, symbol, window_start_ms)
--              account_id NOT NULL，income_type/symbol 可空，必须用 NULLS NOT DISTINCT；
--              PG 默认 NULL 互不相等，同一 income 窗口或 fills/fills 不同账户会被允许重复插入多行游标，
--              ON CONFLICT 必须用这 5 元组才能命中。
--     raw    : PRIMARY KEY (source, entity_key, entity_id)
--              entity_id 折叠：fill→account_id:symbol:trade_id、income→account_id:tran_id
--
--   脱敏口径：raw_payload 落库前必须剔除 apiKey / secret / signature 等敏感字段。
-- ============================================

CREATE SCHEMA IF NOT EXISTS trading;
GRANT ALL PRIVILEGES ON SCHEMA trading TO trading_user;

-- ── 成交明细事实表（userTrades 采集） ──────────────────────────
CREATE TABLE IF NOT EXISTS trading.execution_fact_fill (
    account_id       TEXT        NOT NULL,
    symbol           TEXT        NOT NULL,
    trade_id         BIGINT      NOT NULL,        -- 币安 userTrades 响应 id（自增整数，成交唯一标识）
    order_id         BIGINT,
    side             TEXT,
    qty              NUMERIC(20,8),
    price            NUMERIC(20,8),
    quote_qty        NUMERIC(20,8),
    commission       NUMERIC(20,8),
    commission_asset TEXT,
    realized_pnl     NUMERIC(20,8),
    trade_time_utc   TIMESTAMPTZ,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version   INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (account_id, symbol, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_exec_fill_symbol_time
    ON trading.execution_fact_fill(symbol, trade_time_utc);

GRANT SELECT, INSERT, UPDATE, DELETE ON trading.execution_fact_fill TO trading_user;

-- ── 收入流水事实表（um/income 采集，REALIZED_PNL / COMMISSION 等） ─
CREATE TABLE IF NOT EXISTS trading.execution_fact_income (
    account_id      TEXT        NOT NULL,
    tran_id         BIGINT      NOT NULL,          -- 币安 um/income 响应 tranId（事件唯一标识）
    symbol          TEXT        NOT NULL,
    income_type     TEXT        NOT NULL,
    income          NUMERIC(20,8),
    asset           TEXT,
    info            TEXT,
    trade_id        BIGINT,
    event_time_utc  TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version  INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (account_id, tran_id)
);

CREATE INDEX IF NOT EXISTS idx_exec_income_type_time
    ON trading.execution_fact_income(income_type, event_time_utc);

GRANT SELECT, INSERT, UPDATE, DELETE ON trading.execution_fact_income TO trading_user;

-- ── 采集游标表（断点恢复） ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.execution_fact_cursor (
    id                BIGSERIAL   NOT NULL,
    source            TEXT        NOT NULL,          -- 'userTrades' / 'um/income' 等
    account_id        TEXT        NOT NULL,          -- 账户标识（游标身份键的一部分）
    income_type       TEXT,                           -- income: 'REALIZED_PNL' 等；fills: NULL
    symbol            TEXT,
    window_start_ms   BIGINT      NOT NULL,
    window_end_ms     BIGINT,
    last_event_id     BIGINT,                       -- 本页最后一条的 id / tranId
    covered_until_ms  BIGINT,                       -- 本游标已覆盖的时间上界
    returned_count    INTEGER,
    page_complete     BOOLEAN     NOT NULL DEFAULT FALSE,  -- 返回条数 < limit 才 TRUE
    request_status    TEXT,                         -- 'ok' / 'error' / 'partial' / 'incomplete'
    error_detail      TEXT,
    retry_count       INTEGER     NOT NULL DEFAULT 0,
    last_fetched_at   TIMESTAMPTZ,
    schema_version    INTEGER     NOT NULL DEFAULT 1,
    -- 新唯一键：source + account_id + income_type + symbol + window_start_ms
    -- account_id NOT NULL，income_type/symbol 可空（用 NULLS NOT DISTINCT 让 NULL 也参与唯一性）
    UNIQUE NULLS NOT DISTINCT (source, account_id, income_type, symbol, window_start_ms)
);

-- 注意：旧游标行（缺 account_id/income_type 列）不会被新代码命中，
-- 新代码会从窗口起点重采，幂等 upsert 回补。冷隔离迁移，不 apply 任何 SQL 迁移。

GRANT SELECT, INSERT, UPDATE, DELETE ON trading.execution_fact_cursor TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE trading.execution_fact_cursor_id_seq TO trading_user;

-- ── 原始响应留痕表（脱敏后落库；与事实表主键一一对应） ─────────
CREATE TABLE IF NOT EXISTS trading.execution_fact_raw (
    source          TEXT        NOT NULL,
    entity_key      TEXT        NOT NULL,             -- 'fill' / 'income'
    entity_id       TEXT        NOT NULL,             -- 对应事实表的复合键折叠值
    raw_payload     JSONB       NOT NULL,             -- 落库前必须脱敏（剔除密钥/签名）
    payload_sha256  TEXT        NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version  INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (source, entity_key, entity_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON trading.execution_fact_raw TO trading_user;

DO $$
BEGIN
    RAISE NOTICE 'execution_facts 四张表创建完成: execution_fact_fill / income / cursor / raw';
END $$;
