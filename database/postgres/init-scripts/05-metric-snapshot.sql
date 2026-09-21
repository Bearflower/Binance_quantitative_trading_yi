-- ============================================
-- 数据看板提速改造 · 预计算落库表 DDL（幂等）
--   定时任务将 Binance 实时聚合结果固化落库，
--   前端请求退化为单次 DB 查询（<10ms），替代实时拉 Binance
-- ============================================

-- ============================================
-- 预计算指标快照表
--   一条记录 = 某粒度(day/week/month) 某聚合维度(total/strategy/symbol)
--               某个区间起点 的指标集
--   UNIQUE 用 '' 占位空串（而非 NULL），使复合唯一约束可幂等去重
-- ============================================
CREATE TABLE IF NOT EXISTS public.metric_snapshot (
    id            SERIAL PRIMARY KEY,
    granularity   VARCHAR(8)    NOT NULL,               -- day|week|month
    scope         VARCHAR(10)   NOT NULL,               -- total|strategy|symbol
    strategy_id   VARCHAR(32)   NOT NULL DEFAULT '',    -- ''=total 级
    symbol        VARCHAR(20)   NOT NULL DEFAULT '',    -- ''=非 symbol 级
    bucket_key    DATE          NOT NULL,               -- 区间起点（天/周一/月1号）
    bucket_start  TIMESTAMP     NOT NULL,               -- 区间实际开始时间
    bucket_end    TIMESTAMP     NOT NULL,               -- 区间实际结束（进行中区间=本次生成时刻）
    label         VARCHAR(32)   NOT NULL,               -- 前端展示标签，如 "09/10"
    net_pnl       DECIMAL(20,8) NOT NULL DEFAULT 0,     -- 净盈亏（已扣佣金）
    gross_pnl     DECIMAL(20,8) NOT NULL DEFAULT 0,     -- 毛利润
    commission    DECIMAL(20,8) NOT NULL DEFAULT 0,     -- 佣金支出（负值）
    wins          INTEGER       NOT NULL DEFAULT 0,     -- 盈利笔数
    losses        INTEGER       NOT NULL DEFAULT 0,     -- 亏损笔数
    closed_count  INTEGER       NOT NULL DEFAULT 0,     -- 平仓笔数（=wins+losses）
    fill_count    INTEGER       NOT NULL DEFAULT 0,     -- 成交数（Binance 口径）
    order_count   INTEGER       NOT NULL DEFAULT 0,     -- 委托数（DB trade_records 口径）
    snapshot_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP, -- 本次计算时间（新鲜度/兜底判断）
    created_at    TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_metric UNIQUE (granularity, scope, strategy_id, symbol, bucket_key)
);

CREATE INDEX IF NOT EXISTS idx_metric_lookup
    ON public.metric_snapshot(granularity, scope, strategy_id, symbol);
CREATE INDEX IF NOT EXISTS idx_metric_bucket
    ON public.metric_snapshot(granularity, scope, bucket_key);

-- ============================================
-- 授权：给看板连接用户读写权限
-- ============================================
GRANT SELECT, INSERT, UPDATE, DELETE ON public.metric_snapshot TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE public.metric_snapshot_id_seq TO trading_user;