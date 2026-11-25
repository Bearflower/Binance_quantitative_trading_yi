-- 既有触发器自动使用替换后的函数；两种原始来源均保留首次交易所响应。
BEGIN;
CREATE OR REPLACE FUNCTION trading.guard_immutable_income() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
DECLARE old_raw jsonb;
BEGIN
    IF TG_OP='UPDATE' AND
       (to_jsonb(OLD)-'fetched_at') IS NOT DISTINCT FROM (to_jsonb(NEW)-'fetched_at') THEN
        RETURN NULL; -- exact normalized retry: retain original fact and first fetch time
    END IF;
    SELECT to_jsonb(r) INTO old_raw FROM trading.execution_fact_raw r
      WHERE source IN ('um/income','fapi/income') AND entity_key='income'
        AND entity_id=OLD.account_id||':'||OLD.income_type||':'||OLD.tran_id
      ORDER BY fetched_at, source LIMIT 1;
    RAISE EXCEPTION 'Contradictory immutable income observation'
      USING ERRCODE='P7501', DETAIL=jsonb_build_object('old',to_jsonb(OLD),'new',to_jsonb(NEW),'old_raw',old_raw)::text;
END $$;
CREATE OR REPLACE FUNCTION trading.guard_immutable_income_raw() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
BEGIN
    IF (OLD.source IN ('um/income','fapi/income') AND OLD.entity_key='income') OR
       (TG_OP='UPDATE' AND NEW.source IN ('um/income','fapi/income') AND NEW.entity_key='income') THEN
        IF TG_OP='UPDATE' AND (OLD.source,OLD.entity_key,OLD.entity_id)=(NEW.source,NEW.entity_key,NEW.entity_id)
           AND trading.income_raw_semantic(OLD.raw_payload)=trading.income_raw_semantic(NEW.raw_payload) THEN
            RETURN NULL; -- semantically identical wire retry preserves first raw/hash/time
        END IF;
        RAISE EXCEPTION 'Contradictory immutable income raw observation'
          USING ERRCODE='P7502',DETAIL=jsonb_build_object('old',to_jsonb(OLD),'new',to_jsonb(NEW))::text;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW; -- original fill raw behavior is deliberately unchanged
END $$;

-- 同一 UID、币种和成交 ID 已有证据时，另一模式的同号成交不能改写旧记录。
CREATE OR REPLACE FUNCTION trading.check_execution_fill_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,trading AS $$
BEGIN
    -- 旧成交允许补充首次缺失的字段。同一成交 ID 的已有金额及身份禁止改写或清空。
    IF NOT (jsonb_strip_nulls(to_jsonb(OLD) - 'fetched_at') <@ (to_jsonb(NEW) - 'fetched_at')) THEN
        RAISE EXCEPTION 'conflicting execution fill identity' USING ERRCODE='P7503';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS execution_fill_identity ON trading.execution_fact_fill;
CREATE TRIGGER execution_fill_identity BEFORE UPDATE ON trading.execution_fact_fill
FOR EACH ROW EXECUTE FUNCTION trading.check_execution_fill_identity();
-- source_kind 原有 PostgreSQL CHECK 只列举了 PM 和普通成交。
ALTER TABLE trading.order_strategy_evidence
    DROP CONSTRAINT IF EXISTS order_strategy_evidence_source_kind_check;
ALTER TABLE trading.order_strategy_evidence
    ADD CONSTRAINT order_strategy_evidence_source_kind_check
    CHECK (source_kind IN ('trade_record','pm_algo_link','classic_algo_link'));
COMMIT;
