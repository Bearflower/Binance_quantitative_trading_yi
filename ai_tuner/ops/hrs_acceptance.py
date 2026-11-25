"""Production HRS acceptance, with a host scheduler and a read-only container probe."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

try:
    from ai_tuner.ops import digest_outbox
except ImportError:  # 以脚本方式直接运行时，当前目录即 ai_tuner/ops
    try:
        import digest_outbox
    except ImportError:  # 只读探针在策略容器内执行，容器内没有 outbox 模块
        digest_outbox = None

BEIJING = timezone(timedelta(hours=8))
START = datetime(2026, 10, 30, tzinfo=BEIJING)
POLICIES = {
    "hrs": (8, "reduced"), "btc_eth": (5, "capped"),
    "btc_eth_aggressive": (5, "reduced"), "new_coin": (5, "capped"),
}
CONTAINERS = {s: f"trading_system-{s}" for s in POLICIES}
PATTERNS = {
    "仅剩 TP2 后重启": re.compile(r"仅(?:剩)?TP2.*(?:接管|入场时间)|重启.*仅(?:剩)?TP2"),
    "微仓 N/A": re.compile(r"TP[12].*数量精度调整后为 ?0.*不可挂"),
    "BTC/ETH 加仓同步失败": re.compile(r"加仓.*(?:持仓查询失败|保护单重建失败|条件单失败)"),
    "精度元数据缺失": re.compile(r"精度.*(?:LOT_SIZE|PRICE_FILTER|缺失|不可用|未找到)"),
}
ALLOCATION_LOGS = re.compile(
    r"自动分配影子|月度资金分配.*(?:成功|失败|异常|完成)|资金分配冻结|采集.*(?:不完整|失败)"
)
UNICODE_ESCAPE = re.compile(r"\\u([a-fA-F0-9]{4})")
SYMBOL = re.compile(r"\b([A-Z0-9]{2,24}USDT)\b")


def decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("non-finite amount")
    return number


def evaluate_policies(rows, now):
    issues = []
    for strategy, expected in POLICIES.items():
        row = next((row for row in rows if row["strategy_id"] == strategy), None)
        if not row:
            issues.append(f"{strategy} 缺少活动政策")
            continue
        if ((row["revision"], row["mode"]) != expected
                or row["allow_add"] is not False or row["freeze_allocation"] is not True
                or not row["audited"] or row["effective_at"] > now
                or (strategy == "hrs" and decimal(row["entry_ceiling"]) != Decimal("26.18"))):
            issues.append(f"{strategy} r{row['revision']} 政策或审计漂移，需核对受控变更")
        if strategy == "hrs":
            from strategies.hrs.risk_budget import FLOOR, policy_budget_version
            try:
                budget_matches = policy_budget_version(row) == FLOOR
            except (ValueError, TypeError):
                budget_matches = False
            if not budget_matches:
                issues.append(f"hrs r{row['revision']} 预算版本与已批准最小订单政策不符")
            from strategies.hrs.margin_budget import HRS_OWNED, policy_margin_scope
            try:
                margin_matches = policy_margin_scope(row) == HRS_OWNED
            except (ValueError, TypeError):
                margin_matches = False
            if not margin_matches:
                issues.append(f"hrs r{row['revision']} 保证金口径与已批准自身额度及账户余额检查不符")
        expiry = row["expires_at"].astimezone(BEIJING)
        if expiry <= now + timedelta(hours=48):
            state = "已到期" if expiry <= now else "48 小时内到期"
            issues.append(f"{strategy} r{row['revision']} {state} {expiry.isoformat()}")
    return issues


def verify_reservation(row, order, positions):
    if (row["state"] != "submitting" or row["strategy_id"] not in POLICIES
            or not isinstance(order, dict) or order.get("symbol") != row["symbol"]
            or order.get("clientOrderId") != row["client_order_id"]
            or order.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
            or str(order.get("reduceOnly")).lower() == "true"):
        return False
    try:
        matching = [decimal(p["positionAmt"]) for p in positions
                    if p.get("symbol") == row["symbol"] and decimal(p["positionAmt"]) != 0]
        if len(matching) != 1:
            return False
        amount, = matching
        filled, requested = decimal(order["executedQty"]), decimal(order["origQty"])
        return (0 < abs(amount) <= filled <= requested == decimal(row["quantity"])
                and order.get("side") == ("BUY" if amount > 0 else "SELL")
                and (row["strategy_id"] != "hrs"
                     or abs(amount) == decimal(row["position_quantity"])))
    except (InvalidOperation, KeyError, ValueError):
        return False


def sample_candidates(logs):
    candidates = set()
    for strategy, when, message in logs:
        symbol = SYMBOL.search(message)
        if not symbol or strategy not in {"btc_eth", "btc_eth_aggressive"}:
            continue
        for label, pattern in PATTERNS.items():
            if label.startswith("BTC/ETH") and symbol[1] not in {"BTCUSDT", "ETHUSDT"}:
                continue
            if pattern.search(message):
                candidates.add((strategy, when, symbol[1], label))
    return sorted(candidates, key=lambda candidate: candidate[1])


def event_identity(message):
    try:
        parsed = json.loads(message)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {key: str(parsed[key]) for key in ("entry_order_id", "episode_entry_order_id", "tp2_order_id")
            if parsed.get(key) is not None and str(parsed[key]).isdigit()}


def same_sample_position(snapshot, identity):
    entry = snapshot.get("episode_entry_order_id") or snapshot.get("entry_order_id")
    if not entry or snapshot.get("grade") not in {"S", "A", "B", "C", "D"}:
        return None
    if any(str(snapshot.get(key)) == value for key, value in identity.items()):
        return str(entry)
    return None


def monthly_result(row, income, blocked):
    month = row["month"]
    if blocked:
        return f"{month} 门禁失败后出现分配写入，需核对作业记录"
    if (income.get("account_coverage_complete") is not True
            or income.get("strategy_attribution_complete") is not True):
        return f"{month} 收入覆盖或唯一归属未证实，待复核"
    try:
        entries = row["entries"]
        total = decimal(row["total_capital"])
        if total <= 0 or row["status"] != "active" or len(entries) != row["strategy_count"]:
            raise ValueError("allocation identity")
        if len({e["strategy_id"] for e in entries}) != len(entries):
            raise ValueError("duplicate strategy")
        if sum(decimal(e["allocated_amount"]) for e in entries) > total:
            raise ValueError("over-allocation")
        for entry in entries:
            basis = decimal(entry["initial_capital"])
            pnl = decimal(entry["realized_pnl"])
            ratio = decimal(entry["allocated_ratio"])
            allocated = decimal(entry["allocated_amount"])
            if (basis <= 0 or ratio < 0 or allocated < 0
                    or abs(pnl / basis - decimal(entry["return_rate"])) > Decimal(".000001")
                    or abs(total * ratio - allocated) > Decimal(".01")
                    or pnl != decimal(income["by_strategy"][entry["strategy_id"]]["realized_pnl_usdt"])):
                raise ValueError("allocation mismatch")
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return f"{month} 资金基数、收入或分配金额核对失败"
    return f"{month} 分配行与月度收入及金额算式一致"


def month_window(month):
    end = datetime.strptime(month, "%Y-%m").replace(tzinfo=BEIJING)
    start = (end - timedelta(days=1)).replace(day=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def read_shadow():
    path = "/app/logs/trial_evidence/allocation-shadow-latest.json"
    try:
        result = subprocess.run(["docker", "exec", "ai-tuner", "cat", path], check=True,
                                text=True, capture_output=True, timeout=20)
        data = json.loads(result.stdout)
        return {k: data.get(k) for k in
                ("account_id", "status", "mode", "allocation", "reasons", "report_id",
                 "start_ms", "end_ms", "as_of", "activation_blocks") if k in data}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_hrs_evidence():
    """Read the existing DB-only reporter; never run an experiment or send."""
    try:
        result = subprocess.run(
            ["docker", "exec", "trading_system-hrs-acceptance", "cat", "/app/logs/hrs-acceptance.json"],
            check=True, text=True, capture_output=True, timeout=20)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def hrs_evidence_lines(report, now):
    pending = "HRS 独立运行验收：证据待核实；收益验收未通过。"
    try:
        if (not isinstance(report, dict) or report.get("report_version") != "hrs-continuous-acceptance-v1"
                or report.get("account_id") != "default"
                or report.get("database_snapshot_coherent") is not True):
            raise ValueError("unverified reporter identity")
        generated = datetime.fromisoformat(report["generated_at"])
        if generated.utcoffset() is None or not 0 <= (now-generated).total_seconds() <= 1800:
            raise ValueError("stale or future reporter")
        technical, financial = report["technical"], report["financial"]
        hours, closed = technical["consecutive_complete_hours"], financial["observed_closed_lifecycles"]
        if (type(hours) is not int or not 0 <= hours <= 24 or type(closed) is not int or closed < 0
                or technical.get("required_complete_hours") != 24
                or financial.get("required_lifecycles") != 5
                or technical.get("coverage_threshold") != .95):
            raise ValueError("unverified acceptance criteria")
        lines = [f"HRS 独立运行验收：连续完整小时 {hours}/24，"
                 f"自然闭合仓位 {closed}/5；工程状态 {financial.get('engineering_status', '待核实')}；"
                 f"证据时间 {generated.astimezone(BEIJING):%m-%d %H:%M}。"]
        lines.append("HRS 收益验收未通过：运行留证与收益改善分别验收；SL/TP 实验仍需"
                     "每方案至少20个合格闭合样本及两个月独立留出验证。")
        return lines
    except (KeyError, ValueError, TypeError, OverflowError):
        return [pending]


def previous_trading_day(now):
    today = now.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today


def summarize_hrs_reconciliation(now, events, *, journal_has_entries=True):
    start, end = previous_trading_day(now)
    heading = f"HRS 主动对账（{start:%Y-%m-%d} 北京时间）"
    if not journal_has_entries:
        return f"{heading}：上一交易日日志未取到。", True
    scoped = []
    for when, symbol, trigger, outcome in events:
        timestamp = datetime.fromisoformat(when)
        if start <= timestamp < end:
            scoped.append((timestamp, symbol, trigger, outcome))
    if not scoped:
        return f"{heading}：未记录主动对账。", False

    attempts = [event for event in scoped if event[1] != "*"]
    close_count = sum(trigger == "full_close" for _, _, trigger, _ in attempts)
    released = sum(outcome == "released" for _, _, _, outcome in attempts)
    latest = {}
    for _, symbol, _, outcome in sorted(attempts):
        latest[symbol] = outcome
    pending = {symbol: outcome for symbol, outcome in latest.items() if outcome != "released"}
    scan_failures = len(scoped) - len(attempts)
    line = (f"{heading}：{len(attempts)} 次（平仓 {close_count}，持仓同步 {len(attempts) - close_count}），"
            f"释放 {released} 笔，最后一次对账仍待重试 {len(pending)} 个品种。")
    if scan_failures:
        line += f" 预留扫描失败 {scan_failures} 次。"
    if pending:
        labels = {
            "exchange_query_failed": "币安查询失败",
            "entry_order_unverified": "入场订单未核实",
            "entry_order_not_terminal": "入场订单未终态",
            "position_unverified": "持仓未核实",
            "position_open": "仍有持仓",
            "orders_unverified": "挂单未核实",
            "ordinary_orders_open": "仍有普通挂单",
            "algo_orders_open": "仍有条件挂单",
            "claim_identity_missing": "缺少占位关联",
            "claim_not_released": "占位更新未生效",
            "multiple_reservations": "存在多笔未释放预留",
            "reservation_not_released": "预留更新未生效",
            "reconciliation_failed": "对账执行失败",
        }
        examples = [f"{symbol} {labels.get(reason, reason)}" for symbol, reason in sorted(pending.items())[:5]]
        line += " " + "；".join(examples) + (" 等。" if len(pending) > 5 else "。")
    return line, bool(pending or scan_failures)


def journal_lines(container, start, end):
    # 边界按 journal 的微秒精度分割，逐段处理以限制内存与单次查询耗时。
    while start < end:
        stop = min(start + timedelta(hours=2), end)
        result = subprocess.run(
            ["journalctl", f"CONTAINER_NAME={container}", "--since", f"@{int(start.timestamp())}",
             "--until", f"@{int(stop.timestamp()) - 1}.999999", "--no-pager", "-o", "json",
             "--output-fields=MESSAGE"], capture_output=True, text=True, timeout=35, check=True,
        )
        yield from result.stdout.splitlines()
        start = stop


def read_journal(since, reconciliation_window):
    read_until = datetime.now(BEIJING)
    samples, allocation, unavailable, reconciliations = {}, [], [], []
    hrs_journal_has_entries = False
    reconcile_start, reconcile_end = reconciliation_window
    sources = dict(CONTAINERS, tuner="ai-tuner")
    for strategy, container in sources.items():
        source_start = min(since, reconcile_start) if strategy == "hrs" else since
        any_rows = False
        try:
            for raw in journal_lines(container, source_start, read_until):
                any_rows = True
                row = json.loads(raw)
                when = datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1e6, BEIJING)
                message = row.get("MESSAGE")
                if not isinstance(message, str):
                    continue
                message = re.sub(r"\x1b\[[0-9;]*m", "", message)
                if strategy == "hrs" and reconcile_start <= when < reconcile_end:
                    hrs_journal_has_entries = True
                    try:
                        event = json.loads(message)
                    except ValueError:
                        event = None
                    if isinstance(event, dict) and event.get("event") == "hrs_close_reconciliation":
                        fields = tuple(event.get(key) for key in ("symbol", "trigger", "outcome"))
                        if all(isinstance(value, str) for value in fields):
                            reconciliations.append((when.isoformat(), *fields))
                if when < since:
                    continue
                identity = event_identity(message)
                message = UNICODE_ESCAPE.sub(lambda match: chr(int(match[1], 16)), message)
                if strategy == "tuner":
                    hit = ALLOCATION_LOGS.search(message)
                    if hit:
                        allocation.append((when.isoformat(), hit[0]))
                else:
                    for candidate in sample_candidates([(strategy, when.isoformat(), message)]):
                        samples[candidate] = identity
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            if strategy == "hrs":
                hrs_journal_has_entries = False
            unavailable.append(strategy)
            continue
        if not any_rows:
            unavailable.append(strategy)
    return ([(*candidate, identity) for candidate, identity in samples.items()],
            allocation, unavailable, reconciliations, hrs_journal_has_entries)


def digest(item):
    return hashlib.sha256(json.dumps(item, ensure_ascii=True, sort_keys=True, default=str).encode()).hexdigest()


def database_env(key):
    return os.environ.get(f"DATABASE_{key}") or os.environ[f"DB_{key}"]


async def probe(inputs):
    import asyncpg

    from scripts.verify_ledger_risk_release import GateError, verify_exchange
    from shared.accounting.income_integrity import require_income_integrity
    from shared.readonly_client import ReadOnlyBinanceClient

    if os.environ.get("TRADE_ACCOUNT_ID") != "default":
        raise RuntimeError("unexpected account binding")
    db = await asyncpg.connect(
        host=database_env("HOST"), port=int(database_env("PORT")), database=database_env("NAME"),
        user=database_env("USER"), password=database_env("PASSWORD"),
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "20000"},
    )
    client = None
    now = datetime.now(BEIJING)
    since = datetime.fromisoformat(inputs["since"])
    try:
        async with db.transaction(isolation="repeatable_read", readonly=True):
            if await db.fetchval("SHOW transaction_read_only") != "on":
                raise RuntimeError("database is not read-only")
            policies = await db.fetch("""
                SELECT p.*, EXISTS (
                    SELECT 1 FROM trading.strategy_risk_audit u
                    WHERE u.account_id=p.account_id AND u.strategy_id=p.strategy_id
                      AND u.revision=p.revision AND u.activated_at<=now()
                ) AS audited FROM trading.strategy_risk_active a
                JOIN trading.strategy_risk_policy p USING(account_id,strategy_id,revision)
                WHERE a.account_id='default' ORDER BY p.strategy_id
            """)
            row = await db.fetchrow("SELECT * FROM public.capital_allocation ORDER BY month DESC,id DESC LIMIT 1")
            current_allocation = dict(row) if row else None
            if current_allocation and isinstance(current_allocation["entries"], str):
                current_allocation["entries"] = json.loads(current_allocation["entries"])
            allocation_hash = digest(current_allocation)
            monthly = None
            hrs = next((e for e in current_allocation["entries"] if e["strategy_id"] == "hrs"), {}) if current_allocation else {}
            baseline = (current_allocation and row["month"] == "2026-11" and row["id"] == 12
                        and row["status"] == "active" and row["total_capital"] == Decimal("523.62")
                        and decimal(hrs.get("realized_pnl", "0")) == Decimal("-13.26424796"))
            if inputs["allocation_hash"] is None and baseline:
                monthly = f"{row['month']} 已验收基准，读取生产记录 id={row['id']}"
            elif current_allocation and inputs["allocation_hash"] != allocation_hash:
                start, end = month_window(row["month"])
                evidence = await db.fetchval("""
                    SELECT r.report FROM trading.ledger_report_active a
                    JOIN trading.ledger_report_run r USING(report_id)
                    WHERE a.account_id='default' AND r.account_id=a.account_id AND a.asset='USDT'
                      AND r.asset=a.asset AND a.window_key=$1 AND r.window_key=a.window_key
                      AND r.report_version=a.report_version AND r.start_ms=$2 AND r.end_ms=$3
                    ORDER BY r.created_at DESC LIMIT 1
                """, f"monthly:{start}", start, end)
                income = json.loads(evidence) if isinstance(evidence, str) else (evidence or {})
                expected = {"account_id": "default", "start_ms": start, "end_ms": end,
                            "requested_end_ms": end, "window_key": f"monthly:{start}"}
                if any(income.get(k) != v for k, v in expected.items()):
                    income = {}
                shadow = inputs.get("shadow") or {}
                shadow_blocked = (shadow.get("start_ms") == start and shadow.get("end_ms") == end
                                  and shadow.get("status") == "BLOCKED_DATA")
                monthly = monthly_result(current_allocation, income, shadow_blocked)
                if monthly.endswith("算式一致"):
                    monthly += "；历史资金基数与配置写入仍需独立核对"
            if current_allocation is None:
                monthly = "缺少月度分配记录"
            reservations = await db.fetch("""
                SELECT r.*, p.remaining_quantity AS position_quantity
                FROM trading.entry_risk_reservation r
                LEFT JOIN hrs.hrs_positions p ON r.strategy_id='hrs' AND p.symbol=r.symbol
                WHERE r.account_id='default' AND r.state<>'released'
                ORDER BY r.strategy_id,r.symbol
            """)
            summaries = await db.fetch("""
                SELECT id,symbol,realized_pnl FROM trading.trade_records
                WHERE strategy='hrs' AND coalesce(account_id,'default')='default'
                  AND order_type='PNL_SUMMARY' AND pnl_source='theoretical_estimate'
                  AND executed_at >= $1 ORDER BY executed_at DESC LIMIT 20
            """, since.astimezone(BEIJING).replace(tzinfo=None))
            candidates = []
            for strategy, when, symbol, category, identity in inputs["samples"][-20:]:
                event_at = datetime.fromisoformat(when).replace(tzinfo=None)
                # Time-window evidence is deliberately labelled a candidate.
                # Legacy events do not always carry a lifecycle or entry order ID.
                schema = {"btc_eth": "btc_eth", "btc_eth_aggressive": "btc_eth_aggressive"}[strategy]
                grades = await db.fetch(f"""
                    SELECT id,grade FROM {schema}.trade_signals WHERE symbol=$1
                      AND status='executed' AND executed_at <= $2 AND executed_at >= $2-interval '7 days'
                    ORDER BY executed_at DESC LIMIT 4
                """, symbol, event_at)
                trades = await db.fetchval("""
                    SELECT count(*) FROM trading.trade_records WHERE symbol=$1
                      AND strategy=$2 AND coalesce(account_id,'default')='default'
                      AND executed_at BETWEEN $3-interval '7 days' AND $3+interval '1 hour'
                """, symbol, strategy, event_at)
                saved = await db.fetchval("""
                    SELECT state_data->'positions'->$2::text FROM public.strategy_states
                    WHERE strategy_name=$1 AND state_key='main'
                """, strategy, symbol)
                snapshot = json.loads(saved) if isinstance(saved, str) else (saved or {})
                entry = same_sample_position(snapshot, identity)
                entry_record = await db.fetchval("""
                    SELECT count(*) FROM trading.trade_records
                    WHERE strategy=$1 AND symbol=$2 AND order_id=$3
                      AND coalesce(account_id,'default')='default'
                      AND order_type IN ('LIMIT','MARKET')
                """, strategy, symbol, entry) if entry else 0
                candidates.append(
                    (strategy, when, symbol, category, len(grades), trades, snapshot, entry, entry_record)
                )
            integrity = True
            try:
                await require_income_integrity(db, "default")
            except Exception:
                integrity = False

        issues = evaluate_policies(policies, now)
        if not integrity:
            issues.append("收入完整性门禁未通过")
        client = ReadOnlyBinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"],
                                     instance_id="hrs-acceptance", use_unified_account=True)
        positions = await client.get_position()
        orders = await client.get_open_orders()
        algos = await client.get_open_algo_orders()
        inventory_issue = None
        try:
            verify_exchange(positions, orders, algos)
        except GateError as exc:
            inventory_issue = str(exc)
            issues.append("交易所持仓及保护单门禁未通过：" + inventory_issue)
        for row in reservations:
            if inventory_issue:
                break
            try:
                order = await client.get_order_by_client_id(row["symbol"], row["client_order_id"])
                verified = verify_reservation(row, order, positions)
            except Exception:
                verified = False
            if not verified:
                issues.append(f"{row['strategy_id']} {row['symbol']} 预留 {row['state']} 待逐笔核对")
        visible_positions = [p["symbol"] for p in positions if decimal(p["positionAmt"]) != 0]
        sample_lines = []
        for strategy, when, symbol, category, grade_count, trade_count, snapshot, entry, entry_record in candidates:
            status = "当前有仓" if symbol in visible_positions else "当前无仓"
            verdict = "待生命周期三方核对"
            if entry_record == 1 and symbol in visible_positions and not inventory_issue:
                try:
                    fills = await client.get_user_trades(symbol, order_id=entry, limit=1000)
                    stop = str(snapshot.get("stop_loss_order_id"))
                    protection = any(p.get("symbol") == symbol and str(p.get("algoId")) == stop
                                     and p.get("orderType", p.get("type")) in {"STOP", "STOP_MARKET"}
                                     for p in orders + algos)
                    if fills and len(fills) < 1000 and all(str(f.get("orderId")) == entry for f in fills) and protection:
                        verdict = f"三方已关联至入场单 {entry}，等级 {snapshot['grade']}；待复核场景专属条件"
                except Exception:
                    verdict = "精确订单或保护查询不可用，待三方核对"
            sample_lines.append(f"{when[5:16].replace('T', ' ')} {strategy} {symbol} {category} 候选；"
                                f"{status}，等级记录 {grade_count}，交易记录 {trade_count}；{verdict}")
        for row in summaries:
            issues.append(f"HRS {row['symbol']} 平仓汇总 id={row['id']} 采用理论盈亏 {row['realized_pnl']} USDT")
        # Read after the potentially slow exchange probe, in a new snapshot.
        async with db.transaction(isolation="repeatable_read", readonly=True):
            gate = await db.fetchrow("""
                SELECT g.account_id,g.strategy_id,g.ready,g.allowed,g.policy_revision,
                       g.as_of::text,g.valid_until::text,g.reasons,
                       clock_timestamp()::text AS checked_at,a.revision AS active_revision
                FROM trading.strategy_equity_gate g
                LEFT JOIN trading.strategy_risk_active a USING(account_id,strategy_id)
                WHERE g.account_id='default' AND g.strategy_id='hrs'
            """)
        return {
            "issues": issues, "samples": sample_lines,
            "positions": visible_positions, "reservations": len(reservations),
            "policies": [f"{r['strategy_id']} r{r['revision']} {r['mode']} "
                         f"{r['expires_at'].astimezone(BEIJING):%m-%d %H:%M}" for r in policies],
            "allocation_hash": allocation_hash, "monthly": monthly,
            "equity_gate": dict(gate) if gate else None,
        }
    finally:
        if client is not None:
            await client.close()
        await db.close()


def send_report(path, report):
    url = path.read_text().strip()
    if not re.fullmatch(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-fA-F-]{36}", url):
        raise ValueError("invalid Feishu webhook")
    body = json.dumps({"msg_type": "text", "content": {"text": report}}, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Feishu HTTP {exc.code}") from None
    except (OSError, ValueError):
        raise RuntimeError("Feishu transport or response error") from None
    code = payload.get("code", payload.get("StatusCode")) if isinstance(payload, dict) else None
    if type(code) is not int or code != 0:
        status = code if type(code) is int else "unknown"
        raise RuntimeError(f"Feishu rejected report (code={status})")
    print("Feishu accepted report (code=0)")


def entry_gate_status(gate, now):
    """Validate current authorization without interpreting a missing row as approval."""
    try:
        if (not isinstance(gate, dict) or gate.get("account_id") != "default"
                or gate.get("strategy_id") != "hrs"
                or type(gate.get("ready")) is not bool or type(gate.get("allowed")) is not bool
                or (gate["allowed"] and not gate["ready"])
                or type(gate.get("policy_revision")) is not int or gate["policy_revision"] <= 0
                or type(gate.get("active_revision")) is not int
                or gate["active_revision"] != gate["policy_revision"]):
            return "UNVERIFIED"
        as_of, checked, valid = (datetime.fromisoformat(gate[key])
                                for key in ("as_of", "checked_at", "valid_until"))
        if not (as_of <= checked <= now and now - as_of <= timedelta(seconds=120)
                and as_of <= valid <= as_of + timedelta(seconds=120)):
            return "UNVERIFIED"
        if gate["allowed"] and now >= valid:
            return "UNVERIFIED"
        return "ALLOWED" if gate["allowed"] else "PAUSED"
    except (KeyError, TypeError, ValueError):
        return "UNVERIFIED"


def hrs_entry_gate_lines(gate, now):
    status = entry_gate_status(gate, now)
    if status == "UNVERIFIED":
        return ["HRS 权益门禁：门禁证据未核实；不能确认开仓授权。"]
    if status == "ALLOWED":
        return ["HRS 权益门禁：允许新增风险；实际开仓仍须通过评分、预算、保证金及保护单检查。"]
    reasons = gate.get("reasons")
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except ValueError:
            reasons = None
    detail = "；".join(reasons) if isinstance(reasons, list) and reasons and all(
        isinstance(item, str) for item in reasons) else "原因待核实"
    return ["HRS 权益门禁：暂停新增风险；" + detail + "。"]


def report_conclusion(issue_count, candidate_count, monthly, shadow_status, entry_status=None):
    findings = [f"{issue_count} 项异常待核对"] if issue_count else ["本次门禁与风控核验通过"]
    if entry_status in {"PAUSED", "UNVERIFIED"}:
        if not issue_count:
            findings = []
        findings.append("HRS 权益门禁暂停新增风险" if entry_status == "PAUSED"
                        else "HRS 权益门禁证据未核实")
    if candidate_count:
        findings.append(f"{candidate_count} 个新增实盘候选待核验")
    allocation = "月度分配有核验结果，见下文" if monthly else "月度分配记录无变化"
    shadow = {
        "BLOCKED_DATA": "全账户自动分配预览受阻",
        "PREVIEW": "全账户自动分配预览已生成",
    }.get(shadow_status, "全账户自动分配预览状态待确认")
    return f"【结论】{'，'.join(findings)}。{allocation}，{shadow}。"


def allocation_shadow_lines(shadow):
    """Expose the original allocation scope/window; never certify HRS returns."""
    if not isinstance(shadow, dict):
        return ["全账户自动分配预览：状态待确认；HRS 收益验收另行核验。"]
    status = shadow.get("status")
    status = status if status in {"BLOCKED_DATA", "PREVIEW"} else "状态待确认"
    window = "窗口未核实"
    try:
        left, right = shadow["start_ms"], shadow["end_ms"]
        if type(left) is not int or type(right) is not int or not 0 <= left < right:
            raise ValueError("invalid allocation window")
        start = datetime.fromtimestamp(left / 1000, BEIJING)
        end = datetime.fromtimestamp(right / 1000, BEIJING)
        window = f"{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}（北京时间，右端不含）"
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        pass
    lines = [f"全账户自动分配预览：{status}；收入窗口：{window}。"]
    reasons = shadow.get("reasons")
    if isinstance(reasons, list) and all(isinstance(item, str) for item in reasons):
        if reasons:
            lines.append("分配预览缺项：" + "；".join(reasons) + "。")
        elif status == "BLOCKED_DATA":
            lines.append("分配预览缺项原因未提供，仍保留 BLOCKED_DATA。")
    elif status == "BLOCKED_DATA":
        lines.append("分配预览缺项格式未核实，仍保留 BLOCKED_DATA。")
    blocks = shadow.get("activation_blocks")
    if isinstance(blocks, list) and blocks and all(isinstance(item, str) for item in blocks):
        lines.append("实盘分配保持冻结；独立启用条件：" + "；".join(blocks) + "。")
    lines.append("HRS 收益验收另行核验；本段只报告全账户分配预览证据。")
    return lines


def publish_or_send(webhook, report, *, status, summary):
    """写入 07:35 汇总 outbox；outbox 不可用时回退直发，避免报告丢失。"""
    if digest_outbox is not None:
        try:
            digest_outbox.publish("hrs", report, status=status, summary=summary)
            return
        except (OSError, ValueError):
            pass
    send_report(webhook, report)


def run_daily(args):
    now = datetime.now(BEIJING)
    state = json.loads(args.state.read_text()) if args.state.exists() else {}
    since = max(START, datetime.fromisoformat(state["lookback"]) if "lookback" in state
                else now - timedelta(hours=48))
    samples, allocation_logs, unavailable, reconciliations, hrs_journal = read_journal(
        since, previous_trading_day(now)
    )
    reconciliation_line, reconciliation_warning = summarize_hrs_reconciliation(
        now, reconciliations, journal_has_entries=hrs_journal
    )
    shadow = read_shadow()
    inputs = {"since": since.isoformat(), "samples": samples, "shadow": shadow,
              "allocation_hash": state.get("allocation_hash")}
    result = subprocess.run(
        ["docker", "exec", "-i", "-w", "/app", CONTAINERS["hrs"],
         "python", "-c", Path(__file__).read_text(), "--probe"],
        text=True, capture_output=True, input=json.dumps(inputs), timeout=180,
    )
    if result.returncode:
        raise RuntimeError(f"read-only probe failed (exit={result.returncode})")
    data = json.loads(result.stdout.splitlines()[-1])
    issues = data["issues"] + ([f"本窗口日志未取到：{', '.join(unavailable)}"] if unavailable else [])
    if reconciliation_warning:
        issues.append("上一交易日主动对账有待重试、扫描失败或日志缺失，见对账小节")
    lines = [f"【HRS 生产只读巡检】{now:%Y-%m-%d %H:%M} 北京时间",
             f"交易所实仓 {len(data['positions'])} 个，未释放预留 {data['reservations']} 条。",
             reconciliation_line,
             "风控：" + "；".join(data["policies"]),
             data["monthly"] or "月度分配记录无变化。"]
    if shadow:
        shadow_signature = digest({k: v for k, v in shadow.items() if k != "as_of"})
        lines.extend(allocation_shadow_lines(shadow))
    else:
        shadow_signature = None
        issues.append("未读到 AI Tuner 月度影子核验证据")
    evidence_now = datetime.now(BEIJING)
    entry_status = entry_gate_status(data.get("equity_gate"), evidence_now)
    lines.extend(hrs_entry_gate_lines(data.get("equity_gate"), evidence_now))
    lines.extend(hrs_evidence_lines(read_hrs_evidence(), evidence_now))
    last_log = allocation_logs[-1] if allocation_logs else None
    if last_log and list(last_log) != state.get("allocation_log"):
        lines.append(f"月度作业日志 {last_log[0][5:16]} {last_log[1]}")
    issue_keys = {digest(item) for item in issues}
    seen = set(state.get("issues", []))
    additions = [issue for issue in issues if digest(issue) not in seen]
    if additions:
        lines.extend(f"新增异常：{item}" for item in additions[:12])
    if issues and len(issues) > len(additions):
        lines.extend(f"持续异常：{issue}" for issue in issues if digest(issue) in seen)
    previous_samples = set(state.get("samples", []))
    new_samples = [sample for sample in data["samples"] if digest(sample) not in previous_samples]
    lines.extend(new_samples[:8] or ["未发现新增指定场景候选。"])
    lines.insert(1, report_conclusion(
        len(issues), len(new_samples), data["monthly"], shadow.get("status") if shadow else None,
        entry_status
    ))
    report = "\n".join(lines)
    print(report)
    if args.dry_run:
        return
    if getattr(args, "send", False):
        send_report(args.webhook, report)
    else:
        publish_or_send(args.webhook, report,
                        status="warn" if issues or entry_status != "ALLOWED" else "ok",
                        summary=(f"{len(issues)} 项异常" if issues else "正常" if entry_status == "ALLOWED"
                                 else "HRS 权益门禁暂停" if entry_status == "PAUSED"
                                 else "HRS 权益门禁证据未核实"))
    next_state = {
        "lookback": (now - timedelta(minutes=10)).isoformat(),
        "allocation_hash": data["allocation_hash"], "shadow_hash": shadow_signature,
        "allocation_log": last_log or state.get("allocation_log"),
        "issues": sorted(issue_keys),
        "samples": sorted(previous_samples.union(map(digest, data["samples"])))[-2000:],
    }
    temporary = args.state.with_suffix(".tmp")
    temporary.write_text(json.dumps(next_state, sort_keys=True))
    temporary.replace(args.state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true", help="直接发送飞书，默认只写入 07:35 汇总 outbox")
    parser.add_argument("--webhook", type=Path,
                        default=Path("/root/trading_system/.secrets/hrs_acceptance_webhook"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/trading-hrs-acceptance/state.json"))
    args = parser.parse_args()
    if args.probe:
        try:
            print(json.dumps(asyncio.run(asyncio.wait_for(probe(json.load(sys.stdin)), 145))))
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__}))
            raise SystemExit(1) from None
    else:
        try:
            run_daily(args)
        except Exception as exc:
            failure = re.fullmatch(r"read-only probe failed \(exit=\d+\)", str(exc))
            if failure and not args.dry_run:
                message = "【HRS 生产只读巡检】\n【结论】本次证据读取失败，验收未完成，请检查服务器服务日志。"
                if getattr(args, "send", False):
                    send_report(args.webhook, message)
                else:
                    publish_or_send(args.webhook, message, status="error", summary="证据读取失败")
            raise


if __name__ == "__main__":
    main()
