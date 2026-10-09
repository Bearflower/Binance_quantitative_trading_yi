-- ============================================================================
-- HRS 重复记账「历史存量」清理（一次性数据迁移，非幂等入口）
-- 文件：database/postgres/migrations/2026-10-09-hrs-pnl-dedup.sql
--
-- ⚠️ 严禁放入 init-scripts/：该目录在每次部署都会被 psql 以 ON_ERROR_STOP=on
--    重跑，会对未来「合法同额成交」再次判定为重复而误删。必须作为一次性手动
--    迁移执行。
--
-- 根因（详见 strategies/hrs/strategy.py::_writeback_pnl_for_full_close）：
--   旧实现存在确定性双写：
--     ① 先 insert_pnl_summary（close_reason=NULL）；
--     ② 再 mark_stop_loss → log_stop_loss → _mark_existing_close_record，其匹配
--        条件含 `order_type <> 'PNL_SUMMARY'`，且匹配窗口仅 ±10 分钟，实际平仓
--        与检测时刻常相隔数小时 → 匹配失败 → 降级再 insert_pnl_summary，同一笔
--        平仓落两条金额完全相同的记录。
--   叠加缺少幂等保护（回写的是「自开仓起的累计已实现盈亏」，进程重启/监控循环
--   重复命中时会按同一金额再写一条）。代码侧已改为「单条写入 + close_reason +
--   since 幂等去重」，本脚本清理存量。
--
-- 清理口径（用户 2026-10-09 确认）：按 (strategy, symbol, side, realized_pnl)
--   分组，保留最早一条，删除其余；仅作用于 strategy='hrs'。
--
-- ★ R-B1 修正（2026-10-09 C 段审查发现，已修订）：
--   原实现在「整组」上排序后无条件删除 rn>1，会把**真实成交**一起删掉。
--   生产库只读复现：57 条待删中 9 条为真实成交（WLDUSDT 3 笔独立 LIMIT、
--   BEATUSDT TAKE_PROFIT×2+STOP、CLUSDT、BZUSDT CONDITIONAL_ORDER 等）。
--   修正：**删除候选仅限 `order_type='PNL_SUMMARY'` 的汇总行**，真实成交永不删除。
--   修正后删除 48 条汇总重复，0 条真实成交。
--
-- ★ 止损标记迁移（同轮新增，处理上面修正的副作用）：
--   上述 48 条待删行中有 18 条带 `close_reason='STOP_LOSS'`（HRS 全部 19 条止损
--   标记中的 18 条——标记恰好都落在被重复写入的那条汇总行上）。若只删不迁，看板
--   「最近止损次数」会从「重复多计」掉到「几乎为 0」（另一头失真）。因此在删除前
--   把待删行上的 STOP_LOSS 标记搬到**同组存活行（rn=1）**，使统计回到「1 次平仓
--   = 1 条标记」。存活行原本已有 close_reason 时不覆盖。
--
-- 已知局限（需人工确认后执行）：同一币种/方向的两次合法平仓若金额恰好相同，
--   会被本规则判为重复而删除较早之外的**汇总行**（真实成交不受影响）。
--
-- 执行方式（部署后从本地手动执行一次；deploy 流程不会自动同步本目录）：
--   ssh -i <私钥> root@43.156.242.184 \
--     "docker exec -i trading_system-postgres psql --set ON_ERROR_STOP=on \
--           -U trading_user -d trading_platform" \
--     < database/postgres/migrations/2026-10-09-hrs-pnl-dedup.sql
-- ============================================================================

BEGIN;

-- 0. 只读预检：列出重复组及其 order_type 组成（真实成交多的组排在最前，便于核对）
\echo '--- 预检：HRS 同额重复组 ---'
SELECT symbol, side, realized_pnl,
       count(*) AS 记录数,
       string_agg(order_type, ' + ' ORDER BY executed_at ASC, id ASC) AS 组成,
       min(executed_at) AS 最早,
       max(executed_at) AS 最晚
FROM trading.trade_records
WHERE strategy = 'hrs' AND realized_pnl IS NOT NULL
GROUP BY symbol, side, realized_pnl
HAVING count(*) > 1
ORDER BY (count(*) FILTER (WHERE order_type <> 'PNL_SUMMARY')) DESC, symbol;

-- 1. 计算组内排名（整个组一起排序，保证「最早一条」判定与删除候选口径一致）
CREATE TEMP TABLE _hrs_ranked ON COMMIT DROP AS
SELECT id, symbol, side, realized_pnl, order_type, close_reason,
       ROW_NUMBER() OVER (
           PARTITION BY symbol, side, realized_pnl
           ORDER BY executed_at ASC, id ASC
       ) AS rn
FROM trading.trade_records
WHERE strategy = 'hrs' AND realized_pnl IS NOT NULL;

-- 2. 止损标记迁移：待删汇总行上的 STOP_LOSS → 同组存活行（rn=1，且存活行尚无标记）
UPDATE trading.trade_records tr
SET close_reason = 'STOP_LOSS'
FROM _hrs_ranked s
WHERE tr.id = s.id
  AND s.rn = 1
  AND tr.close_reason IS NULL
  AND EXISTS (
      SELECT 1 FROM _hrs_ranked d
      WHERE d.rn > 1
        AND d.order_type = 'PNL_SUMMARY'
        AND d.close_reason = 'STOP_LOSS'
        AND d.symbol = s.symbol
        AND d.side = s.side
        AND d.realized_pnl = s.realized_pnl
  );

-- 3. 备份待删行（只含 PNL_SUMMARY 汇总重复行；表已存在则复用，重复执行写入 0 行）
CREATE TABLE IF NOT EXISTS trading.trade_records_hrs_dedup_backup_20261009
    (LIKE trading.trade_records);

INSERT INTO trading.trade_records_hrs_dedup_backup_20261009
SELECT tr.*
FROM trading.trade_records tr
JOIN _hrs_ranked r ON r.id = tr.id
WHERE r.rn > 1 AND r.order_type = 'PNL_SUMMARY';

-- 4. 删除重复汇总行（真实成交 order_type <> 'PNL_SUMMARY' 永不进入候选）
DELETE FROM trading.trade_records tr
USING _hrs_ranked r
WHERE tr.id = r.id AND r.rn > 1 AND r.order_type = 'PNL_SUMMARY';

\echo '--- 清理结果 ---'
SELECT (SELECT count(*) FROM _hrs_ranked WHERE rn > 1 AND order_type = 'PNL_SUMMARY') AS 本次删除行数,
       (SELECT count(*) FROM trading.trade_records_hrs_dedup_backup_20261009)          AS 备份表行数;

-- 5. 复核 1：备份表中不应出现真实成交（预期 0 行）
\echo '--- 复核 1：被删行中是否有真实成交（应为 0 行）---'
SELECT order_type, count(*) AS 行数
FROM trading.trade_records_hrs_dedup_backup_20261009
WHERE order_type <> 'PNL_SUMMARY'
GROUP BY order_type;

-- 6. 复核 2：清理后同一同额组内不应再有 >1 条 PNL_SUMMARY（预期 0 行）
\echo '--- 复核 2：清理后残留的汇总重复组（应为 0 行）---'
SELECT symbol, side, realized_pnl, count(*) AS 汇总行数
FROM trading.trade_records
WHERE strategy = 'hrs' AND realized_pnl IS NOT NULL AND order_type = 'PNL_SUMMARY'
GROUP BY symbol, side, realized_pnl
HAVING count(*) > 1;

COMMIT;
