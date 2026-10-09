# grid V2.5.4 实时风险预警 · 实施计划（先证参数阶段）

**主题**：面向人工操作的 WebSocket 聚合成交风险预警 —— 先证参数、再通功能

**版本**：实施计划 v1.1（M0 启动：§1.2/§1.3 落回 R2-S3 口径定稿）

**日期**：2026-10-08

**关联需求**：[Grid_Trading_V2.5.4.md](../requirements/grid/Grid_Trading_V2.5.4.md)（权威，已完成三轮评审）

**状态**：M0 数据通路已完成（2026-10-09：采集器 9 文件 + 87 测试行/分支 100%，47.79h/2,814,926 笔真实回溯已合并验证）；未部署任何在线设施，停在 M0/M1 门等 C 段复审与用户授权

---

## 0. 排期原则与双轨数据路径

用户已拍板三个决策：

1. **排期 = 先证参数，再通功能**：先用离线回放验证参数方向、锁定 rules 设计，再建在线设施（WS 接入 / 双进程 / SQLite 持久化 / 心跳 / DEGRADED），避免先建设施再返工。
2. **数据路径 = 双轨并行**：
3. **影子先行**：在线设施（含 WebSocket）不等到 30 天样本后才建，而是与轨道B 数据积累**并行**——规则引擎离线可回放（M1）通过后，先以**影子模式**上线（只记录 + 「研究参考级」提醒，明确标注未经样本外验证、可能误报/漏报），30 天统计验证通过后才切正式 alert。用户现有保护（小时巡检 + 手动 APP 终止价）全程保留。

| 轨道 | 目标 | 数据来源 | 状态 |
|---|---|---|---|
| **A 机制方向验证** | 快速验证效率因子去留 + 采样尺度 | REST 拉最近 1~2 天 + 现有 20 分钟样本 | 立即执行 |
| **B 统计功效验证** | 攒 30 天历史样本 | 常驻落库器（**立即上服务器，独立最小容器**，早启动等 30 天） | 立即部署（2026-10-08 起算，见 §0.1） |

**核心待解科学问题**（需求 §2.6）：研究种子 `research_seed_01` 的 `E_300` 效率门槛 0.60 把「5m 跌 0.5% 的 255 秒提前量」完全滤掉（四个整秒决策点 `E_300 = 0.3465/0.2456/0.2511/0.3250` 均 < 0.60）。文档明确不得因单事件直接降阈值，须比较「取消效率过滤 / 较低门槛 / 原门槛」×「采样尺度」对震荡误报与可操作覆盖率的影响。

### 0.1 服务器先行落库（2026-10-08 决策，方案 A）

轨道B 常驻落库器**立即以独立最小容器上服务器**，与 M1 离线回放及 M0 之前的在线设施建设并行推进，使 30 天统计验证段的起始日不被在线设施返工延误：

- **形态**：独立最小容器，仅含 REST 增量拉取 + `agg_trades` / `price_samples_1s` 落库 + 滚动清理（§1.2/§1.3），不含 WS、规则引擎、通知等 M2 设施。
- **数据落点**：服务器挂载独立命名卷至 `/app/data`（容器内 `AGGTRADE_BASE_DIR=/app`），配置项 `storage.db_path: data/aggtrades/ethusdt_aggtrades.sqlite` 解析为 `/app/data/aggtrades/ethusdt_aggtrades.sqlite`（同 §1.3）；与 `grid_realtime.sqlite3` 卷分离。
- **起算口径**：验证段自落库器上线当日 **2026-10-08（UTC+8）00:00 起算**，累计 ≥30 完整自然日；因容器故障缺失的自然日单独列出（§3.5），不自动顺延。
- **与 M2 关系**：M2 影子设施上线后，实时进程 feed 复用同一 `ethusdt_aggtrades.sqlite` 卷做实时消费与续传定位，落库器的 REST 补缺口职责保持不变（§1.5）；落库器是 M2 前就绪、M2 接管消费的先行件。
- **部署口径**：走目标项目部署规则（新增加库器镜像与 compose 条目）；本计划文档修订不视同已部署，实际落库以部署日志与容器内首批成交记录为准。

---

## 1. 数据通路与落点

### 1.1 拉取口径（币安 /fapi/v1/aggTrades）

- 端点 `GET /fapi/v1/aggTrades`，公共接口 `signed=False`。
- 参数三选一（互斥）：`startTime+endTime+limit`（**时间窗 ≤ 1h**，币安硬约束，limit≤1000）/ `fromId+limit`（游标）/ 仅 `limit`（取最近 N 条）。
- 分页：
  - 轨道A 回溯：按 1h 切片 `for t in range(start, end, 1h)`，切片内若返回满 1000 条则用本批最后一条 `a` 作 `fromId` 继续翻页（时间切片 + ID 翻页双保险）。
  - 轨道B 增量：`fromId = 库内 max(agg_trade_id)` 逐页取，直到空或到当前时间。
- 限流/重试：复用 [download_klines.py](../../scripts/download_klines.py) 范式 —— 指数退避 `2^retry`（1/2/4s，max 3 次），仅 `ConnectionError/SSLError/429` 重试，`4xx` 直接放弃，页间 `sleep(0.05~0.1)`。
- 断点续传：每页提交后持久化 checkpoint；重启从 checkpoint 继续。
- 补缺口：以「库内 max(trade_time_ms) 是否连续」判定，有缝则回补，MAX 窗口 = REST 可回溯深度（1~2 天），更早的记 `gap_start/gap_end` 不硬凑。

### 1.2 落库 schema（两张表：逐笔 + 1s 采样）

```sql
CREATE TABLE IF NOT EXISTS agg_trades (
  agg_trade_id  INTEGER PRIMARY KEY,          -- 币安按品种聚合后的全局唯一 ID，天然主键
  symbol        TEXT NOT NULL,                -- ETHUSDT
  price         TEXT NOT NULL,                -- Decimal 字符串，避免浮点
  quantity      TEXT NOT NULL,
  first_trade_id INTEGER, last_trade_id INTEGER,
  trade_time_ms INTEGER NOT NULL,             -- 交易所事件时间 T
  is_buyer_maker  INTEGER NOT NULL,           -- 0/1
  received_at_ms  INTEGER NOT NULL            -- 本地取得时刻：WS 行为真实接收时刻；REST/文件导入行为本地拉取/导入执行时刻（不代表行情到达，arrival 回放仅 WS 行可用，见 §2.1；NOT NULL 保留）
) WITHOUT ROWID;
CREATE INDEX idx_agg_symbol_time ON agg_trades(symbol, trade_time_ms);
```

- 去重：`INSERT OR IGNORE` / `ON CONFLICT(agg_trade_id) DO NOTHING`，主键单调递增天然幂等，无更新语义。

1s 整秒采样价格序列（统计验证主表，全量 30 天不滚动，与逐笔同库；**S3 定稿，2026-10-08 B 段落回**）：

```sql
CREATE TABLE IF NOT EXISTS price_samples_1s (
  sample_ms      INTEGER PRIMARY KEY,   -- 整秒决策点 t（.000）；p(t)=不晚于 t 的最后一笔
  symbol         TEXT NOT NULL,          -- ETHUSDT
  price          TEXT NOT NULL,          -- 不晚于 t.000 的最后一笔成交价（T<=t，可早于 t）；空缺点沿用前锚点
  trade_count    INTEGER NOT NULL,       -- 窗口 (t-1s, t] 成交笔数；空缺点=0
  last_trade_id  INTEGER NOT NULL,       -- 锚点成交 agg_trade_id；空缺点沿用前一笔
  anchor_time_ms INTEGER NOT NULL,       -- 锚点成交事件时间，恒 <= sample_ms（S3-iii）
  materialized_at_ms INTEGER NOT NULL    -- 物化本地时刻（审计）
) WITHOUT ROWID;
CREATE INDEX idx_samples_symbol_time ON price_samples_1s(symbol, sample_ms);
```

- 物化语义（S3 三定，2026-10-08 B 段落回；M0 实现时对齐审计脚本决策点口径，无裁量空间）：
  1. **决策点价格口径**：`sample_ms=t` 为整秒决策点，价格取**不晚于 t.000** 的最后一笔成交（T<=t，需求 §5.3），锚点可以早于 t；与 `scripts/audit_grid_review.py` 的 `price(t)=bisect_right(ts,t)` 同口径，保证 M1 可复现四点效率与 0.746s（AC-24/31）。成交 T 按窗口 `(t-1s, t]` 归属，即决策点 `ceil(T/1000)*1000`：T 毫秒部分非 000 时等同 `floor(T)+1s`；**T 恰为整秒 .000 时属于 t 本身**（与 bisect_right 的 T<=t 一致），不推到下一点。早期文字曾写死 `floor(T)+1s`，在整秒边界与 bisect 矛盾；M0 用 164071 笔真实样本随机 500 点比对（ceil 口径 0 偏差）后改定为 ceil。
  2. **稠密落行**：首个决策点 = floor(首笔 T)+1s（与审计脚本起点一致），其后每个已关闭决策点都落一行；首笔之前无锚点可沿用，不落行。窗口 `(t-1s, t]` 有成交则 `price/last_trade_id/anchor_time_ms` 取窗口内最后一笔、`trade_count` 为窗口笔数；**无成交窗口也落行**，`trade_count=0`，其余三列沿用前一锚点。稠密行用以区分「行情真空（count=0 的行存在）」与「采集缺口（整行缺失）」，支撑需求 §8.1 缺口检测与 M3 序列完整性核对（约 86,400 行/天）。
  3. **锚点时刻列**：`anchor_time_ms` 为锚点成交的事件时间（恒 <= `sample_ms`）；逐笔按 72h 滚动删除、`last_trade_id` 无法再 join 后，特征可用性仍按 `sample_ms - anchor_time_ms <= features.max_anchor_gap_seconds*1000` 判定（S3-iii）。
  4. **关闭上界**：可物化的最大决策点 = floor(库内 max(trade_time_ms))——该点窗口 `(t-1s,t]` 已有成交（maxT 恰为整秒 .000 时其本身也在 T<=t 内，计入该点），证明已关闭故纳入物化；下一点尚未有成交关闭，不物化。样本范围与审计脚本 `range(..., ts[-1]//1000*1000+1, 1000)` 末点一致。
  5. `INSERT OR IGNORE` 依 `sample_ms` 幂等；乱序迟到的逐笔不回改已物化样本（对应需求 §8.1 迟到不回改已做判断）。

### 1.3 数据落点与存储策略（用户已确认）

- **独立 DB**，不并进 `grid_realtime.sqlite3`（后者受 `max_bytes=1GB` 写入准入与 WAL 竞争约束，混入行情会打爆水位）。
  - 本地 Mac：`data/aggtrades/ethusdt_aggtrades.sqlite`（相对仓库根解析）
  - 服务器：挂载独立命名卷至 `/app/data`，路径 `/app/data/aggtrades/ethusdt_aggtrades.sqlite`
    （基准目录 = `/app` 由容器 `AGGTRADE_BASE_DIR` 指定，与本地同为 `data/aggtrades/` 子目录结构）
- **存储策略 = 1s 整秒采样序列方案（用户确认）**：

| 用途 | 数据 | 保留策略 | 规模 |
|---|---|---|---|
| 事件复现精确验证（0.746s / 3.406s，AC-15/24） | 逐笔 aggTrades | 仅滚动保留 48~72h | 短期，可忽略 |
| 30 天统计功效验证（覆盖率/提前量/误报） | 1s 整秒采样价格序列 | 全量 30 天 | ~86,400 行/天 → **2.6M 行/月，<500MB** |

  滚动特征 `r_w`/`E_w` 最小输入粒度即 1 秒（`sample_seconds=1`，w∈{60,180,300}），1s 序列完全够统计验证；逐笔只用于毫秒级精确复现，现有 20 分钟样本已能覆盖，无需长期囤逐笔。

- **物化时机**：常驻落库器（轨道B）在每批逐笔 upsert 提交后，对已「关闭」的整秒决策点物化——以出现晚于决策点 t.000 的成交为关闭信号（可物化上界 = `floor(max_trade_time_ms/1000)*1000`，见 §1.2 第 4 条）；批次尾部未关闭决策点留待下一批或 checkpoint 时收口，避免并发改写已物化样本。无成交窗口按 §1.2-S3-ii 在稠密物化扫描中补 `trade_count=0` 沿用行；逐笔被 72h 清理后该序列仍自带 `anchor_time_ms`，锚距判定不依赖逐笔表（S3-iii）。
- **滚动清理执行者与频率（定值）**：由常驻落库器执行，默认每小时代替 checkpoint 执行一次（重启时立即执行一次）。逐笔 `agg_trades` 保留 `agg_trades_retention_hours`（默认 **72**，消除 48~72 歧义；配置项，禁硬编码），删除 `trade_time_ms` 早于保留窗的行；`price_samples_1s` 全量 30 天不滚动。清理与物化在同一事务提交，避免删掉未物化逐笔造成 1s 序列空洞。

### 1.4 拉取/落库关键函数签名

```python
async def fetch_agg_trades(symbol, *, start_time=None, end_time=None,
                           from_id=None, limit=1000) -> list[dict]  # 单页
async def fetch_range_agg_trades(symbol, start_time, end_time) -> AsyncIterator[dict]  # 轨道A
async def fetch_incremental_agg_trades(symbol, from_id) -> AsyncIterator[dict]          # 轨道B
def upsert_agg_trades(conn, trades: list[dict]) -> int   # INSERT OR IGNORE，返回新增数
def get_latest_agg_trade_id(conn, symbol) -> int | None  # 续传/重启定位
def has_gap(conn, symbol, since_ms) -> bool              # 回溯补缺口判据
```

### 1.5 断线补齐与连续性恢复（双写责任划分）

落库器（轨道B，REST 常驻）与实时进程（轨道B feed，WS + 落库）都写 `agg_trades`，均按 `agg_trade_id` 主键幂等去重；断线后的补齐责任**唯一归于落库器**：

- **执行者与数据源**：落库器常驻，用 REST `fromId = 库内 max(agg_trade_id)` 逐页补齐到当前时间；实时进程 feed 断线时不负责回补，只负责恢复后的实时消费与去重。
- **重连连续性核对口径**：实时进程重连后，将 WS 流首个 `agg_trade_id` 与库内 `max(agg_trade_id)` 比对（WS 流 id vs 库内 max id）。连续（`ws_first_id <= 库内 max_id + 1`）则进入窗口预热；有缝（缺口 id）则进入 DEGRADED，把缺口区间登记 `gap_start/gap_end` 交由落库器 REST 回补。
- **DEGRADED 退出判据**：缺口补齐完成（落库器写入后，库内 `max(agg_trade_id)` 连续且 ≥ WS 当前流 id）且滚动窗口预热完成（`features.max_anchor_gap_seconds` 内无缺口）后，实时进程退出 DEGRADED、恢复完整窗口判断。兜底窗口 = REST 可回溯深度（1~2 天）；更早缺口不硬凑，记 gap 不补（对应 §1.1 补缺口口径）。

---

## 2. 文件划分（realtime/ 代码骨架）

新增 `strategies/grid/realtime/` 下 8 文件，单向依赖，`rules` 零反向 import：

```
feed ──┐
       ├─► features ─► rules ─► state
reference_store(main 注入快照) ────────────┘
delivery ◄── main（异步队列，不进 rules）
main 编排：feed → features → rules → state → delivery
replay 复用 feed/features/rules/state（替换 Clock 与投递）
```

| 文件 | 职责 | 关键签名 |
|---|---|---|
| `feed.py` | aggTrades WS 接入、退避重连、去重、补齐；吐有序成交流 | `async iter_trades(cfg) -> AsyncIterator[AggTrade]` |
| `features.py` | 整秒采样、滚动窗口、效率；特征不可用填 `None` | `sample_price`、`compute_features` |
| `rules.py` | **纯规则**：不 import pandas/网络/时间/系统时钟 | `classify_region`、`evaluate_*`、`pick_state` |
| `state.py` | 状态机、持续计时、episode/reference 去重、恢复 | `evaluate_episode`、`record_event`、`recover` |
| `reference_store.py` | 快照合法性校验、reference_status 五态（VALID/STALE/MISSING/INVALID/SYNC_UNCERTAIN）、权威版本 | `accept_snapshot`、`current_status` |
| `delivery.py` | 事件 outbox、级别优先级、TTL、异步重试 | `enqueue`、`pump`、`dispatch_fact` |
| `main.py` | 独立进程入口，装配各层，注入 Clock | `run(cfg, clock)` |
| `replay.py` | 读历史成交、按时间+ID 喂引擎、输出决策/指标 | `load_trades`、`run_replay` |

### 2.1 Clock 抽象（注入，非依赖）

```python
class Clock(Protocol):
    def event_ms(self) -> int: ...                 # 交易所事件时间（窗口/采样用）
    def monotonic(self) -> float: ...              # 单调时钟（本地超时用）
    def sample_mark(self, event_ms: int) -> int: ...  # 整秒对齐（向下取整到秒）
```

- **event_time 回放（理想）**：`ReplayClock` 的 `event_ms` 由 feed 逐笔 `trade_time_ms` 推进；`monotonic` 用文件序假时钟。
- **arrival_time 回放（在线等价）**：按 `received_at`/接收顺序推进（含丢包）。原始文件无 `received_at`，本版只能 event_time（AC-18 要求同一模式重复一致，不要求两种模式结果相同）。差异**只落在 main 装配处**，特征/规则层无感知。

### 2.2 features 层

```python
@dataclass
class FeatureSlice:
    r_w: dict[int, Decimal | None]      # w∈{60,180,300}：r_w = p(t)/p(t-w)-1
    a_down: dict[int, Decimal | None]   # -r_w
    a_up: dict[int, Decimal | None]     #  r_w
    e_w: dict[int, Decimal | None]      # |q_last-q_first|/Σ|Δq|，分母0→0，缺口超限→None

def sample_price(trades_seq, target_ms: int, max_gap_ms: int) -> Decimal | None
    # 锚点：bisect_right 找不晚于 target 的最后一笔；距离 > max_anchor_gap_seconds → None
def compute_features(samples, now_ms, cfg) -> FeatureSlice   # 任一 q 缺失 → 该特征 None，不填零
```

### 2.3 rules 层（均纯函数，接收已算特征）

```python
class Region(Enum): INSIDE, BUFFER_DOWN, BUFFER_UP, BELOW_SL, ABOVE_SU

def classify_region(snap, price: Decimal) -> Region
def evaluate_normal(slice, region, cfg) -> bool            # 普通候选：a&方向持续&位置相关
def urgent_inside(slice, region, cfg) -> bool              # 紧急：区间内快速恶化
def urgent_buffer(slice, region, cfg) -> bool              # 紧急：缓冲区继续恶化（a_60）
def urgent_critical(region, u: Decimal, cfg) -> bool       # 紧急：critical_buffer_fraction<=u<1，逐笔
def evaluate_boundary(snap, price: Decimal) -> BoundaryFact | None
def pick_state(levels: Levels) -> Level   # BOUNDARY→URGENT→NOTICE→CANDIDATE→IDLE/UNKNOWN 自上而下
```

持续计时状态放 `state.py`：每方向维护 `holds: dict[branch, first_held_ms]`，满足 `setdefault` 记 t0、不满足 `pop`；`event_ms - t0 >= hold_seconds` 触发。三紧急分支计时各自独立不拼接。

```python
class Level(Enum): IDLE, CANDIDATE, NOTICE, URGENT, BOUNDARY_REACHED
def step(prev_level, new_level, episode, repeat_cfg) -> Action  # 升级绕过冷却；同级按间隔
def record_event(episode_id, reference_id, direction, level, ...) # 唯一键去重
def recover(db_row) -> Episode | None
```

### 2.4 replay.py

```python
def load_aggtrades(path) -> list[AggTrade]:       # 校验 SHA256 指纹
def sort_by_time_id(trades) -> list[AggTrade]     # trade_time_ms 升序、同毫秒按 aggTrades ID
def run_replay(trades, snapshot, cfg, mode) -> ReplayResult
    # 逐笔 feed→sample→features→rules→state；输出决策事件 + 覆盖率/提前量/误报指标
```

### 2.5 小时出口侧最小改动（由 [signal_bot.py](../../strategies/grid/signal_bot.py) 第 421-423 行改造）

```python
if self._should_notify(signal):
    sent = await self._send_notification(signal)   # 捕获返回值（原代码丢弃）
    self.last_signals[symbol] = signal             # 保留原「失败也前移」冷却语义
    if sent and signal.grid_params:                # 仅成功 + 含边界才导出快照
        await self.export_snapshot(signal)
```

配置加载入口（[config_loader.py](../../shared/config_loader.py) 新增兼容入口）：

```python
def load_strategy_config_with_metadata(strategy_dir: str) -> tuple[dict, dict]:
    # metadata: {"requested_overrides_version", "applied_overrides_version"(无覆盖记 None),
    #            "config_hash"(SHA256，排除密钥/webhook，UTF-8+排序键+固定分隔符+规范化数值),
    #            "load_error": str|None}
```

旧 `load_strategy_config` 委托同一加载路径仍只返回 `dict`；覆盖失败回退基础配置时 `applied_overrides_version=None`，不可标为已应用。

### 2.6 grid_realtime.sqlite3 持久化 schema（在线设施四表）

与 §1.2 的行情库分离；`grid_realtime.sqlite3` 走 WAL，小时出口独占写 reference/session，实时进程写 event/outbox（对应需求 §3.4）。

```sql
-- reference：权威建议快照（小时出口写，实时进程读）
CREATE TABLE IF NOT EXISTS reference (
  reference_id      TEXT PRIMARY KEY,     -- 唯一版本（同一消息重试不生成新版本）
  session_id        INTEGER NOT NULL,     -- 出口进程会话 ID
  seq               INTEGER NOT NULL,     -- 出口进程内递增序号
  send_status       TEXT NOT NULL,        -- PREPARED/SENDING/SENT/FAILED/UNKNOWN
  symbol            TEXT NOT NULL,
  calculated_at_ms  INTEGER NOT NULL,
  effective_at_ms   INTEGER,              -- SENT 后置为发送确认时刻
  grid_lower TEXT NOT NULL, grid_upper TEXT NOT NULL,
  stop_lower TEXT NOT NULL, stop_upper TEXT NOT NULL,
  stop_move_up_price TEXT, stop_move_down_price TEXT,
  market_state TEXT, atr TEXT, adx_1h TEXT, adx_4h TEXT,
  config_version TEXT, overrides_version TEXT, config_hash TEXT,
  message_id TEXT, source TEXT,
  created_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_reference_session_seq ON reference(session_id, seq);

-- session：出口进程会话与心跳快照（小时心跳线程写，§2.7）
CREATE TABLE IF NOT EXISTS session (
  session_id          INTEGER PRIMARY KEY, -- 出口进程启动会话 ID
  current_seq         INTEGER NOT NULL DEFAULT 0,  -- 当前递增序号（每次发送 +1）
  current_reference_id TEXT,               -- 当前确认参考
  pending_seq         INTEGER,             -- 未决发送序号（发送中置位，发送结束清空）
  status              TEXT NOT NULL,       -- ACTIVE/SYNC_UNCERTAIN/CLOSED
  updated_at_ms       INTEGER NOT NULL     -- 心跳更新时间
);

-- event：分级事件（实时进程写，唯一键去重）
CREATE TABLE IF NOT EXISTS event (
  event_id      TEXT PRIMARY KEY,   -- 唯一键：确定性生成（方向+reference_id+级别+episode 分量）
  episode_id    TEXT NOT NULL,
  reference_id  TEXT NOT NULL,
  direction     TEXT NOT NULL,      -- DOWN/UP
  level         TEXT NOT NULL,      -- CANDIDATE/NOTICE/URGENT/BOUNDARY_REACHED/HISTORICAL_REFERENCE_CROSSED/HEALTH
  first_held_ms INTEGER,
  sent_ms       INTEGER,
  config_hash   TEXT,
  payload       TEXT NOT NULL
);
CREATE INDEX idx_event_episode ON event(episode_id);

-- outbox：投递事实锁存（锁存语义，不因 TTL/反弹静默删除）
CREATE TABLE IF NOT EXISTS outbox (
  outbox_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id       TEXT NOT NULL,
  kind           TEXT NOT NULL,     -- BOUNDARY_REACHED / HISTORICAL_REFERENCE_CROSSED / 普通 / HEALTH
  status         TEXT NOT NULL,     -- PENDING/SENT/FAILED/UNKNOWN
  locked_at_ms   INTEGER NOT NULL,  -- 事实锁存时刻
  ttl_deadline_ms INTEGER,          -- 普通消息 TTL；锁存事实不设
  retry_count    INTEGER NOT NULL DEFAULT 0,
  payload        TEXT NOT NULL
);
CREATE INDEX idx_outbox_status ON outbox(status);
```

关键约束（对应需求 §4.2.7/§4.3/§8.2）：

- `reference.send_status` 仅允许 PREPARED/SENDING/SENT/FAILED/UNKNOWN；不得把任何记录伪造为 SENT。恢复只认「已确认发送、无更高未决版本、经当前出口会话确认」的快照。
- `reference_status` 五态（VALID/STALE/MISSING/INVALID/SYNC_UNCERTAIN）由 `reference_store.current_status` 运行时判定，非单独落列：MISSING=无行，STALE=按 `effective_at_ms` 对 `max_age_seconds` 派生，SYNC_UNCERTAIN=会话失联／持久化交接失败（见 `session.status`）。
- `session.current_seq` 单调递增，发送成功后 +1；心跳只读转嫁 `current_reference_id`/`pending_seq`，不替出口判定成功。
- `event.event_id` 唯一键幂等；`outbox` 的 BOUNDARY_REACHED 与 HISTORICAL_REFERENCE_CROSSED 为**锁存事实**，不适用普通 TTL、不因反弹静默删除，未送达/UNKNOWN 不因年龄清理。

### 2.7 小时侧心跳线程落点

心跳是小时出口进程内的常驻线程（需求 §3.5），**不**落在 `realtime/` 主进程内：

- **宿主文件**：小时侧新增 `strategies/grid/heartbeat.py`，由小时出口进程启动一个 daemon 线程；不改动小时调度等待逻辑。
- **发布介质**：写 `grid_realtime.sqlite3` 的 `session` 表（§2.6）——`session_id`、`current_seq`、`current_reference_id`、`pending_seq`、`updated_at_ms`。走持久表而非进程内存，因为小时出口与实时进程是两个独立进程，内存无法跨进程共享。
- **签名**：`def publish_heartbeat(state_snapshot, conn) -> None`，只读转嫁不可变状态快照，不在心跳线程计算指标、不判定发送成功。
- **实时进程消费方式与轮询频率**：`realtime/main.py` 每 `reference_sync.heartbeat_seconds`（研究种子默认 1s）轮询一次 `session` 表；连续超过 `reference_sync.max_silence_seconds`（默认 5s）未更新则判 SYNC_UNCERTAIN、暂停操作建议（对应需求 §3.5 心跳周期与失联阈值）。

---

## 3. 参数实验矩阵

### 3.1 采样尺度操作化定义（用户已确认）

§5.2/§5.3 的「采样固定 1 秒」是**决策/越界/持续性计时**采样，生产硬约束不可改。「采样尺度」（§2.6 所指）= **计算 `E_w` 时从整秒价格序列 q 抽取子序列的步长 stride**：

```
E_w(stride) = |q_last − q_first| / Σ|q_i − q_(i−1)|   （q 为每 stride 秒抽一点）
stride=1s → 300 点/窗（原口径）；stride=3s → 100 点；stride=5s → 60 点
```

机制：原事件 `E_300` 被压低，是 1s 细粒度下微幅往返把分母 Σ|Δq| 撑大；增大 stride 跳过微观往返，慢速单边 E 应回升。

实现：研究覆盖 profile 在 `realtime_alert` 节**新增研究专用字段 `features.e_resample_seconds`**（默认 1）。生产 `sample_seconds`/`windows_seconds` 仍强制校验；`e_resample_seconds` 仅 research profile 允许，alert 模式对未知字段拒绝加载。

### 3.2 正交矩阵（10 组合）

「无过滤」经 `efficiency_filter.enabled=false` 实现（阈值设 0 违反 `(0,1]` 校验）。涨跌幅/位置/持续在轨道A 固定（`return_5m=0.005`、`near_grid=0.40`、`hold=10s`），聚焦两因子避免维度爆炸。

| profile_id | 效率过滤 | E 采样尺度 stride |
|---|---|---|
| `research_seed_02.nofilter` | 无 | N/A |
| `research_seed_02.e30.s1/s3/s5` | 0.30 | 1s / 3s / 5s |
| `research_seed_02.e40.s1/s3/s5` | 0.40 | 1s / 3s / 5s |
| `research_seed_02.e60.s1/s3/s5` | 0.60 | 1s / 3s / 5s |

`e60.s1` 即原 `research_seed_01` 参数，作基线保留；`research_seed_01` 仅作失败对照，不复活为 alert。

### 3.3 对照设计两批（需求 §10.2 五组）

| 组 | 轨道A（立即） | 轨道B（30 天后） |
|---|---|---|
| ① 原小时策略 | 现有 10 条重建信号算基线增量 | 完整增量 + Wilson 区间 |
| ② 3m/15m 收盘 1.5%/2%（被替代方案） | 1m K线可算 | 误报/提前量权衡 |
| ③ 仅出区间/缓冲区升级 | aggtrades 可算，0.746s 复现 | 慢速逼近/快速跳越专项 |
| ④ 实时滚动+区间内+缓冲升级（候选） | **主矩阵 10 profile 全跑** | 可操作覆盖率、告警负担达标检验 |
| ⑤ 普通异动+收盘确认 | 现有数据可算方向 | 噪音过滤 vs 丢提前量量化 |

### 3.4 判读指标（需求 §10.4）

- **提前量分布**：首次同向穿越时间 − 预警决策时间，报中位数/分位数/负值；重点看 255s 前兆被哪些 profile 恢复。
- **可操作覆盖率**：扣 30/60/120s 人工延迟后仍为正占比。
- **误报代理**：普通提醒后未进同向缓冲也未升级且已恢复 → 误报，用预注册 15/30/60min 两档观察窗。
- **告警负担**：每日普通/紧急/越界事件数、重复数、最高密度。

**「保留/丢弃效率因子」判据**：
- 取消 E 后提前量恢复、误报不显著增 → E 方向错误，弃/大降；
- 大 stride 使单边 E 区分度改善 → 保留 E 改采样尺度；
- 门槛与尺度扫描下 E 无法同时保证提前量与低误报 → E 无信息，丢弃，改纯涨跌幅+位置。

### 3.5 数据划分与预注册

- **开发段**：2026-10-07 20min + 轨道A REST 1~2 天（标「已参与选参」，不可称样本外）。
- **验证段**：自落库器上线日 2026-10-08（UTC+8）起累计 ≥30 完整自然日，含震荡/慢速单边/快速单边/上涨；因容器故障缺失的自然日单独列出，不自动顺延。
- **预注册**（看验证段前登记，禁反改）：可接受告警负担上限、可操作覆盖率目标（扣 60s 后阈值）、误报代理两档比例上限；先冻结。

---

## 4. 验收口径与里程碑

### 4.1 AC-01~32 分档

**AC-01（兜底，从始至终）**：开关关闭/实时组件崩溃时，原小时计算、调度、推送行为保持一致 —— M0~M4 全阶段硬约束，任一阶段回归都须复验。

| 档 | AC 编号 | 理由 |
|---|---|---|
| **(a) 先证参数**（离线回放/规则正确性/事件复现） | 02, 04, 05, 06, 07, 08, 09, 15, 16, 18, 23, 24, 30, 31 | 快照更新/持续时间/取消候选/直接紧急/跨区/对称边界/episode 合并/0.746s 与诊断基线/理想回放可复现/计时与指纹 —— 均可用 REST 近 1~2 天 + 20 分钟样本地离线验证 |
| **(b) 通功能**（在线 WS/双进程/SQLite/心跳/DEGRADED/同步不确定） | 03, 10, 11, 12, 13, 17, 19, 21, 22, 26, 27, 28, 29 | 持久化恢复/参考原子过期/乱序补齐断线/数据不足恢复/投递重试 TTL 补报/峰值降级/双进程并行/SYNC_UNCERTAIN/STALE 补报/存储队列故障/历史快照/心跳中断/容器重建磁盘满 |
| **(c) 影子运行后**（shadow/alert/压力评估/研究审计） | 14, 20, 25, 32 | shadow 仅记录/alert 不调交易接口且账户文案审计/研究评估审计/慢速逼近人工提前量产品价值边界 —— 均需真实在线运行时长的数据支撑 |

### 4.2 里程碑

| 里程碑 | 进入条件 | 退出条件 | 产出物 |
|---|---|---|---|
| **M0 数据通路** | 任务启动（轨道A 立即可做） | REST 近 1~2 天 + 现有 20 分钟样本落库；字段映射/时间口径校验通过；开发/验证段划分标注完成 | 数据落库脚本、样本清单与划分标记；轨道B（30 天攒数）挂后台运行 |
| **M1 离线可回放** | M0 | (a) 档 AC 全部通过；回放工具按同一规则复现指纹、四点效率与 0.746s；§4.3「先证参数通过判定」成立（挂接 M1→M2 门） | 可复现回放工具、固定数据指纹、规则引擎（单一实现，回测不另写规则） |
| **M2 影子设施上线** | M1 | WebSocket 接入 + 规则引擎接入 + shadow 记录 + 「研究参考级」提醒 + 数据同步落库上线；(b) 档最低通过集 **AC-11/12/21/26/28/29 全部通过**，其余 (b) 档 AC 继续收尾 | 影子模式在线运行，用户收研究参考提醒，30 天数据开始实时积累 |
| **M3 统计功效验证** | M2（轨道B 满 30 天） | 30 天样本 + 实验矩阵全跑；与预注册目标比照（达/不达均出结论）；报告冻结 | 冻结实验报告、锁定/否决 profile |
| **M4 切正式 alert** | M3（验证通过）且 (b) 档 AC 全部通过 | (c) 档 AC 全部通过；alert 模式上线，完整投递/文案审计通过 | 上线结论 + alert 开关决策（验证不达则维持影子，不上线提前预警） |

### 4.3 先证参数判定线（挂接 M1→M2 门）

本判定在 M1 退出时执行，通过者才进入 M2「通功能」；不通过维持 shadow 不上线。

- **通过（进入「通功能」）**：≥1 个 profile 在轨道A 短样本上**同时**满足「恢复可操作提前量（扣 60s 后中位数提前仍为正）+ 误报代理不炸」；且取消/降门槛/改尺度多组合结论方向一致（不靠单一 profile 偶然）。
- **「误报代理不炸」两层判据**：
  - 开发段内部判据（M1 门，方向性、非统计结论）：候选 profile 在开发段样本上的误报代理事件数（普通提醒后未进同向缓冲也未升级且已恢复，15/30/60min 观察窗）不高于无过滤基线 `research_seed_02.nofilter` 的同观察窗误报数（绝对数 + 开发段样本量容差换算）；开发段误报数值在 M1 报告**登记为基线**，不得宣称统计结论。
  - 验证段正式合格线：按 §3.5 在进入验证段前**预注册冻结**两档误报比例上限与可操作覆盖率目标，看完验证段禁反改。
- **轨道A 无穿越事件的降级判定**：若轨道A REST 1~2 天样本内不存在终止价穿越事件，改用「首次进入下方/上方缓冲区（u_down/u_up ∈ (0,1) 首次满足）」作为穿越前预警的**代理锚点**评估提前量，并明确标注证据等级为「缓冲区代理、非真实终止价穿越」；若连缓冲区进入事件都没有，该轨道A 样本对「可操作提前量」判为不可评估，M1 判定退化为「仅规则正确性 + 方向效率因子去留」结论（不得宣称提前量验证通过），并把「无穿越事件」登记为验证段缺失行情类型，交由轨道B 继续攒属实样本。
- **失败（维持 shadow 不上线）**：无任何 profile 同时满足提前量与误报可控；或 E 有无对结果无区分且无额外收益。30 天验证不达预注册目标（§10.5）→ 不上线提前退出提醒，保留影子研究/边界事实有限功能。

---

## 5. 不做清单

- **FR-03**：不调用下单/撤单/改单/保证金/杠杆接口，只出建议告警。
- **AC-01/FR-01**：不改原小时策略的计算/调度/推送行为；回退 = 仅关闭实时组件，不撤销/改写交易所设置。
- 实时预警在线设施（WS/双进程/心跳/alert）的 docker-compose 变更仍等到 M3 另行走部署规范审批；**例外**：轨道B 常驻落库器按 §0.1「服务器先行落库」需新增独立最小容器与 compose 条目，属已批准例外，不在本条“不改 compose”限制内。
- AI 调优（StratTuneAI）不自动激活实时参数 —— 结果需人工审批，不直写 WS/规则参数。
- 回放复用同一规则实现，不另写一套业务规则；不输出「平均少亏 3%」等无依据收益表述（§10.4 末 / FR-10）。

---

## 6. 待实施时核对项

- `aggTrades` WS/REST 端点与历史可用范围，实施时按币安官方文档核对（需求 §5.1 亦注明）。
- `arrival_time` 回放因原始文件缺 `received_at`，本版仅能 event_time（AC-18 不要求两模式结果相同）。