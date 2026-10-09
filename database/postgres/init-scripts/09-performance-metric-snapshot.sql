-- ============================================
-- 绩效指标快照表 DDL（幂等）
--   定时任务将夏普比率、最大回撤等风险调整收益指标固化落库，
--   与 metric_snapshot（盈亏类指标）形成互补——后者衡量绝对收益，
--   本表面向风险调整后的绩效评估。
--
--   口径：
--     granularity: day|week|month|year
--     scope: total（账户级，合并所有策略）| strategy（单策略）
--     strategy_id: '' 表示 total 级；策略级存规范 id（btc_eth 等）
--     bucket_key: 区间起点（当天 / 周一 / 月1号 / 1月1号）
--     window_start / window_end: 本次实际参与计算的样本窗口起止日期
--       （滚动回溯窗口，不等同于 bucket_key 的日历周期，用于前端澄清
--        「月度回撤≠本月回撤，而是近 180 天滚动窗口」）
--
--   UNIQUE 约束用 '' 占位空串（而非 NULL），与 05-metric-snapshot.sql
--   范式一致，使复合唯一约束可幂等 UPSERT。
-- ============================================

CREATE TABLE IF NOT EXISTS public.performance_metric_snapshot (
    id            SERIAL PRIMARY KEY,
    granularity   VARCHAR(8)     NOT NULL,               -- day|week|month|year
    scope         VARCHAR(10)    NOT NULL,               -- total|strategy
    strategy_id   VARCHAR(32)    NOT NULL DEFAULT '',    -- ''=total级；策略级存规范id(btc_eth等)
    bucket_key    DATE           NOT NULL,               -- 区间起点(当天/周一/月1号/1月1号)
    sample_count  INTEGER        NOT NULL DEFAULT 0,
    sharpe        NUMERIC(12,6),                         -- 可空，年化夏普比率
    max_drawdown  NUMERIC(10,6),                         -- 可空，0~1 比例
    window_start  DATE,                                  -- 实际样本窗口起点（可空）
    window_end    DATE,                                  -- 实际样本窗口终点（可空）
    snapshot_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at    TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_perf_metric UNIQUE (granularity, scope, strategy_id, bucket_key)
);

-- 存量库补列（CREATE TABLE IF NOT EXISTS 不会为已存在的表加列；部署时 init-scripts
-- 会以 ON_ERROR_STOP=on 幂等重跑本文件，故此 ALTER 可安全生效）
ALTER TABLE public.performance_metric_snapshot
    ADD COLUMN IF NOT EXISTS window_start DATE,
    ADD COLUMN IF NOT EXISTS window_end   DATE;

CREATE INDEX IF NOT EXISTS idx_perf_metric_lookup
    ON public.performance_metric_snapshot(granularity, scope, strategy_id);
CREATE INDEX IF NOT EXISTS idx_perf_metric_bucket
    ON public.performance_metric_snapshot(granularity, scope, bucket_key);

-- ============================================
-- 授权：给看板连接用户读写权限
-- ============================================
GRANT SELECT, INSERT, UPDATE, DELETE ON public.performance_metric_snapshot TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE public.performance_metric_snapshot_id_seq TO trading_user;
