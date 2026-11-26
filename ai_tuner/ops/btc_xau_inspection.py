"""向飞书发送 BTC–XAU 影子实验的每日只读巡检。"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.request import Request, urlopen

try:
    from ai_tuner.ops import digest_outbox
except ImportError:  # 以脚本方式直接运行时，当前目录即 ai_tuner/ops
    import digest_outbox

STATE = Path("/var/lib/btc-xau-relative")
RULES = Path("/opt/btc-xau-relative/v1/rules.json")
HOOK = Path("/root/trading_system/.secrets/btc_xau_inspection_webhook")
CST = timezone(timedelta(hours=8))


def local_time(ts):
    return datetime.fromtimestamp(ts, CST).strftime("%m-%d %H:%M") if ts else "暂无"


def coverage(value, target):
    if value is None:
        return f"暂无到期样本，目标 {target:.0%}"
    gap = max(0, target - value)
    return f"{value:.1%} / {target:.0%}，差 {gap * 100:.1f} 个百分点"


def collection(state, rules, now):
    timer = subprocess.run(
        ["systemctl", "is-active", "btc-xau-relative.timer"],
        capture_output=True, text=True, timeout=5,
    )
    if timer.returncode:
        return "异常，采集定时器未运行"
    age = now - (state / "report.json").stat().st_mtime
    end = datetime.fromisoformat(rules["start"]).timestamp() + rules["weeks"] * 7 * 86400
    if now >= end and age <= 3600:
        return "观察窗口已结束，采集服务报告仍在更新"
    with sqlite3.connect(f"{(state / 'shadow.sqlite').as_uri()}?mode=ro", uri=True, timeout=3) as db:
        probe = db.execute("SELECT v FROM meta WHERE k='startup_probe'").fetchone()
        start = json.loads(probe[0])["ts"] if probe else None
        stats = db.execute("SELECT min(slot),max(observed) FROM observations").fetchone()
        first = start if start is not None else stats[0]
        previous = int(now - rules["entry_grace"]) // 3600 * 3600
        eligible = []
        if first is not None:
            for slot in range(previous - 23 * 3600, previous + 1, 3600):
                d = datetime.fromtimestamp(slot, timezone.utc)
                if slot + rules["entry_grace"] >= first and not (d.weekday() == 5 or (d.weekday() == 6 and d.hour < 20)):
                    eligible.append(slot)
        seen = {row[0] for row in db.execute("SELECT slot FROM observations WHERE slot>=?", (previous - 23 * 3600,))}
        errors = db.execute("SELECT count(*) FROM incidents WHERE ts>?", (now - 86400,)).fetchone()[0]
    if age > 3600:
        label = "异常，报告超过 1 小时未更新"
    elif eligible and eligible[-1] not in seen:
        label = "异常，最近一个预期小时缺报价"
    elif eligible and len(seen.intersection(eligible)) / len(eligible) < rules["min_spot_coverage"]:
        label = "异常，近 24 小时有效报价覆盖偏低"
    elif eligible:
        label = "正常"
    elif probe:
        label = "定时器正常，已有启动报价，等待有效小时"
    else:
        label = "待核验，暂无有效报价"
    return (f"{label}｜最近有效报价 {local_time(stats[1])}｜"
            f"近24h有效 {len(seen.intersection(eligible))}/{len(eligible)}｜重试/异常 {errors} 次")


def next_step(report, health, now, start, end, pending, errors, max_error_rate):
    collection_status = health.split("｜", 1)[0]
    if collection_status.startswith("异常，"):
        reason = collection_status.removeprefix("异常，")
        return f"检查采集服务和日志（{reason}），恢复后复核报价覆盖。"
    if collection_status.startswith("待核验"):
        return "检查采集日志和首笔有效报价。"
    if now >= end:
        if report["status"] == "观察中":
            return "先刷新窗口结束后的报告，再按最终数据验收。"
        if pending:
            return f"核对未结周期（{pending} 笔）的平仓和资金费，处理后复核验收。"
        return {
            "证据不足": "复核覆盖、执行和样本缺口，再决定是否另立扩窗实验。",
            "未通过研究门槛": "复盘未达标项，维持影子研究。",
            "通过研究门槛": "归档验收并安排人工复核，维持影子运行。",
        }[report["status"]]
    if report["late_exit"] or errors > report["expected_slots"] * max_error_rate:
        return "核对延迟退出或异常决策对应时段的日志与账本。"
    if now < start:
        return f"维持预热，{local_time(start)} 正式开始后复核首批采集与决策。"
    # 冻结方案规定第 1、4 周只复核数据质量、方向和样本数。
    for week in (1, 4):
        checkpoint = start + week * 7 * 86400
        if now <= checkpoint < end:
            action = "复核采集、方向分布和样本数。"
            break
    else:
        checkpoint = end
        action = "窗口结束后做最终验收。"
    checkpoint_local = datetime.fromtimestamp(checkpoint, CST)
    if checkpoint_local.date() == datetime.fromtimestamp(now, CST).date():
        return f"继续影子观察，今天 {checkpoint_local:%H:%M} {action}"
    return f"继续影子观察，今天无需操作。{local_time(checkpoint)} {action}"


def format_report(report, rules, health, now, test=False):
    start = datetime.fromisoformat(report["start"]).timestamp()
    end = datetime.fromisoformat(report["end"]).timestamp()
    pnl = report["net_usdt"]
    best = max(pnl["btc_only"], pnl["gold_only"])
    edge = pnl["relative"] - best
    stressed = report["stressed_net_usdt"]["relative"]
    dd = report["closed_equity_drawdown"]["relative"]
    weeks = list(report["weekly_net"].values())
    wins = sum(w[0] > max(w[1:]) for w in weeks)
    target_wins = rules["weeks"] // 2 + 1
    errors = sum(report["decisions"].get(k, 0) for k in ("expired", "data_error"))
    max_error_rate = 1 - rules["coverage"]
    pending = report["opened"] - report["settled"]
    remaining = max(0, end - max(now, start))
    settled = report["settled"]
    title = "BTC–XAU 影子巡检" + (" · 部署验证" if test else "")
    def positive(value):
        return f"{value:+.4f} USDT（已达标）" if value > 0 else f"{value:+.4f} USDT（还需改善超过 {abs(value):.4f}）"
    def count(value, target):
        return f"{value}/{target}，差 {max(0, target - value)}"
    return "\n".join([
        title,
        f"北京时间 {local_time(now)}｜{report['status']}｜{'预热' if now < start else '正式窗口'}",
        f"下一步｜{next_step(report, health, now, start, end, pending, errors, max_error_rate)}",
        f"采集 {health}",
        f"累计已结净收益 USDT｜双腿 {pnl['relative']:+.4f}｜仅 BTC {pnl['btc_only']:+.4f}｜仅黄金 {pnl['gold_only']:+.4f}",
        *(["目前没有已结周期，以下收益指标等待样本。"] if not settled else []),
        f"离验收｜观察至 {datetime.fromtimestamp(end, CST):%Y-%m-%d %H:%M}，观察时长还差 {remaining / 86400:.1f} 天",
        f"决策覆盖 {coverage(report['coverage'], rules['coverage'])}｜行情覆盖 {coverage(report['spot_coverage'], rules['min_spot_coverage'])}",
        f"执行｜待结 {pending}，延迟退出 {report['late_exit']}（最终均须为 0）；异常决策 {errors}/{report['expected_slots']}，占比须 ≤{max_error_rate:.0%}",
        f"样本｜已结 {count(settled,rules['min_trades'])}｜多BTC {count(report['long_btc'],rules['min_each_side'])}｜空BTC {count(report['short_btc'],rules['min_each_side'])}",
        f"周度｜活跃周 {count(len(weeks),rules['min_active_weeks'])}｜双腿胜两对照 {count(wins,target_wins)}",
        *( [f"净收益须 >0，当前 {positive(pnl['relative'])}",
            f"领先最佳对照须 >0，当前 {positive(edge)}",
            f"成本加倍后须 >0，当前 {positive(stressed)}",
            f"已平仓权益回撤 {dd:.2%} / 上限 {rules['max_drawdown']:.0%}，超出 {max(0, dd-rules['max_drawdown'])*100:.2f} 个百分点"]
           if settled else [f"收益验收｜双腿净收益、相对最佳对照的领先额、成本加倍后净收益均须 >0，已平仓权益回撤须 ≤{rules['max_drawdown']:.0%}。"]),
    ])


def report_status(message):
    """从正文结论推导 outbox 状态与首部短语。"""
    if "采集 异常" in message:
        return "error", "采集异常"
    if "采集 待核验" in message:
        return "warn", "待核验"
    if "异常，" in message:
        return "error", "采集异常"
    return "ok", "正常"


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--test-send", action="store_true")
    parser.add_argument("--send", action="store_true", help="直接发送飞书，默认只写入 07:35 汇总 outbox")
    args = parser.parse_args()
    try:
        rules = json.loads(RULES.read_text())
        report = json.loads((STATE / "report.json").read_text())
        now = time.time()
        health = collection(STATE, rules, now)
        message = format_report(report, rules, health, now, args.test_send)
        status, summary = report_status(message)
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        message = (f"BTC–XAU 影子巡检｜异常\n{local_time(time.time())} 无法核对实验报告（{type(exc).__name__}）"
                   "\n下一步｜检查采集服务日志和影子账本，确认报告能正常更新。")
        status, summary = "error", "报告读取失败"
    if args.dry_run:
        print(message)
        return
    if not (args.send or args.test_send):
        try:
            digest_outbox.publish("btc_xau", message, status=status, summary=summary)
            return
        except (OSError, ValueError) as exc:
            print(f"写入 outbox 失败，回退直发: {type(exc).__name__}")
    payload = json.dumps({"msg_type": "text", "content": {"text": message}}).encode()
    try:
        req = Request(HOOK.read_text().strip(), data=payload, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=12) as response:
            result = json.load(response)
        code = result.get("code", result.get("StatusCode"))
        if code != 0:
            raise RuntimeError("飞书拒绝消息")
    except Exception as exc:
        raise SystemExit(f"飞书发送失败（{type(exc).__name__}）") from None
    print("BTC–XAU 巡检发送成功，飞书返回 code=0。")


if __name__ == "__main__":
    main()
