"""
D1 只读采集与不可变原始事实（execution_facts）

旁路采集脚本，不改动任何策略生产者行为。核心采集逻辑完全可离线测试：
API 客户端与数据下沉都通过参数注入，测试时传入假对象即可。

功能：
  - export_fills       ：从 userTrades 拉成交明细（分页 + 去重 + 游标）
  - export_income      ：从 um/income 拉收入流水（分页 + 去重 + 游标）
  - sanitize_payload   ：原始响应脱敏（剔除 apiKey / secret / signature 等）
  - compute_sha256     ：payload SHA256（幂等校验用）

CLI 定位（本轮）：旁路纯采集 + 注入式 sink，默认**不**组装真实数据库 sink，
采集结果走 stdout/JSONL 输出。真库写入由调用方显式注入 sink 负责——
这是为了避免 CLI 入口未经审批直接污染生产库。

设计要点：
  - 幂等键：fills 用 (account_id, symbol, trade_id=响应的 id)；
           income 用 (account_id, tran_id=响应的 tranId)。
  - 分页：time 递增 + limit；满页时把 start_time 推进到本页最后一条 time，
          同毫秒多笔被 limit 截断时下一页会再拉同 time 的，但 Set 去重。
  - page_complete：仅当返回条数 < limit 才判定页尾；满页 (== limit) 必须继续翻。
  - 脱敏：纯函数，递归剔除密钥/签名字段。
  - 注入：api_client 与 sink 都由外部传入，脚本本体不依赖任何真实网络/数据库。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import structlog

logger = structlog.get_logger()

# ── 常量（禁硬编码业务参数） ────────────────────────────────────────

# 脱敏字段名集合（完整字段名匹配，大小写不敏感；不做子串匹配，避免误删
# 如 "list_with_secrets" / "the_secret_marker" 这类非密钥字段名里恰好带的子串）
_SENSITIVE_KEYS_LOWER = frozenset({
    "apikey", "api_key", "apisecret", "api_secret",
    "secret", "secretkey", "secret_key",
    "signature", "sign", "auth", "authtoken",
    "authorization", "password", "token", "accesskey",
    "recvwindow",
})

# 单页默认上限（币安硬上限 1000）
_DEFAULT_PAGE_LIMIT = 1000

# 分页防无限循环：一个时间窗内最多翻多少页（安全守卫，不是业务参数）
_MAX_PAGES_GUARD = 1000

# 错误详情 / 日志行里的截断长度（避免把整个异常栈打进游标字段）
_ERROR_DETAIL_TRUNCATE = 200
_LOG_FIELD_TRUNCATE = 120

# 环境变量：币安 API 密钥（与 .env.example / docker-compose 保持一致）
_ENV_API_KEY = "BINANCE_API_KEY"
_ENV_API_SECRET = "BINANCE_API_SECRET"

# 环境变量：DB sink 写入开关（truthy 开启；默认关闭，纯 JSONL 输出）
_ENV_DB_WRITE = "EXECUTION_FACTS_DB_WRITE"


# ────────────────────────────────────────────────────────────────────
# 数据类：标准化行（脚本内部用，下游 sink 自行映射到 SQL 列）
# ────────────────────────────────────────────────────────────────────

@dataclass
class FillFact:
    """标准化成交明细行（对应 trading.execution_fact_fill）"""
    account_id: str
    symbol: str
    trade_id: int                 # 币安响应的 id（自增整数，成交唯一标识）
    order_id: Optional[int]
    side: Optional[str]
    qty: Optional[str]            # 保留字符串精度（Decimal 下游处理）
    price: Optional[str]
    quote_qty: Optional[str]
    commission: Optional[str]
    commission_asset: Optional[str]
    realized_pnl: Optional[str]
    trade_time_ms: Optional[int]  # 毫秒
    raw_payload: Dict[str, Any]    # 脱敏后原始响应
    payload_sha256: str


@dataclass
class IncomeFact:
    """标准化收入流水行（对应 trading.execution_fact_income）"""
    account_id: str
    tran_id: int                  # 币安 um/income 响应的 tranId
    symbol: str
    income_type: str
    income: Optional[str]
    asset: Optional[str]
    info: Optional[str]
    trade_id: Optional[int]
    event_time_ms: Optional[int]  # 毫秒
    raw_payload: Dict[str, Any]
    payload_sha256: str


@dataclass
class CursorRecord:
    """采集游标（对应 trading.execution_fact_cursor）

    身份键 = source + account_id + income_type + symbol + window_start_ms。
    fills 采集 income_type=None，symbol=币种；income 采集 income_type=具体类型，symbol=None。
    不同 income_type / 不同账户 / fills vs income 各自独立游标，互不覆盖。
    """
    source: str                            # 'userTrades' / 'um/income'
    account_id: str                        # 账户标识（游标身份键的一部分）
    income_type: Optional[str]             # income: 'REALIZED_PNL' 等；fills: None
    symbol: Optional[str]                  # fills: 币种; income: None
    window_start_ms: int
    window_end_ms: Optional[int] = None
    last_event_id: Optional[int] = None
    covered_until_ms: Optional[int] = None
    returned_count: int = 0
    page_complete: bool = False
    request_status: str = "ok"             # ok / error / partial / incomplete
    error_detail: Optional[str] = None
    retry_count: int = 0


@dataclass
class PageAdvance:
    """分页推进点：记录本页最大时间 + 本页最大事件 ID。"""
    time_ms: int
    event_id: Optional[int] = None


@dataclass(frozen=True)
class _PageCtx:
    """翻页上下文：身份 + 窗口 + 回调。构造一次，向下传递，消除同构 kwargs。"""
    source: str
    account_id: str
    income_type: Optional[str]
    symbol: Optional[str]
    window_start_ms: int
    window_end_ms: int
    parse_row: Callable[[Dict[str, Any]], Optional[Any]]
    dedup_key: Callable[[Any], Any]
    time_ms_of: Callable[[Any], Optional[int]]
    event_id_of: Callable[[Any], Optional[int]]
    last_event_id: Callable[[List[Any]], Optional[int]]
    api_call_factory: Callable[[int, Optional[int]], Any]
    row_time_ms: Callable[[Dict[str, Any]], Optional[int]]
    supports_id_cursor: bool = True


@dataclass
class ExportResult:
    """一次采集的返回结果（给 sink 落库 + 给调用方看）"""
    records: List[Any] = field(default_factory=list)
    page_complete: bool = False
    cursor: Optional[CursorRecord] = None
    errors: List[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# 游标工厂（消除全文 7 处重复构造）
# ────────────────────────────────────────────────────────────────────

def _make_cursor(
    *,
    source: str,
    account_id: str,
    income_type: Optional[str],
    symbol: Optional[str],
    window_start_ms: int,
    window_end_ms: Optional[int],
    covered_until_ms: Optional[int],
    request_status: str,
    returned_count: int = 0,
    page_complete: bool = False,
    last_event_id: Optional[int] = None,
    retry_count: int = 0,
    error_detail: Optional[str] = None,
) -> CursorRecord:
    """CursorRecord 工厂：把散落在多处的重复构造收敛成一处。"""
    return CursorRecord(
        source=source,
        account_id=account_id,
        income_type=income_type,
        symbol=symbol,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        last_event_id=last_event_id,
        covered_until_ms=covered_until_ms,
        returned_count=returned_count,
        page_complete=page_complete,
        request_status=request_status,
        retry_count=retry_count,
        error_detail=error_detail,
    )


# ────────────────────────────────────────────────────────────────────
# 纯函数工具：脱敏 + sha256
# ────────────────────────────────────────────────────────────────────

def is_sensitive_key(key: str) -> bool:
    """判断字段名是否敏感（保守策略：驼峰/下划线归一后任何片段命中敏感词即 True）。

    归一化策略：
      - 原样 lower + 去下划线：api_key / secretKey → apikey / secretkey → 集合查一次
      - 驼峰拆片（连续大写开头小写结尾），任意相邻片段拼接 → 集合查一次
        例如 mySecretKey → ["my", "Secret", "Key"]
                       → "my"="my"（否）→ "Secret"="secret"（是，返回 True）
      - 注意：驼峰拆片后**单片段**也会查集合，这是保守剔除策略。
        因此 the_secret_marker → 单片段 "secret" 命中 → True；
        token_count → 单片段 "token" 命中 → True。
        代价是可能过度剔除非密钥业务字段（如含 "token" 命名的计数器），
        但安全侧允许宁错杀不放过——敏感字段宁多删不遗漏。

    不做任意子串匹配（如 "secrets" 复数不在敏感词集合就不会命中）。
    """
    lowered = key.lower()
    # 1) 直接查集合（lower + 去下划线两个版本）
    if lowered in _SENSITIVE_KEYS_LOWER:
        return True
    collapsed = lowered.replace("_", "")
    if collapsed in _SENSITIVE_KEYS_LOWER:
        return True

    # 2) 驼峰拆片后，相邻片段拼接 → 集合查一次
    #    拆分规则：大写字母紧跟小写字母的位置（secretKey → secret + Key）
    camel_parts = re.findall(r"[a-z]+|[A-Z][a-z]*", key)
    camel_lower_parts = [p.lower() for p in camel_parts]
    n = len(camel_lower_parts)
    for lo in range(n):
        for hi in range(lo + 1, n + 1):
            merged = "".join(camel_lower_parts[lo:hi])
            if merged in _SENSITIVE_KEYS_LOWER:
                return True
    return False


def sanitize_payload(raw: Any) -> Any:
    """递归剔除敏感字段。纯函数，方便测试直接调用。"""
    if isinstance(raw, dict):
        result: Dict[str, Any] = {}
        for k, v in raw.items():
            if is_sensitive_key(k):
                continue  # 敏感 key 直接丢弃
            result[k] = sanitize_payload(v)
        return result
    if isinstance(raw, list):
        return [sanitize_payload(item) for item in raw]
    return raw


def compute_sha256(payload: Dict[str, Any]) -> str:
    """对脱敏后的 payload 计算 SHA256（sort_keys 保证稳定）。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────
# 响应 → 标准化行
# ────────────────────────────────────────────────────────────────────

def _parse_fill_row(account_id: str, row: Dict[str, Any]) -> Optional[FillFact]:
    """把一条 userTrades 响应标准化成 FillFact。
    缺失关键字段（id 或 symbol）时返回 None 表示跳过。
    """
    trade_id_raw = row.get("id")
    symbol = row.get("symbol")
    if trade_id_raw is None or symbol is None:
        logger.warning(
            "userTrades 响应缺失关键字段，跳过",
            missing=("id" if trade_id_raw is None else "symbol"),
            row=row,
        )
        return None

    sanitized = sanitize_payload(row)
    return FillFact(
        account_id=account_id,
        symbol=str(symbol),
        trade_id=int(trade_id_raw),
        order_id=_safe_int(row.get("orderId")),
        side=_safe_str(row.get("side")),
        qty=_safe_str(row.get("qty")),
        price=_safe_str(row.get("price")),
        quote_qty=_safe_str(row.get("quoteQty")),
        commission=_safe_str(row.get("commission")),
        commission_asset=_safe_str(row.get("commissionAsset")),
        realized_pnl=_safe_str(row.get("realizedPnl")),
        trade_time_ms=_safe_int(row.get("time")),
        raw_payload=sanitized,
        payload_sha256=compute_sha256(sanitized),
    )


def _parse_income_row(account_id: str, row: Dict[str, Any]) -> Optional[IncomeFact]:
    """把一条 um/income 响应标准化成 IncomeFact。缺失关键字段时跳过。"""
    tran_id_raw = row.get("tranId")
    if tran_id_raw is None:
        logger.warning(
            "um/income 响应缺失 tranId，跳过", row=row,
        )
        return None

    sanitized = sanitize_payload(row)
    return IncomeFact(
        account_id=account_id,
        tran_id=int(tran_id_raw),
        symbol=str(row.get("symbol") or "UNKNOWN"),
        income_type=str(row.get("incomeType") or "UNKNOWN"),
        income=_safe_str(row.get("income")),
        asset=_safe_str(row.get("asset")),
        info=_safe_str(row.get("info")),
        trade_id=_safe_int(row.get("tradeId")),
        event_time_ms=_safe_int(row.get("time")),
        raw_payload=sanitized,
        payload_sha256=compute_sha256(sanitized),
    )


def _safe_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _row_time_ms(row: Dict[str, Any]) -> Optional[int]:
    """从原始 API 行取出毫秒级时间戳（模块级复用，供 fills / income 共用）。"""
    return _safe_int(row.get("time"))


def _safe_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    return str(val)


# ────────────────────────────────────────────────────────────────────
# 核心导出器（注入 api_client + sink）
# ────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionFactExporter:
    """旁路只读采集器。

    所有 I/O 都通过注入的 api_client / sink 完成；构造函数不联网。
    分页、去重、游标、脱敏逻辑全部在实例方法里——测试直接 new 本类
    传假对象就能全离线覆盖。

    income 采集要求统一账户客户端。若客户端显式声明
    use_unified_account=False，export_income 在读取或写入游标前报错，
    避免把“不支持的账户模式”记成成功采集的空结果。
    """
    account_id: str                              # 账户标识（下游复合键的一部分）
    api_client: Any                              # 需具备 async get_user_trades / get_income_history
    sink: Any = None                             # 可选；需具备 async upsert_fills/upsert_income/save_cursor
    page_limit: int = _DEFAULT_PAGE_LIMIT         # 单页上限；业务参数（默认 1000）
    max_pages: int = _MAX_PAGES_GUARD            # 防无限循环守卫
    _cursor_store: Dict[Tuple[str, str, Optional[str], Optional[str], int], CursorRecord] = field(
        default_factory=dict
    )  # 内存游标存储（测试用；真实 sink 提供 save_cursor / load_cursor 时会优先用它）

    # ── 分页消费通用函数（抽去 _consume_fill_page / _consume_income_page 的同构代码）

    def _consume_page(
        self,
        rows: List[Dict[str, Any]],
        *,
        parse_row: Callable[[Dict[str, Any]], Optional[Any]],
        dedup_key: Callable[[Any], Any],
        time_ms_of: Callable[[Any], Optional[int]],
        event_id_of: Callable[[Any], Optional[int]],
        seen: Set[Any],
        prev_advance: Optional[PageAdvance] = None,
    ) -> Tuple[List[Any], PageAdvance]:
        """通用一页消费：标准化 → Set 去重 → 求本页最大时间 + 本页最大事件 ID。

        Args:
            rows: API 返回的原始字典列表
            parse_row: 单条标准化函数（返回 None 表示跳过）
            dedup_key: 从标准行提取去重键（如 (account_id, symbol, trade_id)）
            time_ms_of: 从标准行提取时间毫秒（None 表示跳过）
            event_id_of: 从标准行提取事件 ID（fills 用 trade_id；income 用 tran_id）
            seen: 调用方维护的去重集合（本函数原地更新）
            prev_advance: 调用方当前持有的 PageAdvance。当本页没有有效行时，
                返回值保持该原值，避免把时间戳重置成 0 导致 while 循环误判收尾。
        Returns:
            (本页新增的标准化行列表, PageAdvance(本页及之前最大 time + event_id))
        """
        prev_time = prev_advance.time_ms if prev_advance else 0
        prev_event = prev_advance.event_id if prev_advance else None
        new_items: List[Any] = []
        max_time = prev_time
        max_event = prev_event
        for row in rows or []:
            fact = parse_row(row)
            if fact is None:
                continue
            key = dedup_key(fact)
            if key in seen:
                continue
            seen.add(key)
            new_items.append(fact)
            t = time_ms_of(fact)
            if t is not None and t > max_time:
                max_time = t
            eid = event_id_of(fact)
            if eid is not None and (max_event is None or eid > max_event):
                max_event = eid
        return new_items, PageAdvance(time_ms=max_time, event_id=max_event)

    # ── 公共入口：fills ────────────────────────────────────────────

    async def export_fills(
        self,
        symbol: str,
        window_start_ms: int,
        window_end_ms: int,
    ) -> ExportResult:
        """拉取某 symbol 在给定时间窗内的全部成交明细。"""
        source = "userTrades"
        seen: Set[Tuple[str, str, int]] = set()
        collected: List[FillFact] = []
        result = ExportResult()

        cursor = await self._load_cursor(source, None, symbol, window_start_ms)
        cur_start = cursor.covered_until_ms if cursor and cursor.covered_until_ms else window_start_ms

        ctx = _PageCtx(
            source=source, account_id=self.account_id,
            income_type=None, symbol=symbol,
            window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            parse_row=lambda row: _parse_fill_row(self.account_id, row),
            dedup_key=lambda f: (f.account_id, f.symbol, f.trade_id),
            time_ms_of=lambda f: f.trade_time_ms,
            event_id_of=lambda f: f.trade_id,
            last_event_id=lambda items: _last_trade_id(items),
            api_call_factory=lambda cs, fid: self.api_client.get_user_trades(
                symbol,
                start_time=cs if fid is None else None,
                end_time=window_end_ms if fid is None else None,
                limit=self.page_limit, from_id=fid,
            ),
            row_time_ms=_row_time_ms,
            supports_id_cursor=True,
        )
        result.cursor, result.errors = await self._run_export_loop(
            ctx=ctx, cur_start=cur_start, seen=seen, collected=collected,
            sink_upsert=getattr(self.sink, "upsert_fills", None),
        )
        result.records = collected
        result.page_complete = result.cursor.page_complete
        return result

    async def export_income(
        self,
        window_start_ms: int,
        window_end_ms: int,
        income_type: Optional[str] = None,
    ) -> ExportResult:
        """拉取给定时间窗内的全部收入流水。"""
        if getattr(self.api_client, "use_unified_account", True) is False:
            raise ValueError("income 采集要求统一账户客户端（use_unified_account=True）")
        source = "um/income"
        income_type_arg = income_type or "REALIZED_PNL"
        seen: Set[Tuple[str, int]] = set()
        collected: List[IncomeFact] = []
        result = ExportResult()

        cursor = await self._load_cursor(source, income_type_arg, None, window_start_ms)
        cur_start = cursor.covered_until_ms if cursor and cursor.covered_until_ms else window_start_ms

        ctx = _PageCtx(
            source=source, account_id=self.account_id,
            income_type=income_type_arg, symbol=None,
            window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            parse_row=lambda row: _parse_income_row(self.account_id, row),
            dedup_key=lambda f: (f.account_id, f.tran_id),
            time_ms_of=lambda f: f.event_time_ms,
            event_id_of=lambda f: f.tran_id,
            last_event_id=lambda items: _last_tran_id(items),
            api_call_factory=lambda cs, _fid: self.api_client.get_income_history(
                start_time=cs, end_time=window_end_ms,
                limit=self.page_limit, income_type=income_type_arg,
            ),
            row_time_ms=_row_time_ms,
            supports_id_cursor=False,
        )
        result.cursor, result.errors = await self._run_export_loop(
            ctx=ctx, cur_start=cur_start, seen=seen, collected=collected,
            sink_upsert=getattr(self.sink, "upsert_income", None),
        )
        result.records = collected
        result.page_complete = result.cursor.page_complete
        return result

    # ── while 循环内部辅助：三块游标构造（各十几行，消除主循环的重复） ─

    def _ok_cursor(
        self, *,
        source: str, account_id: str, income_type: Optional[str], symbol: Optional[str],
        window_start_ms: int, window_end_ms: int,
        covered_until_ms: int,
        returned_count: int,
        last_event_id: Optional[int],
    ) -> CursorRecord:
        """本页到窗口尾（page_complete=TRUE）的游标。"""
        return _make_cursor(
            source=source, account_id=account_id, income_type=income_type,
            symbol=symbol, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            covered_until_ms=covered_until_ms,
            returned_count=returned_count,
            page_complete=True, request_status="ok",
            last_event_id=last_event_id,
        )

    def _max_pages_guard_cursor(
        self, *,
        source: str, account_id: str, income_type: Optional[str], symbol: Optional[str],
        window_start_ms: int, window_end_ms: int,
        covered_until_ms: int,
        returned_count: int,
        last_event_id: Optional[int] = None,
    ) -> CursorRecord:
        """max_pages 强制退出（守卫）的游标：page_complete=False + request_status='partial'。

        保留 last_event_id 供后续断点恢复（续传只发 fromId，不传时间参数）。
        """
        return _make_cursor(
            source=source, account_id=account_id, income_type=income_type,
            symbol=symbol, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            covered_until_ms=covered_until_ms,
            returned_count=returned_count,
            page_complete=False, request_status="partial",
            last_event_id=last_event_id,
            error_detail="翻页超过 max_pages 守卫",
        )

    # ── while 循环核心（fills / income 共用） ─────────────────────

    async def _run_paging_until_done(
        self, *,
        ctx: _PageCtx,
        cur_start: int, cur_event_id: Optional[int],
        seen: Set[Any], collected: List[Any],
    ) -> Tuple[CursorRecord, List[str]]:
        """while 翻页循环（含 max_pages 守卫 + 不可推进检测），返回 (cursor, errors)。

        循环条件仅保留 page_count < max_pages——去掉时间墙 loop_advance.time_ms < window_end_ms：
        fills 满页触界（max_time == window_end_ms）后仍需 fromId 续取同毫秒剩余记录，
        时间墙会提前退出。循环终止由 _resolve_page_outcome 的 final_cursor → should_break 触发。
        无限循环由 max_pages + _detect_unstoppable 双重兜底。
        """
        errors: List[str] = []; cursor: Optional[CursorRecord] = None
        page_count = 0
        loop_advance = PageAdvance(time_ms=cur_start, event_id=cur_event_id)

        while page_count < self.max_pages:
            page_count += 1
            done = await self._one_page_iteration(
                ctx=ctx,
                loop_advance=loop_advance,
                seen=seen, collected=collected,
                last_retry=cursor.retry_count if cursor else 0,
            )

            loop_advance = done.next_advance
            cursor, should_break = self._resolve_iteration_dispatch(done, errors)
            if should_break:
                break

        else:
            # max_pages 强制退出——仅循环至少跑了一次但 page_count 打满才走这里。
            errors.append(f"翻页超过 max_pages={self.max_pages}，强制终止")
            cursor = self._max_pages_guard_cursor(
                source=ctx.source, account_id=ctx.account_id,
                income_type=ctx.income_type, symbol=ctx.symbol,
                window_start_ms=ctx.window_start_ms, window_end_ms=ctx.window_end_ms,
                covered_until_ms=loop_advance.time_ms, returned_count=len(collected),
                last_event_id=loop_advance.event_id,
            )

        assert cursor is not None, "while 循环结束前 cursor 必须已赋值"
        return cursor, errors

    async def _run_export_loop(
        self, *,
        ctx: _PageCtx,
        cur_start: int,
        seen: Set[Any], collected: List[Any],
        sink_upsert: Optional[Callable[[List[Any]], Any]],
    ) -> Tuple[CursorRecord, List[str]]:
        """通用翻页循环编排：前置守卫 + 分页循环 + 统一收尾。

        事务边界：先 sink_upsert 写事实（含 raw），确认成功后再 save_cursor。
        sink 或 cursor 任一失败 → page_complete=False + errors 非空，游标不推进
        （covered_until_ms 回退到本次起点，重试时可从窗口起点重采）。
        """
        # 前置守卫 + 先加载已有游标 + 按完成态决定是否翻页
        cursor, errors, loaded_cursor = await self._prepare_cursor_and_fetch(
            ctx=ctx, cur_start=cur_start,
            seen=seen, collected=collected,
        )

        # ═══ 事务边界：先事实后游标（委托 _commit_transaction）═══
        await self._commit_transaction(
            cursor=cursor, collected=collected, source=ctx.source,
            sink_upsert=sink_upsert, cur_start=cur_start,
            loaded_cursor=loaded_cursor, errors=errors,
        )

        return cursor, errors

    # ── 前置守卫 + 断点恢复 + 翻页循环（_run_export_loop 的准备阶段） ──

    async def _prepare_cursor_and_fetch(
        self, *,
        ctx: _PageCtx,
        cur_start: int,
        seen: Set[Any], collected: List[Any],
    ) -> Tuple[CursorRecord, List[str], Optional[CursorRecord]]:
        """前置守卫 + 先加载已有游标 + 按完成态决定是否翻页。

        返回 (cursor, errors, loaded_cursor)。loaded_cursor 在两个分支都可能非空——
        守卫分支（幂等 OK 短路）里 loaded_cursor 是已完成游标；
        翻页循环分支里 loaded_cursor 是断点恢复用（含 last_event_id）。
        仅当 sink 和内存 store 都无游标时 loaded_cursor=None。

        守卫修正（Round 4）：仅当 loaded_cursor 存在且 page_complete=True 时
        才允许幂等 OK 短路。loaded_cursor=None 表示首次查询——即便 cur_start
        已到 window_end_ms（如单毫秒闭区间），也必须进入翻页循环查一次，
        由 `_build_complete` / `_build_unstoppable_result` 判定窗口真实状态，
        不能在入口就假设"无游标=窗口为空"。
        """
        loaded_cursor = await self._load_cursor(
            ctx.source, ctx.income_type, ctx.symbol, ctx.window_start_ms,
        )

        # 守卫必须同时命中三件事：到界、有游标、游标已完成 → 才允许幂等 OK 短路。
        # 若 loaded_cursor 为 None（首次查询）或 page_complete=False（partial /
        # incomplete），不得短路——必须进入翻页循环重新判定。
        if (
            cur_start >= ctx.window_end_ms
            and loaded_cursor is not None
            and loaded_cursor.page_complete
        ):
            cursor = self._ok_cursor(
                source=ctx.source, account_id=ctx.account_id,
                income_type=ctx.income_type, symbol=ctx.symbol,
                window_start_ms=ctx.window_start_ms, window_end_ms=ctx.window_end_ms,
                covered_until_ms=cur_start, returned_count=len(collected),
                last_event_id=ctx.last_event_id(collected),
            )
            errors: List[str] = []
        else:
            # 翻页循环分支：断点恢复从 loaded_cursor.last_event_id 续传
            cur_event_id = (
                loaded_cursor.last_event_id if loaded_cursor else None
            )
            cursor, errors = await self._run_paging_until_done(
                ctx=ctx, cur_start=cur_start, cur_event_id=cur_event_id,
                seen=seen, collected=collected,
            )
        return cursor, errors, loaded_cursor

    # ── 事务边界：先事实后游标（先 sink_upsert，成功后 save_cursor） ──

    async def _commit_transaction(
        self, *,
        cursor: CursorRecord,
        collected: List[Any],
        source: str,
        sink_upsert: Optional[Callable[[List[Any]], Any]],
        cur_start: int,
        loaded_cursor: Optional[CursorRecord],
        errors: List[str],
    ) -> None:
        """事务边界：先 sink_upsert 写事实（含 raw），成功后再 save_cursor。

        sink 或 cursor 任一失败 → 就地修改 cursor 字段 + 追加 errors，
        cursor.covered_until_ms 回退到 cur_start（R2 关键：重试从起点重采）。
        """
        # 1) 先写事实（含 raw），失败则 page_complete=False + errors，游标回退
        sink_errors = await self._sink_upsert_checked(
            sink_upsert, collected, source,
        )
        if sink_errors:
            # sink 写失败 → 游标不推进：covered_until_ms 回退到本次起点
            cursor.page_complete = False
            cursor.request_status = "error"
            cursor.covered_until_ms = cur_start  # 关键：回退让重试能从原起点重采
            cursor.last_event_id = loaded_cursor.last_event_id if loaded_cursor else None
            cursor.error_detail = "; ".join(sink_errors)[:_ERROR_DETAIL_TRUNCATE]
            errors.extend(sink_errors)

        # 2) 事实成功后再 save_cursor；失败同样追加 errors + 回退推进位
        # （与 sink 失败分支对称：都回退到 cur_start，保证失败后重试范围一致）
        cursor_errors = await self._save_cursor_checked(cursor)
        if cursor_errors:
            errors.extend(cursor_errors)
            cursor.page_complete = False
            cursor.request_status = "error"
            cursor.covered_until_ms = cur_start

    @staticmethod
    def _resolve_iteration_dispatch(
        done: "PageIterationResult",
        errors: List[str],
    ) -> Tuple[CursorRecord, bool]:
        """消费 PageIterationResult：追加 errors、返回 cursor + 是否 break。"""
        if done.is_unstoppable:
            errors.append(done.unstoppable_detail or "无法继续翻页")
            assert done.final_cursor is not None
            return done.final_cursor, True
        if done.final_cursor is not None:
            if done.error_msg:
                errors.append(done.error_msg)
            return done.final_cursor, True
        assert done.partial_cursor is not None
        return done.partial_cursor, False

    # ── _one_page_iteration 的辅助：不可推进检测 ──

    @staticmethod
    def _detect_unstoppable(
        raw_page_full: bool,
        new_advance: PageAdvance,
        loop_advance: PageAdvance,
        supports_id_cursor: bool,
        window_end_ms: int,
    ) -> bool:
        """不可推进检测（区分 fills / income）。

        只有原始页满（raw_page_full=True）才有可能命中：
          - fills（supports_id_cursor=True）：有 fromId，只有「ID 卡住」才不可续传
            （时间卡住不阻塞——fromId 续传能越过同毫秒）。
          - income（supports_id_cursor=False）：靠时间推进；到达右边界或同毫秒
            时间卡住 → 无法再细分，标记 incomplete。
        """
        if not raw_page_full:
            return False
        if supports_id_cursor:
            # fills：ID 卡住才是真阻塞
            return new_advance.event_id == loop_advance.event_id
        # income：到右边界或同毫秒时间卡住
        return (new_advance.time_ms >= window_end_ms
                or new_advance.time_ms == loop_advance.time_ms)

    async def _one_page_iteration(
        self, *,
        ctx: _PageCtx,
        loop_advance: PageAdvance,
        seen: Set[Any], collected: List[Any],
        last_retry: int,
    ) -> "PageIterationResult":
        """执行一次循环迭代：请求 → 保存原始页 → 客户端过滤 → 委托 _resolve_page_outcome。

        关键：过滤前保存 raw_rows——续传页（fromId）会夹带越界记录被过滤掉，
        必须基于原始条数判断本页是否满。
        """
        raw_rows = await self._try_fetch_page(
            ctx.api_call_factory(loop_advance.time_ms, loop_advance.event_id),
            source=ctx.source, account_id=ctx.account_id,
            income_type=ctx.income_type, symbol=ctx.symbol,
            window_start_ms=ctx.window_start_ms, window_end_ms=ctx.window_end_ms,
            cur_start=loop_advance.time_ms, retry_count=last_retry,
        )
        if isinstance(raw_rows, CursorRecord):
            err = raw_rows.error_detail or "请求失败"
            return PageIterationResult(
                final_cursor=raw_rows, error_msg=err,
                next_advance=PageAdvance(
                    time_ms=loop_advance.time_ms, event_id=loop_advance.event_id,
                ),
            )
        rows = self._filter_rows_within_window(
            raw_rows, ctx.row_time_ms, ctx.window_end_ms,
        )
        # 基于原始页（过滤前）判定是否满页——这是 R10 闭区间收尾的核心
        raw_page_full = len(raw_rows) >= self.page_limit
        saw_out_of_window = len(rows) < len(raw_rows)  # 有被过滤的越界记录
        return self._resolve_page_outcome(
            ctx=ctx,
            rows=rows, raw_page_full=raw_page_full,
            saw_out_of_window=saw_out_of_window,
            loop_advance=loop_advance, seen=seen, collected=collected,
        )

    # ── _one_page_iteration 后半：消费 + 分派（主方法 ≤ 30 行，子方法各 ≤ 25 行） ─

    def _resolve_page_outcome(
        self, *,
        ctx: _PageCtx,
        rows: List[Dict[str, Any]],
        raw_page_full: bool,
        saw_out_of_window: bool,
        loop_advance: PageAdvance,
        seen: Set[Any], collected: List[Any],
    ) -> "PageIterationResult":
        """一页消费 + 分派到三情形收尾。

        三情形（完成信号含两个变种）：
          (a1) 原始页未满 → 窗口内记录已排空；
          (a2) 原始页满但续传 API 全返回越界记录（过滤后 rows 空 + saw_out_of_window=True）
               → 同样是排空信号（不是真 ID 卡住）；
          (b) 原始页满 + 未触不可推进 → 继续续传；
          (c) 原始页满 + 命中不可推进 → incomplete。
        """
        new_items, new_advance = self._consume_page(
            rows, parse_row=ctx.parse_row, dedup_key=ctx.dedup_key,
            time_ms_of=ctx.time_ms_of, event_id_of=ctx.event_id_of,
            seen=seen, prev_advance=loop_advance,
        )
        collected.extend(new_items)

        # (a2) 越界排空信号：原始页满但过滤后全空 → 不是真 ID 卡住
        if raw_page_full and saw_out_of_window and len(rows) == 0:
            return self._build_complete(ctx, new_advance, collected)
        # (c) 不可推进检测（fills=ID 卡住；income=到界或同毫秒卡住）
        if self._detect_unstoppable(
            raw_page_full, new_advance, loop_advance,
            ctx.supports_id_cursor, ctx.window_end_ms,
        ):
            return self._build_incomplete(ctx, new_advance, collected)
        # (a1) 原始页未满 → 窗口内记录已排空
        if not raw_page_full:
            return self._build_complete(ctx, new_advance, collected)
        # (b) 原始页满 → 未完成，继续续传
        return self._build_partial(ctx, new_advance, collected)

    def _build_complete(
        self, ctx: _PageCtx, new_advance: PageAdvance, collected: List[Any],
    ) -> "PageIterationResult":
        """构造完成态游标（a1 原始页未满 或 a2 越界排空）。"""
        last_event = new_advance.event_id or ctx.last_event_id(collected)
        cursor = _make_cursor(
            source=ctx.source, account_id=ctx.account_id,
            income_type=ctx.income_type, symbol=ctx.symbol,
            window_start_ms=ctx.window_start_ms, window_end_ms=ctx.window_end_ms,
            covered_until_ms=ctx.window_end_ms,
            returned_count=len(collected),
            page_complete=True, request_status="ok",
            last_event_id=last_event,
        )
        return PageIterationResult(
            final_cursor=cursor, next_advance=new_advance,
        )

    def _build_partial(
        self, ctx: _PageCtx, new_advance: PageAdvance, collected: List[Any],
    ) -> "PageIterationResult":
        """构造未完成继续续传态的 partial 游标（情形 b）。"""
        cursor = _make_cursor(
            source=ctx.source, account_id=ctx.account_id,
            income_type=ctx.income_type, symbol=ctx.symbol,
            window_start_ms=ctx.window_start_ms, window_end_ms=ctx.window_end_ms,
            covered_until_ms=new_advance.time_ms,
            returned_count=len(collected),
            page_complete=False, request_status="partial",
            last_event_id=new_advance.event_id,
        )
        return PageIterationResult(
            partial_cursor=cursor, next_advance=new_advance,
        )

    def _build_incomplete(
        self, ctx: _PageCtx, new_advance: PageAdvance, collected: List[Any],
    ) -> "PageIterationResult":
        """构造不可推进 incomplete 游标（情形 c）。"""
        if ctx.supports_id_cursor:
            detail = "接口返回未推进（事件ID卡住），无法继续翻页"
        elif new_advance.time_ms >= ctx.window_end_ms:
            detail = (
                "income 同毫秒记录可能超单页且接口不支持事件ID续传，"
                "无法确认右边界排空"
            )
        else:
            detail = (
                "income 同毫秒记录超过单页上限且接口不支持事件ID续传，"
                "无法继续翻页"
            )
        cursor = _make_cursor(
            source=ctx.source, account_id=ctx.account_id,
            income_type=ctx.income_type, symbol=ctx.symbol,
            window_start_ms=ctx.window_start_ms, window_end_ms=ctx.window_end_ms,
            covered_until_ms=new_advance.time_ms,
            returned_count=len(collected),
            page_complete=False, request_status="incomplete",
            last_event_id=new_advance.event_id,
            error_detail=detail,
        )
        return PageIterationResult(
            final_cursor=cursor, is_unstoppable=True,
            unstoppable_detail=detail, next_advance=new_advance,
        )

    @staticmethod
    def _filter_rows_within_window(
        rows: List[Dict[str, Any]],
        row_time_ms: Callable[[Dict[str, Any]], Optional[int]],
        window_end_ms: int,
    ) -> List[Dict[str, Any]]:
        """客户端窗口右边界过滤。

        续传页不带 endTime，API 可能返回 time > window_end_ms 的越界记录；
        在消费阶段过滤掉，保证不越界采集、不污染游标。
        对原始行时间为 None 的记录保留（无法判定，交给下游处理）。
        """
        out: List[Dict[str, Any]] = []
        for r in rows:
            t = row_time_ms(r)  # 一次取值，避免列表推导里调用两次
            if t is None or t <= window_end_ms:
                out.append(r)
        return out

    async def _try_fetch_page(
        self,
        api_call_coro: Any,
        *,
        source: str,
        account_id: str,
        income_type: Optional[str],
        symbol: Optional[str],
        window_start_ms: int,
        window_end_ms: int,
        cur_start: int,
        retry_count: int,
    ) -> Any:
        """执行一次 API 请求；抛异常时构造 error 游标返回，否则返回 rows 列表。

        返回值二选一：
          - List[Dict[str, Any]]：成功返回数据（可能空）
          - CursorRecord：请求失败的错误游标（调用方 break）
        """
        try:
            rows = await api_call_coro
            return rows
        except Exception as exc:
            logger.warning(
                "API 请求失败，不丢已取数据",
                source=source, account_id=account_id, income_type=income_type,
                symbol=symbol,
                error=str(exc)[:_ERROR_DETAIL_TRUNCATE],
            )
            return _make_cursor(
                source=source, account_id=account_id, income_type=income_type,
                symbol=symbol,
                window_start_ms=window_start_ms, window_end_ms=window_end_ms,
                covered_until_ms=cur_start,
                request_status="error",
                retry_count=retry_count + 1,
                error_detail=str(exc)[:_ERROR_DETAIL_TRUNCATE],
            )

    async def _sink_upsert_checked(
        self,
        sink_upsert: Optional[Callable[[List[Any]], Any]],
        collected: List[Any],
        source: str,
    ) -> List[str]:
        """有 sink 且有数据时尝试下沉；失败返回 error 列表（上抛被捕获后转 errors）。

        与旧 _maybe_sink_upsert 的区别：失败不再吞，返回 errors 列表由调用方处理。
        """
        if sink_upsert is None or not collected:
            return []
        try:
            await sink_upsert(collected)
            return []
        except Exception as exc:
            msg = f"{source} 事实下沉失败: {str(exc)[:_LOG_FIELD_TRUNCATE]}"
            logger.error(msg)
            return [msg]

    # ── 游标持久化（注入 sink + 内存 fallback） ─────────────────

    def _cursor_key(
        self,
        source: str,
        account_id: str,
        income_type: Optional[str],
        symbol: Optional[str],
        window_start_ms: int,
    ) -> Tuple[str, str, Optional[str], Optional[str], int]:
        """构造游标身份键（内存 store 用；与 sink SQL 查询条件一致）。"""
        return (source, account_id, income_type, symbol, window_start_ms)

    async def _load_cursor(
        self,
        source: str,
        income_type: Optional[str],
        symbol: Optional[str],
        window_start_ms: int,
    ) -> Optional[CursorRecord]:
        """尝试从 sink 或内存取已有游标（断点恢复）。"""
        key = self._cursor_key(source, self.account_id, income_type, symbol, window_start_ms)
        if self.sink is not None:
            loader = getattr(self.sink, "load_cursor", None)
            if loader is not None:
                try:
                    loaded = await loader(
                        source, self.account_id, income_type, symbol, window_start_ms,
                    )
                    if loaded is not None:
                        self._cursor_store[key] = loaded
                        return loaded
                except Exception as exc:
                    logger.warning(
                        "sink.load_cursor 失败，降级内存查找",
                        source=source, account_id=self.account_id,
                        income_type=income_type, symbol=symbol,
                        error=str(exc)[:_LOG_FIELD_TRUNCATE],
                    )
        return self._cursor_store.get(key)

    async def _save_cursor_checked(self, cursor: CursorRecord) -> List[str]:
        """落游标：内存 store 必写（即使 sink 失败也记下来供下次重试），
        sink 写失败返回 error 列表。"""
        key = self._cursor_key(
            cursor.source, cursor.account_id, cursor.income_type,
            cursor.symbol, cursor.window_start_ms,
        )
        self._cursor_store[key] = cursor
        if self.sink is None:
            return []
        saver = getattr(self.sink, "save_cursor", None)
        if saver is None:
            return []
        try:
            await saver(cursor)
            return []
        except Exception as exc:
            msg = f"{cursor.source} 游标持久化失败: {str(exc)[:_LOG_FIELD_TRUNCATE]}"
            logger.error(msg)
            return [msg]


@dataclass
class PageIterationResult:
    """_one_page_iteration 的返回载体。

    新增 next_advance（PageAdvance）和 is_unstoppable 标志。
    单次迭代只有一个 break 分支会生效：
      - 请求失败 / 到页尾 / 到窗口上界 → final_cursor 有值，partial_cursor=None
      - 本页满且窗口未结束 → partial_cursor 有值，final_cursor=None
      - 不可推进（同毫秒溢出 + 接口不支持 ID 续传）→ is_unstoppable=True
    """
    final_cursor: Optional[CursorRecord] = None
    partial_cursor: Optional[CursorRecord] = None
    error_msg: Optional[str] = None
    next_advance: Optional[PageAdvance] = None
    is_unstoppable: bool = False
    unstoppable_detail: Optional[str] = None

def _last_trade_id(collected: List[FillFact]) -> Optional[int]:
    for fact in reversed(collected):
        return fact.trade_id
    return None


def _last_tran_id(collected: List[IncomeFact]) -> Optional[int]:
    for fact in reversed(collected):
        return fact.tran_id
    return None


# ────────────────────────────────────────────────────────────────────
# CLI 入口（本轮旁路纯采集 + 注入式 sink，默认不带 sink）
# ────────────────────────────────────────────────────────────────────

def _resolve_window(args: argparse.Namespace) -> Tuple[int, int, List[str]]:
    """解析 CLI 参数 → (窗口起点毫秒, 窗口终点毫秒, 交易对列表)。"""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - args.days * 24 * 3600 * 1000
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    return start_ms, now_ms, symbols


def _build_client_from_env():
    """从环境变量构造 BinanceClient；密钥缺失时打印中文报错并返回 (None, exit_code=2)。

    返回值二选一：
      - (BinanceClient, 0)：正常
      - (None, 2)：环境变量缺失
    """
    from shared.binance_api import BinanceClient  # type: ignore
    api_key = os.environ.get(_ENV_API_KEY, "").strip()
    api_secret = os.environ.get(_ENV_API_SECRET, "").strip()
    if not api_key or not api_secret:
        logger.error(
            "未找到币安 API 密钥，请先设置环境变量后重试",
            env_api_key=_ENV_API_KEY, env_api_secret=_ENV_API_SECRET,
        )
        print(
            f"错误：缺少环境变量 {_ENV_API_KEY} 和/或 {_ENV_API_SECRET}。"
            "请在 .env 或 shell 里配置后再运行 CLI。",
            file=sys.stderr,
        )
        return None, 2

    # use_unified_account 由 BinanceClient 默认值决定（项目默认 True）。
    # income 仅支持统一账户；非统一账户模式由 export_income 与
    # BinanceClient.get_income_history 显式拒绝。
    client = BinanceClient(api_key=api_key, api_secret=api_secret)
    return client, 0


# ── DB sink 组装 / 释放（封装开关判断 + 延迟 import + connect/disconnect）─

async def _maybe_open_sink():
    """按 EXECUTION_FACTS_DB_WRITE 决定是否组装 PG sink。

    truthy（1/true/yes/on，大小写不敏感）才开启；connect 失败降级
    (None, None)，不阻塞采集。返回 (sink, db_manager)。
    """
    flag = os.environ.get(_ENV_DB_WRITE, "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return None, None
    try:
        from shared.database import DatabaseManager
        from shared.execution_facts_sink import PostgresExecutionFactsSink
        db = DatabaseManager(
            host=os.getenv("DATABASE_HOST", "postgres"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            database=os.getenv("DATABASE_NAME", "trading_platform"),
            user=os.getenv("DATABASE_USER", "trading_user"),
            password=os.getenv("DATABASE_PASSWORD", "trading_password_2024"),
        )
        await db.connect()
        sink = PostgresExecutionFactsSink(db)
        logger.info("DB sink 已开启，execution_facts 将落库")
        return sink, db
    except Exception as exc:
        logger.warning(
            "DB sink 组装失败，降级为纯 JSONL 输出",
            error=str(exc)[:_ERROR_DETAIL_TRUNCATE],
        )
        return None, None


async def _maybe_close_sink(db_manager: Any) -> None:
    """若开启了 db_manager 则释放连接池；异常忽略。"""
    if db_manager is None:
        return
    try:
        await db_manager.disconnect()
    except Exception as exc:
        logger.warning("DB disconnect 异常，忽略", error=str(exc)[:_ERROR_DETAIL_TRUNCATE])


async def _run_cli_fills(
    exporter: ExecutionFactExporter, symbols: List[str],
    start_ms: int, now_ms: int,
) -> int:
    """按 symbols 循环采集 fills，输出 JSONL。有 errors 时返回 1。"""
    exit_code = 0
    for sym in symbols:
        result = await exporter.export_fills(sym, start_ms, now_ms)
        _emit_jsonl("fills", symbol=sym, result=result)
        logger.info(
            "fills 采集完成",
            symbol=sym, total=len(result.records),
            page_complete=result.page_complete, errors=result.errors,
        )
        if result.errors:
            exit_code = 1
    return exit_code


async def _run_cli_income(
    exporter: ExecutionFactExporter,
    start_ms: int, now_ms: int,
    income_type: str,
) -> int:
    """采集 income，输出 JSONL。有 errors 时返回 1。"""
    result = await exporter.export_income(start_ms, now_ms, income_type=income_type)
    _emit_jsonl("income", result=result)
    logger.info(
        "income 采集完成",
        total=len(result.records),
        page_complete=result.page_complete, errors=result.errors,
    )
    return 1 if result.errors else 0


async def _run_cli(args: argparse.Namespace) -> int:
    """CLI 编排入口：解析参数 → 构造 client → 执行模式 → 汇总 exit_code。

    EXECUTION_FACTS_DB_WRITE=true/yes/on/1 时自动组装 PG sink；
    否则沿用 JSONL 纯输出。finally 保证 DB 连接池释放。
    """
    start_ms, now_ms, symbols = _resolve_window(args)
    client, code = _build_client_from_env()
    if client is None:
        return code

    sink, db_manager = await _maybe_open_sink()
    exporter = ExecutionFactExporter(
        account_id=args.account_id, api_client=client,
        sink=sink, page_limit=args.page_limit,
    )
    try:
        exit_code = 0
        if args.mode in ("fills", "both") and symbols:
            exit_code |= await _run_cli_fills(exporter, symbols, start_ms, now_ms)
        if args.mode in ("income", "both"):
            exit_code |= await _run_cli_income(
                exporter, start_ms, now_ms, args.income_type,
            )
        return exit_code
    finally:
        await _maybe_close_sink(db_manager)


def _emit_jsonl(kind: str, result: ExportResult, *, symbol: Optional[str] = None) -> None:
    """把一次采集结果以 JSONL 形式打印到 stdout。"""
    summary = {
        "kind": kind,
        "symbol": symbol,
        "record_count": len(result.records),
        "page_complete": result.page_complete,
        "cursor": {
            "source": result.cursor.source if result.cursor else None,
            "covered_until_ms": result.cursor.covered_until_ms if result.cursor else None,
            "request_status": result.cursor.request_status if result.cursor else None,
        },
        "errors": result.errors,
    }
    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="D1 旁路只读采集：execution_facts（不改任何生产者行为）",
    )
    parser.add_argument("--mode", choices=["fills", "income", "both"], default="both")
    parser.add_argument("--symbols", help="逗号分隔的交易对，如 BTCUSDT,ETHUSDT")
    parser.add_argument("--days", type=int, default=7, help="回看窗口天数（业务参数，默认 7）")
    parser.add_argument("--account-id", default=os.environ.get("TRADE_ACCOUNT_ID", "default"))
    parser.add_argument("--income-type", default="REALIZED_PNL")
    parser.add_argument("--page-limit", type=int, default=_DEFAULT_PAGE_LIMIT)
    args = parser.parse_args()

    exit_code = asyncio.run(_run_cli(args))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
