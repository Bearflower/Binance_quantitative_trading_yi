-- 币安 PM um/income 的 tranId 仅在同一收入类型内唯一。
-- 2026-11-02 只读清点：10 月 1,302 条三类收入中，303 个 tranId 跨类型重复。
-- 先改变键再采集；事实与人工复核表原有行保留不变。
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE trading.execution_fact_income
    DROP CONSTRAINT execution_fact_income_pkey;
ALTER TABLE trading.execution_fact_income
    ADD CONSTRAINT execution_fact_income_pkey
    PRIMARY KEY (account_id, income_type, tran_id);

ALTER TABLE trading.reviewed_income_attribution
    DROP CONSTRAINT reviewed_income_attribution_pkey;
ALTER TABLE trading.reviewed_income_attribution
    ADD CONSTRAINT reviewed_income_attribution_pkey
    PRIMARY KEY (account_id, income_type, tran_id);

COMMIT;
