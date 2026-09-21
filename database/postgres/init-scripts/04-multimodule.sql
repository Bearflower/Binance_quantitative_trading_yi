-- ============================================
-- 数据看板多模块改造 · 新增表 DDL（幂等）
-- 包含：权益快照 / ai_tuner 执行记录 / 策略持仓上报
-- ============================================

-- ============================================
-- ① 净资产历史快照表（模块①）
--    每日北京时间 23:30 落库一条，同日 UPSERT 幂等
-- ============================================
CREATE TABLE IF NOT EXISTS public.equity_snapshot (
    id                SERIAL PRIMARY KEY,
    snapshot_date     DATE NOT NULL UNIQUE,          -- 北京日期
    total_equity      DECIMAL(20,8) NOT NULL,         -- 账户总权益（含未实现盈亏）
    available_balance DECIMAL(20,8) NOT NULL,         -- 可用余额
    open_positions    INTEGER NOT NULL DEFAULT 0,     -- 当前非零持仓数
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_equity_snapshot_date ON public.equity_snapshot(snapshot_date);

-- ============================================
-- ② ai_tuner 周度调优执行记录表（模块③A）
--    weekly_job 每个策略 success/skip/error 落一条
-- ============================================
CREATE TABLE IF NOT EXISTS public.ai_tuner_runs (
    id            SERIAL PRIMARY KEY,
    run_key       VARCHAR(40) NOT NULL,              -- 一次周度调优批次标识（周日日期，如 2026-09-13）
    strategy_id   VARCHAR(32) NOT NULL,              -- 策略ID（btc_eth/new_coin/hrs/grid）
    strategy_name VARCHAR(64),
    status        VARCHAR(10) NOT NULL,              -- 'success' | 'skip' | 'error'
    executed_at   TIMESTAMP NOT NULL,                -- 北京时间执行结束时间
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_tuner_runs_skey ON public.ai_tuner_runs(run_key);

-- ============================================
-- ③ 策略当前持仓上报表（模块④）
--    各策略开/平仓时 UPSERT/DELETE，(strategy_id,symbol) 唯一
-- ============================================
CREATE TABLE IF NOT EXISTS trading.strategy_open_positions (
    strategy_id VARCHAR(32) NOT NULL,                -- 看板 _STRATEGY_KEY_MAP 的 key
    symbol      VARCHAR(20) NOT NULL,                -- 交易对（如 BTCUSDT）
    margin      DECIMAL(20,8) NOT NULL DEFAULT 0,     -- 当前占用保证金
    quantity    DECIMAL(20,8) NOT NULL DEFAULT 0,     -- 当前持仓数量
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_id, symbol)
);

-- ============================================
-- 授权：给看板/策略连接用户读写权限
-- （trading_user 由部署脚本在 docker-compose 中创建）
-- ============================================
GRANT SELECT, INSERT, UPDATE, DELETE ON public.equity_snapshot TO trading_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.ai_tuner_runs TO trading_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON trading.strategy_open_positions TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE public.equity_snapshot_id_seq TO trading_user;
GRANT USAGE, SELECT ON SEQUENCE public.ai_tuner_runs_id_seq TO trading_user;