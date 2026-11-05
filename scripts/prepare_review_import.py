"""Validate an operator-edited review workbook and prepare an immutable import batch.

This runs locally and only reads the XLSX; it never connects to the trading DB.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path


STRATEGIES = {"hrs", "btc_eth", "btc_eth_aggressive", "new_coin", "grid"}
HEADERS = (
    "收入类型", "流水号", "时间（北京时间）", "币种", "金额 USDT", "未归属原因",
    "成交号", "订单号", "建议策略", "建议依据", "证据索引",
    "审核决定", "确认策略", "审核人", "证据备注",
    "账户", "UTC 时间", "资产", "身份校验 SHA256",
)
BEIJING = timezone(timedelta(hours=8))


def _text(value):
    return "" if value is None else str(value).strip()


def _local_time(utc_text):
    dt = datetime.fromisoformat(utc_text.replace("Z", "+00:00")).astimezone(BEIJING)
    return f"{dt.year}/{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def validate_review_row(cells, source):
    """Return a confirmed row or None; reject every changed exchange identity field."""
    if len(cells) != len(HEADERS):
        raise ValueError("表格列数不符")
    checks = (
        (0, "income_type", "收入类型"), (1, "tran_id", "流水号"),
        (3, "symbol", "币种"), (5, "unattributed_reason", "未归属原因"),
        (6, "trade_id", "成交号"), (7, "order_id", "订单号"),
        (8, "suggested_strategy", "建议策略"), (9, "suggestion_basis", "建议依据"),
        (10, "evidence_ref", "证据索引"), (15, "account_id", "账户"),
        (17, "asset", "资产"),
        (18, "identity_sha256", "身份校验"),
    )
    for index, key, name in checks:
        if _text(cells[index]) != _text(source.get(key)):
            raise ValueError(f"{name}与导出源不一致")
    if _text(cells[16]) != "UTC " + source["event_time_utc"]:
        raise ValueError("UTC 时间与导出源不一致")
    if _text(cells[2]) != _local_time(source["event_time_utc"]):
        raise ValueError("北京时间与导出源不一致")
    try:
        if Decimal(str(cells[4])) != Decimal(str(source["income_usdt"])):
            raise ValueError("金额与导出源不一致")
    except (InvalidOperation, TypeError):
        raise ValueError("金额与导出源不一致") from None
    for index, name in ((1, "流水号"), (6, "成交号"), (7, "订单号")):
        if cells[index] is not None and not isinstance(cells[index], str):
            raise ValueError(f"{name}必须保留为文本，防止长整数失真")

    decision = _text(cells[11])
    strategy, reviewer, note = (_text(cells[12]), _text(cells[13]), _text(cells[14]))
    if decision in {"", "跳过"}:
        if strategy or reviewer or note:
            raise ValueError("未确认行不应填写策略、审核人或证据备注")
        return None
    if decision != "确认":
        raise ValueError("审核决定只能是确认、跳过或留空")
    if strategy not in STRATEGIES:
        raise ValueError("确认策略不是已知策略代码")
    if not reviewer:
        raise ValueError("确认行缺少审核人")
    if len(note) < 8:
        raise ValueError("确认行需要可回查的证据备注（至少 8 字）")
    return {key: source[key] for key in (
        "account_id", "income_type", "tran_id", "symbol", "income_usdt", "asset",
        "event_time_utc", "trade_id", "order_id", "identity_sha256",
    )} | {
        "strategy": strategy, "reviewed_by": reviewer, "evidence_note": note,
        "suggested_strategy": source.get("suggested_strategy", ""),
        "suggestion_basis": source.get("suggestion_basis", ""),
        "evidence_ref": source.get("evidence_ref", ""),
    }


def prepare(workbook_path, source_path, output_path):
    from openpyxl import load_workbook

    source = json.loads(Path(source_path).read_text())
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        sheet = workbook["逐笔复核"]
        headers = tuple(cell.value for cell in sheet[5][:len(HEADERS)])
        if headers != HEADERS:
            raise ValueError("复核表列名或顺序被更改")
        entries = []
        for index, original in enumerate(source["rows"], start=6):
            cells = [sheet.cell(index, col).value for col in range(1, len(HEADERS) + 1)]
            accepted = validate_review_row(cells, original)
            if accepted:
                entries.append(accepted)
        extra = sheet.cell(6 + len(source["rows"]), 1).value
        if extra is not None:
            raise ValueError("复核表新增了未在原始快照中的数据行")
    finally:
        workbook.close()
    batch = {"account_id": source["account_id"], "month": source["month"],
             "source_report_id": source["report_id"], "source_end_ms": source["end_ms"],
             "rows": entries}
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(batch, handle, ensure_ascii=False, indent=2)
    return {"approved": len(entries), "total": len(source["rows"]), "output": output_path}


def main():
    parser = argparse.ArgumentParser(description="校验已填写的 XLSX，生成待入库 JSON；不连接数据库")
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.workbook, args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
