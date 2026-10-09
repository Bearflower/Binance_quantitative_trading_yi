-- grid V2.5.4 实时风险预警 · 行情库 schema（计划 §1.2，S3 定稿 2026-10-08）
-- 独立 DB：agg_trades（逐笔，72h 滚动）+ price_samples_1s（1s 稠密采样，30 天不滚动）
-- 单品种库：库文件按品种命名（ethusdt_aggtrades.sqlite），主键不含 symbol；
--           如需多品种采集，按品种各建一库，或将两表主键改为 (symbol, id) 复合键。
-- 幂等：可重复执行。

-- 逐笔聚合成交
CREATE TABLE IF NOT EXISTS agg_trades (
  agg_trade_id   INTEGER PRIMARY KEY,          -- 币安按品种聚合后的唯一 ID，天然主键
  symbol         TEXT NOT NULL,                -- ETHUSDT
  price          TEXT NOT NULL,                -- Decimal 字符串，避免浮点误差
  quantity       TEXT NOT NULL,                -- Decimal 字符串
  first_trade_id INTEGER,
  last_trade_id  INTEGER,
  trade_time_ms  INTEGER NOT NULL,             -- 交易所事件时间 T（毫秒）
  is_buyer_maker INTEGER NOT NULL,             -- 0/1
  received_at_ms INTEGER NOT NULL              -- 本地取得时刻：WS=真实接收时刻；REST/文件=本地拉取/导入时刻
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_agg_symbol_time
  ON agg_trades(symbol, trade_time_ms);

-- 1s 整秒采样价格序列（统计验证主表；决策点语义，需求 §5.3）
CREATE TABLE IF NOT EXISTS price_samples_1s (
  sample_ms         INTEGER PRIMARY KEY,      -- 整秒决策点 t（.000）；p(t)=不晚于 t 的最后一笔
  symbol            TEXT NOT NULL,            -- ETHUSDT
  price             TEXT NOT NULL,            -- 不晚于 t.000 的最后一笔成交价（T<=t，可早于 t）；空缺点沿用前锚点
  trade_count       INTEGER NOT NULL,         -- 窗口 (t-1s, t] 成交笔数；空缺点=0
  last_trade_id     INTEGER NOT NULL,         -- 锚点成交 agg_trade_id；空缺点沿用前一笔
  anchor_time_ms    INTEGER NOT NULL,         -- 锚点成交事件时间，恒 <= sample_ms（逐笔 72h 清理后锚距仍可判）
  materialized_at_ms INTEGER NOT NULL         -- 物化本地时刻（审计）
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_samples_symbol_time
  ON price_samples_1s(symbol, sample_ms);
