-- ============================================
-- 数据看板提速 · 持仓/占用对账快照表 DDL（幂等）
--   小时级对账任务从币安持仓 + trading.strategy_open_positions +
--   trading.trade_records（下单标注策略）推导各策略当前持仓量与持仓保证金，
--   以占用比 = 持仓保证金 / 分配金额 供月度资金分配展示。
--   前端概览/占用直接读本表，避免依赖各策略实时上报的不可靠性。
-- ============================================
CREATE TABLE IF NOT EXISTS public.strategy_position_snapshot (
    strategy_id         VARCHAR(32)     PRIMARY KEY,              -- 策略ID（唯一）
    strategy_name       VARCHAR(50)     NOT NULL DEFAULT '',      -- 策略名称（前端展示）
    open_position_count INTEGER         NOT NULL DEFAULT 0,       -- 当前持仓数（币种数）
    open_margin         DECIMAL(20,8)   NOT NULL DEFAULT 0,       -- 当前持仓保证金（USDT）
    allocated_amount    DECIMAL(20,8)   NOT NULL DEFAULT 0,       -- 分配金额（来自月度资金分配）
    occupied_ratio      DECIMAL(10,6)   NOT NULL DEFAULT 0,       -- 占用比 = open_margin / allocated_amount
    snapshot_at         TIMESTAMP,                                -- 本次对账计算时间（新鲜度/兜底判断）
    updated_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_position_snapshot UNIQUE (strategy_id)
);

-- 授权：给看板连接用户读写权限
GRANT SELECT, INSERT, UPDATE, DELETE ON public.strategy_position_snapshot TO trading_user;

-- ============================================
-- 持仓/占用对账快照 · 历史明细表（幂等）
--   随小时级对账任务同步追加一条，保留占用比/持仓的历史时间序列，
--   供前端「占用比趋势」按天聚合展示。
--   以 (strategy_id, snapshot_hour) 为唯一键，同一小时重复对账覆盖，避免重复行。
-- ============================================
CREATE TABLE IF NOT EXISTS public.strategy_position_snapshot_history (
    strategy_id         VARCHAR(32)     NOT NULL,                   -- 策略ID
    strategy_name       VARCHAR(50)     NOT NULL DEFAULT '',        -- 策略名称
    open_position_count INTEGER         NOT NULL DEFAULT 0,         -- 该小时持仓数
    open_margin         DECIMAL(20,8)   NOT NULL DEFAULT 0,         -- 该小时持仓保证金（USDT）
    allocated_amount    DECIMAL(20,8)   NOT NULL DEFAULT 0,         -- 分配金额
    occupied_ratio      DECIMAL(10,6)   NOT NULL DEFAULT 0,         -- 占用比
    snapshot_hour       TIMESTAMP       NOT NULL,                   -- 对账所属小时（date_trunc('hour')）
    updated_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_id, snapshot_hour)
);

-- 历史表按小时查询索引（趋势按天聚合）
CREATE INDEX IF NOT EXISTS idx_position_snapshot_history_hour
    ON public.strategy_position_snapshot_history(snapshot_hour);

-- 授权：给看板连接用户读写权限
GRANT SELECT, INSERT, UPDATE, DELETE ON public.strategy_position_snapshot_history TO trading_user;