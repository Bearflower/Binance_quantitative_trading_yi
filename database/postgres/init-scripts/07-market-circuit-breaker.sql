-- ============================================
-- 组合级单边行情熔断 · 指数快照表 DDL（幂等）
--   指数计算器（data_backend 每小时 03 分）按池各写一条，
--   三策略（MTPCS 原版/激进版、HRS）开仓前读取实时判定（不落布尔状态）。
--   文档依据：docs/requirements/market_circuit_breaker/单边行情空头熔断需求文档.md（v1.1，第 6 节）
-- ============================================

-- 熔断指数快照表
--   一条记录 = 某池(mtpcs/hrs) 某北京整点 的池等权 1h 涨跌幅
--   UNIQUE (pool, index_hour)：同池同一整点只保留一条，幂等 UPSERT
CREATE TABLE IF NOT EXISTS public.market_circuit_breaker_index (
    id           BIGSERIAL PRIMARY KEY,
    pool         VARCHAR(20)    NOT NULL,               -- 'mtpcs' | 'hrs'（分池双指数）
    index_hour   TIMESTAMPTZ    NOT NULL,               -- 对应当前整点（K线收盘时点，北京整点）
    equal_weight DOUBLE PRECISION NOT NULL,             -- 池等权 1h 涨幅（小数，单币已 cap±10%）
    equal_weight_12h DOUBLE PRECISION NULL,             -- 池等权 12h 累计涨幅（二期，单币已 cap±10%）
    symbol_count INT            NOT NULL,               -- 参与 1h 计算的有效标的数
    symbols      JSONB          DEFAULT '[]'::jsonb,    -- 参与 1h 计算的标的列表
    symbol_count_12h INT        NULL,                   -- 参与 12h 计算的有效标的数（二期）
    symbols_12h  JSONB          DEFAULT '[]'::jsonb,    -- 参与 12h 计算的标的列表（二期）
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT uq_cb_index UNIQUE (pool, index_hour)
);

-- 二期迁移（幂等）：为老表补充 12h 累计列；已存在的表走下面语句，全新表上面 CREATE 已含
ALTER TABLE public.market_circuit_breaker_index
    ADD COLUMN IF NOT EXISTS equal_weight_12h DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS symbol_count_12h INT,
    ADD COLUMN IF NOT EXISTS symbols_12h JSONB DEFAULT '[]'::jsonb;

-- 按 (pool, index_hour) 倒序索引：策略按整点读取最新指数
CREATE INDEX IF NOT EXISTS idx_cb_index_pool_hour
    ON public.market_circuit_breaker_index(pool, index_hour DESC);

-- 授权：给交易/看板连接用户读写权限
GRANT SELECT, INSERT, UPDATE, DELETE ON public.market_circuit_breaker_index TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE public.market_circuit_breaker_index_id_seq TO trading_user;
