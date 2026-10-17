-- 已迁入 init-scripts/11-execution-facts.sql，init-scripts 为权威执行路径（2026-10-15）。
-- D1 只读采集与不可变原始事实：execution_facts 四张表
-- =========================================================
-- 文件放置在 migrations/ 目录下，但**本轮不随部署自动执行**。
-- 原因：该功能的消费方（会计/资金流核对、策略对账、归因）尚未在同一部署流程中迁入，
-- 贸然执行会让生产库多出空表却无任何业务方消费，属于"孤儿 schema 对象"。
-- 确认消费方后应把本文件迁入 database/postgres/init-scripts/ 才会随部署生效。
-- 本轮仅落仓库，绝不 apply。
--
-- 幂等键语义（关键，防重复计入）：
--   - execution_fact_fill  : PRIMARY KEY (account_id, symbol, trade_id)
--     trade_id 取币安 userTrades 响应的 **id**（整数递增的成交唯一标识，
--     不是响应里的 tradeId 字段；同毫秒多笔的 id 仍可区分），复合含 symbol
--     避免"不同币种恰好同 id"被误判重复。禁止用时间窗+币种去重。
--   - execution_fact_income : PRIMARY KEY (account_id, tran_id)
--     tran_id 取 um/income 响应的 **tranId**（收入流水事件唯一标识）。
--   - execution_fact_cursor : UNIQUE NULLS NOT DISTINCT (source, symbol, window_start_ms)
--     每个 (数据源, 币种, 窗口起点) 对应一条采集游标，用于断点恢复。
--     symbol 可空（income 无 symbol 维度，symbol=NULL 参与唯一性判定），
--     必须用 NULLS NOT DISTINCT：PG 默认 NULL 互不相等，
--     同一 income 窗口会被允许重复插入多行游标，ON CONFLICT 永不命中，
--     断点恢复幂等键形同虚设。与姊妹迁移 decision-evidence.sql 口径一致。
--   - execution_fact_raw   : PRIMARY KEY (source, entity_key, entity_id)
--     原始留痕；entity_key 取值 fill / income，entity_id 就是上面两表的 id 值；
--     raw_payload 落库前必须脱敏（剔除 apiKey / secret / signature 等）。

BEGIN;

-- ── 成交明细事实表（userTrades 采集）
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

-- ── 收入流水事实表（um/income 采集，REALIZED_PNL / COMMISSION 等）
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

-- ── 采集游标表（断点恢复）
CREATE TABLE IF NOT EXISTS trading.execution_fact_cursor (
    id                BIGSERIAL   NOT NULL,
    source            TEXT        NOT NULL,          -- 'userTrades' / 'um/income' 等
    symbol            TEXT,
    window_start_ms   BIGINT      NOT NULL,
    window_end_ms     BIGINT,
    last_event_id     BIGINT,                       -- 本页最后一条的 id / tranId
    covered_until_ms  BIGINT,                       -- 本游标已覆盖的时间上界
    returned_count    INTEGER,
    page_complete     BOOLEAN     NOT NULL DEFAULT FALSE,  -- 返回条数 < limit 才 TRUE
    request_status    TEXT,                         -- 'ok' / 'error' / 'partial'
    error_detail      TEXT,
    retry_count       INTEGER     NOT NULL DEFAULT 0,
    last_fetched_at   TIMESTAMPTZ,
    schema_version    INTEGER     NOT NULL DEFAULT 1,
    UNIQUE NULLS NOT DISTINCT (source, symbol, window_start_ms)
);

-- ── 原始响应留痕表（脱敏后落库；与事实表主键一一对应）
CREATE TABLE IF NOT EXISTS trading.execution_fact_raw (
    source          TEXT        NOT NULL,
    entity_key      TEXT        NOT NULL,             -- 'fill' / 'income'
    entity_id       TEXT        NOT NULL,             -- 对应事实表的主键 id
    raw_payload     JSONB       NOT NULL,             -- 落库前必须脱敏（剔除密钥/签名）
    payload_sha256  TEXT        NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version  INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (source, entity_key, entity_id)
);

COMMIT;
