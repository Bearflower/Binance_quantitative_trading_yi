# QuantView Strategy Detail Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将策略详情页重构为现代浅色分析工作台，让日、周、月作为全局筛选器同步驱动当前策略指标、全部策略列表、趋势占位与交易对明细。

**Architecture:** 使用 FastAPI 在本地同源提供 `/api` 与详情页静态资源，避免 8099 静态服务器把 API 路径回退到 HTML。前端拆成语义化 `detail.html`、页面专用 `detail.css`、纯状态工具 `detail-state.js` 和页面控制器 `detail.js`；通过请求版本号隔离过期响应。后端先发布可空的策略分析契约，只有真实策略级时间序列可用时才返回趋势与回撤，否则前端明确显示 `—` 和空状态。

**Tech Stack:** FastAPI、Pydantic、原生 HTML/CSS/JavaScript、ECharts、Node.js `node:test`、pytest、Codex in-app browser

---

## File Structure

- Modify: `dashboard/backend/main.py` — 本地同源服务入口，在 API 路由之后挂载前端静态目录。
- Modify: `dashboard/backend/models/schemas.py` — 定义可空风险指标与策略级表现点契约。
- Create: `dashboard/backend/tests/test_dashboard_static.py` — 验证 `/api/health` 为 JSON、`/detail.html` 为 HTML。
- Create: `dashboard/backend/tests/test_strategy_analytics_schema.py` — 验证分析字段的空值和真实值序列化。
- Modify: `dashboard/frontend/detail.html` — 仅保留语义化页面骨架和资源引用。
- Create: `dashboard/frontend/css/detail.css` — 详情页视觉、响应式与可访问性样式。
- Create: `dashboard/frontend/js/detail-state.js` — URL、周期、请求版本、格式化等纯函数。
- Create: `dashboard/frontend/js/detail.js` — 数据请求、模块渲染、事件与 ECharts 生命周期。
- Create: `dashboard/frontend/tests/detail-state.test.js` — 纯状态和格式化逻辑测试。
- Create: `dashboard/frontend/tests/detail-contract.test.js` — HTML 结构和资源边界回归测试。
- Modify: `dashboard/README.md` — 增加本地 8099 启动与详情页验证方法。

现有 `dashboard/frontend/css/style.css`、`dashboard/frontend/js/api.js`、`dashboard/frontend/js/config.js` 保持不动，减少与工作区现有改动冲突。

### Task 1: 建立 8099 同源服务基线

**Files:**
- Create: `dashboard/backend/tests/test_dashboard_static.py`
- Modify: `dashboard/backend/main.py`
- Modify: `dashboard/README.md`

- [ ] **Step 1: 写失败测试，锁定 API 与静态页的 Content-Type**

```python
from fastapi.testclient import TestClient

from dashboard.backend.main import app


client = TestClient(app)


def test_health_endpoint_returns_json():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == 0


def test_detail_page_is_served_as_html():
    response = client.get("/detail.html?strategy=hrs&type=daily")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "策略详情" in response.text
```

- [ ] **Step 2: 运行测试并确认静态页测试失败**

Run: `pytest dashboard/backend/tests/test_dashboard_static.py -q`

Expected: `test_health_endpoint_returns_json` 通过，`test_detail_page_is_served_as_html` 返回 404。

- [ ] **Step 3: 在所有 API 路由之后挂载前端目录**

在 `dashboard/backend/main.py` 引入 `StaticFiles` 和前端路径，并确保挂载语句位于 `/api`、异常处理器和 API 根路径定义之后：

```python
from fastapi.staticfiles import StaticFiles

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# 必须最后挂载，避免静态路由吞掉 /api。
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="dashboard")
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `pytest dashboard/backend/tests/test_dashboard_static.py -q`

Expected: `2 passed`。

- [ ] **Step 5: 在 README 写明本地运行方式**

````markdown
### 本地详情页

```bash
uvicorn dashboard.backend.main:app --host 127.0.0.1 --port 8099
```

打开 `http://localhost:8099/detail.html?strategy=hrs&type=daily`。前端和 `/api` 由同一进程提供，避免静态服务器将 API 请求回退成 HTML。
````

- [ ] **Step 6: 提交基线服务改动**

```bash
git add dashboard/backend/main.py dashboard/backend/tests/test_dashboard_static.py dashboard/README.md
git commit -m "fix(dashboard): serve local UI and API from one origin"
```

### Task 2: 发布诚实的策略分析数据契约

**Files:**
- Create: `dashboard/backend/tests/test_strategy_analytics_schema.py`
- Modify: `dashboard/backend/models/schemas.py`

- [ ] **Step 1: 写失败测试，要求缺失分析数据序列化为 null 和空数组**

```python
from dashboard.backend.models.schemas import StrategyDetailData, StrategySummary


BASE = {
    "id": "hrs",
    "name": "HRS策略",
    "updated_at": "2026-08-21T09:00:00+08:00",
}


def test_strategy_summary_uses_null_for_unknown_drawdown():
    payload = StrategySummary(id="hrs", name="HRS策略").model_dump()
    assert payload["max_drawdown"] is None


def test_strategy_detail_defaults_to_empty_performance_series():
    payload = StrategyDetailData(**BASE).model_dump()
    assert payload["max_drawdown"] is None
    assert payload["profit_factor"] is None
    assert payload["commission_ratio"] is None
    assert payload["performance_points"] == []


def test_strategy_detail_serializes_real_performance_points():
    payload = StrategyDetailData(
        **BASE,
        max_drawdown=-12.5,
        performance_points=[
            {"date": "2026-08-21", "cumulative_pnl": "18.2000", "drawdown": "-2.0000"}
        ],
    ).model_dump()
    assert payload["max_drawdown"] == -12.5
    assert payload["performance_points"][0]["cumulative_pnl"] == "18.2000"
```

- [ ] **Step 2: 运行测试并确认字段不存在**

Run: `pytest dashboard/backend/tests/test_strategy_analytics_schema.py -q`

Expected: FAIL，提示 `max_drawdown` 或 `performance_points` 不存在。

- [ ] **Step 3: 添加可空指标和表现点模型**

```python
class StrategyPerformancePoint(BaseModel):
    """单个策略在所选周期内的真实累计收益与回撤点。"""

    date: str = Field(..., description="日期")
    cumulative_pnl: str = Field(..., description="累计净盈亏")
    drawdown: str = Field(..., description="相对前期峰值的回撤")
```

向 `StrategySummary` 添加：

```python
max_drawdown: Optional[float] = Field(None, description="最大回撤；无可靠序列时为空")
```

向 `StrategyDetailData` 添加：

```python
max_drawdown: Optional[float] = Field(None, description="最大回撤；无可靠序列时为空")
profit_factor: Optional[float] = Field(None, description="利润因子；无可靠数据时为空")
commission_ratio: Optional[float] = Field(None, description="手续费占比；无可靠口径时为空")
performance_points: List[StrategyPerformancePoint] = Field(
    default_factory=list,
    description="策略级累计净盈亏与回撤序列",
)
```

- [ ] **Step 4: 运行 schema 测试**

Run: `pytest dashboard/backend/tests/test_strategy_analytics_schema.py -q`

Expected: `3 passed`。

- [ ] **Step 5: 提交分析契约**

```bash
git add dashboard/backend/models/schemas.py dashboard/backend/tests/test_strategy_analytics_schema.py
git commit -m "feat(dashboard): add nullable strategy analytics contract"
```

### Task 3: 建立可测试的页面状态模型

**Files:**
- Create: `dashboard/frontend/js/detail-state.js`
- Create: `dashboard/frontend/tests/detail-state.test.js`

- [ ] **Step 1: 写失败测试覆盖 URL、非法周期和过期请求**

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const state = require('../js/detail-state.js');

test('从 URL 恢复策略与周报周期', () => {
  assert.deepEqual(
    state.readPageState('?strategy=hrs&type=weekly'),
    { strategyId: 'hrs', reportType: 'weekly' },
  );
});

test('非法周期回退为 daily', () => {
  assert.equal(state.normalizeReportType('yearly'), 'daily');
});

test('更新 URL 时同时保留策略和周期', () => {
  assert.equal(
    state.buildSearchParams('new_coin', 'monthly'),
    '?strategy=new_coin&type=monthly',
  );
});

test('只有最新请求版本可以提交渲染', () => {
  assert.equal(state.isCurrentRequest(3, 3), true);
  assert.equal(state.isCurrentRequest(2, 3), false);
});

test('缺失指标格式化为破折号', () => {
  assert.equal(state.formatMetric(null, 'percent'), '—');
  assert.equal(state.formatMetric(undefined, 'number'), '—');
});
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `node --test dashboard/frontend/tests/detail-state.test.js`

Expected: FAIL with `Cannot find module '../js/detail-state.js'`。

- [ ] **Step 3: 实现无 DOM 依赖的 UMD 状态工具**

```javascript
(function exposeDetailState(root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.DetailState = value;
}(globalThis, function createDetailState() {
  const REPORT_TYPES = new Set(['daily', 'weekly', 'monthly']);

  function normalizeReportType(value) {
    return REPORT_TYPES.has(value) ? value : 'daily';
  }

  function readPageState(search) {
    const params = new URLSearchParams(search);
    return {
      strategyId: params.get('strategy') || '',
      reportType: normalizeReportType(params.get('type')),
    };
  }

  function buildSearchParams(strategyId, reportType) {
    const params = new URLSearchParams();
    params.set('strategy', strategyId);
    params.set('type', normalizeReportType(reportType));
    return `?${params.toString()}`;
  }

  function isCurrentRequest(version, currentVersion) {
    return version === currentVersion;
  }

  function formatMetric(value, kind) {
    if (value === null || value === undefined || value === '') return '—';
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    if (kind === 'percent') return `${number.toFixed(1)}%`;
    return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(number);
  }

  return { normalizeReportType, readPageState, buildSearchParams, isCurrentRequest, formatMetric };
}));
```

- [ ] **Step 4: 运行 Node 测试**

Run: `node --test dashboard/frontend/tests/detail-state.test.js`

Expected: `5 passed`。

- [ ] **Step 5: 提交状态工具**

```bash
git add dashboard/frontend/js/detail-state.js dashboard/frontend/tests/detail-state.test.js
git commit -m "test(dashboard): define detail page state model"
```

### Task 4: 重建语义化详情页骨架

**Files:**
- Create: `dashboard/frontend/tests/detail-contract.test.js`
- Modify: `dashboard/frontend/detail.html`

- [ ] **Step 1: 写失败的页面契约测试**

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'detail.html'), 'utf8');

test('详情页使用独立资源且没有内联业务脚本和样式', () => {
  assert.match(html, /css\/detail\.css/);
  assert.match(html, /js\/detail-state\.js/);
  assert.match(html, /js\/detail\.js/);
  assert.doesNotMatch(html, /<style>/);
  assert.doesNotMatch(html, /<script>\s*let detailType/);
});

test('页面包含全局周期、全策略、趋势和交易对区域', () => {
  assert.match(html, /id="period-switcher"/);
  assert.match(html, /id="strategy-list"/);
  assert.match(html, /id="performance-chart"/);
  assert.match(html, /id="symbol-table-body"/);
});

test('页面具备主内容跳转与状态播报', () => {
  assert.match(html, /href="#main-content"/);
  assert.match(html, /aria-live="polite"/);
});
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `node --test dashboard/frontend/tests/detail-contract.test.js`

Expected: FAIL，缺少独立资源和目标区域。

- [ ] **Step 3: 用语义化骨架替换内联页面**

`detail.html` 必须包含以下稳定结构：

```html
<a class="skip-link" href="#main-content">跳到主要内容</a>
<header class="detail-nav">...</header>
<main id="main-content" class="detail-page">
  <section class="strategy-heading" aria-labelledby="strategy-title">...</section>
  <section class="metric-grid" aria-label="当前策略核心指标">...</section>
  <div class="analysis-grid">
    <section class="panel performance-panel" aria-labelledby="performance-title">
      <div id="performance-chart" role="img" aria-label="策略累计收益与回撤"></div>
      <p id="performance-summary" class="chart-summary"></p>
    </section>
    <section class="panel strategy-panel" aria-labelledby="strategy-list-title">
      <div id="strategy-list"></div>
    </section>
  </div>
  <section class="panel symbol-panel" aria-labelledby="symbol-title">
    <tbody id="symbol-table-body"></tbody>
  </section>
</main>
<div id="page-announcer" class="sr-only" aria-live="polite"></div>
<script src="js/config.js"></script>
<script src="js/api.js"></script>
<script src="js/vendor/echarts.min.js"></script>
<script src="js/detail-state.js"></script>
<script src="js/detail.js"></script>
```

周期按钮使用 `role="radio"`、`aria-checked` 和 `data-range`；全策略列表使用原生 `<button>` 行，交易对明细使用原生 `<table>`。删除重复 `detail-sub`、重复变量声明、emoji 图标和内联 `style`。

- [ ] **Step 4: 运行页面契约测试**

Run: `node --test dashboard/frontend/tests/detail-contract.test.js`

Expected: `3 passed`。

- [ ] **Step 5: 提交语义化骨架**

```bash
git add dashboard/frontend/detail.html dashboard/frontend/tests/detail-contract.test.js
git commit -m "refactor(dashboard): rebuild strategy detail markup"
```

### Task 5: 实现现代浅色分析工作台样式

**Files:**
- Create: `dashboard/frontend/css/detail.css`

- [ ] **Step 1: 添加页面级设计令牌和桌面布局**

```css
.detail-page-shell {
  --detail-bg: #f4f7fb;
  --detail-surface: #ffffff;
  --detail-border: #dfe7f1;
  --detail-primary: #2457d6;
  --detail-positive: #087f5b;
  --detail-negative: #c33d4a;
  --detail-text: #172033;
  --detail-muted: #68758a;
  min-height: 100vh;
  background: var(--detail-bg);
  color: var(--detail-text);
}

.analysis-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(360px, 0.85fr);
  gap: 20px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}
```

- [ ] **Step 2: 添加全策略行、表格、状态和具体动效属性**

```css
.strategy-row {
  width: 100%;
  border: 0;
  border-left: 3px solid transparent;
  background: transparent;
  transition: background-color 160ms ease, border-color 160ms ease;
}

.strategy-row[aria-current="true"] {
  border-left-color: var(--detail-primary);
  background: #eef4ff;
}

.strategy-row:focus-visible,
.period-button:focus-visible,
.retry-button:focus-visible {
  outline: 3px solid rgb(36 87 214 / 28%);
  outline-offset: 2px;
}
```

- [ ] **Step 3: 添加 1024、768、375 响应式规则与 reduced motion**

```css
@media (max-width: 1024px) {
  .analysis-grid { grid-template-columns: 1fr; }
  .metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

@media (max-width: 768px) {
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .strategy-heading { align-items: flex-start; flex-direction: column; }
}

@media (max-width: 420px) {
  .metric-grid { grid-template-columns: 1fr; }
  .detail-page { padding-inline: 16px; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; }
}
```

- [ ] **Step 4: 静态扫描禁止项**

Run: `rg -n "transition:\s*all|<style>|style=" dashboard/frontend/detail.html dashboard/frontend/css/detail.css`

Expected: 无输出。

- [ ] **Step 5: 提交页面样式**

```bash
git add dashboard/frontend/css/detail.css
git commit -m "feat(dashboard): style the strategy analysis workspace"
```

### Task 6: 实现全局周期和全策略联动

**Files:**
- Modify: `dashboard/frontend/js/detail-state.js`
- Modify: `dashboard/frontend/tests/detail-state.test.js`
- Create: `dashboard/frontend/js/detail.js`

- [ ] **Step 1: 补充格式化与安全文本测试**

```javascript
test('金额保留符号且非法数值不污染页面', () => {
  assert.equal(state.formatSignedAmount('12.5'), '+12.50');
  assert.equal(state.formatSignedAmount('-2'), '-2.00');
  assert.equal(state.formatSignedAmount('bad'), '—');
});

test('请求错误只转换为可展示消息', () => {
  assert.equal(state.errorMessage(new Error('网络错误')), '网络错误');
  assert.equal(state.errorMessage({}), '数据加载失败，请重试');
});
```

- [ ] **Step 2: 运行测试并确认新函数缺失**

Run: `node --test dashboard/frontend/tests/detail-state.test.js`

Expected: FAIL，`formatSignedAmount` 或 `errorMessage` 不存在。

- [ ] **Step 3: 实现格式化辅助函数并导出**

```javascript
function formatSignedAmount(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  const prefix = number > 0 ? '+' : '';
  return `${prefix}${number.toFixed(2)}`;
}

function errorMessage(error) {
  return typeof error?.message === 'string' && error.message
    ? error.message
    : '数据加载失败，请重试';
}
```

- [ ] **Step 4: 在 `detail.js` 建立单一页面状态和请求版本**

```javascript
const pageState = {
  ...DetailState.readPageState(window.location.search),
  requestVersion: 0,
  strategies: [],
  chart: null,
};

function beginRequest() {
  pageState.requestVersion += 1;
  return pageState.requestVersion;
}

function commitIfCurrent(version, render) {
  if (DetailState.isCurrentRequest(version, pageState.requestVersion)) render();
}
```

- [ ] **Step 5: 实现周期切换的一次批量刷新**

```javascript
async function refreshPage() {
  const version = beginRequest();
  setPeriodButtons(pageState.reportType);
  setLoading(true);

  const strategiesPromise = api.getStrategies(pageState.reportType);
  const detailPromise = pageState.strategyId
    ? api.getStrategyDetail(pageState.strategyId, pageState.reportType)
    : Promise.resolve(null);

  const [strategiesResult, detailResult] = await Promise.allSettled([
    strategiesPromise,
    detailPromise,
  ]);

  commitIfCurrent(version, () => {
    applyStrategyResult(strategiesResult);
    normalizeSelectedStrategy();
    applyDetailResult(detailResult, version);
    syncUrl();
    setLoading(false);
  });
}
```

若 URL 没有策略，`normalizeSelectedStrategy()` 选择全策略列表第一项并继续加载详情；若详情请求因策略归一化发生变化，只发起一次新的当前策略请求。

- [ ] **Step 6: 实现策略列表客观指标与点击切换**

每行只渲染：名称、净收益、胜率、平仓数、最大回撤。使用 `textContent` 或 DOM API 写入后端文本，禁止将策略名拼进 `innerHTML`。

```javascript
function selectStrategy(strategyId) {
  if (strategyId === pageState.strategyId) return;
  pageState.strategyId = strategyId;
  syncUrl();
  renderStrategySelection();
  refreshSelectedStrategy();
}
```

- [ ] **Step 7: 实现模块级失败与重试**

核心指标、全策略列表、趋势区、交易对表分别提供 `renderLoading`、`renderEmpty`、`renderError`。重试按钮绑定当前 `strategyId` 和 `reportType`；失败模块不清除其他模块已经成功的内容。

- [ ] **Step 8: 运行状态和页面契约测试**

Run: `node --test dashboard/frontend/tests/detail-state.test.js dashboard/frontend/tests/detail-contract.test.js`

Expected: 全部通过。

- [ ] **Step 9: 提交页面控制器**

```bash
git add dashboard/frontend/js/detail-state.js dashboard/frontend/js/detail.js dashboard/frontend/tests/detail-state.test.js
git commit -m "feat(dashboard): synchronize strategy detail period state"
```

### Task 7: 渲染真实趋势或明确空状态

**Files:**
- Modify: `dashboard/frontend/js/detail.js`

- [ ] **Step 1: 仅接受策略详情的 `performance_points`**

```javascript
function renderPerformance(detail) {
  const points = Array.isArray(detail?.performance_points)
    ? detail.performance_points
    : [];
  if (points.length === 0) {
    disposeChart();
    renderPerformanceEmpty('当前周期暂无可验证的策略级收益序列');
    return;
  }
  renderPerformanceChart(points);
}
```

不得调用账户级 `/api/trend` 填充当前策略图表。

- [ ] **Step 2: 用 ECharts 绘制累计收益和回撤**

```javascript
pageState.chart.setOption({
  animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  tooltip: { trigger: 'axis' },
  legend: { data: ['累计净收益', '回撤'] },
  xAxis: { type: 'category', data: points.map((point) => point.date) },
  yAxis: { type: 'value', axisLabel: { formatter: '{value} USDT' } },
  series: [
    { name: '累计净收益', type: 'line', showSymbol: false, data: points.map((point) => Number(point.cumulative_pnl)) },
    { name: '回撤', type: 'line', areaStyle: {}, showSymbol: false, data: points.map((point) => Number(point.drawdown)) },
  ],
});
```

- [ ] **Step 3: 添加文字摘要与 resize 清理**

图表下方用文字报告期末累计收益和最大回撤；窗口 resize 使用已存在图表实例的 `resize()`，页面卸载调用 `dispose()`，避免重复创建实例。

- [ ] **Step 4: 运行前端测试与禁止项扫描**

Run: `node --test dashboard/frontend/tests/*.test.js`

Expected: 全部通过。

Run: `rg -n "getTrend\(|transition:\s*all|innerHTML.*strategy\.name" dashboard/frontend/detail.html dashboard/frontend/js/detail.js dashboard/frontend/css/detail.css`

Expected: 无输出。

- [ ] **Step 5: 提交趋势与风险呈现**

```bash
git add dashboard/frontend/js/detail.js
git commit -m "feat(dashboard): render verified strategy performance only"
```

### Task 8: 完成自动化与浏览器验收

**Files:**
- Modify: `dashboard/README.md`（仅在验证命令需要修正时）

- [ ] **Step 1: 运行 Dashboard 后端测试**

Run: `pytest dashboard/backend/tests -q`

Expected: 全部通过，无新增 warning 或 error。

- [ ] **Step 2: 运行详情页前端测试**

Run: `node --test dashboard/frontend/tests/*.test.js`

Expected: 全部通过。

- [ ] **Step 3: 启动本地同源服务**

Run: `uvicorn dashboard.backend.main:app --host 127.0.0.1 --port 8099`

Expected: 8099 端口监听，`GET /api/health` 返回 JSON。

- [ ] **Step 4: 验证 URL 和 API Content-Type**

Run: `curl -fsS -D - 'http://127.0.0.1:8099/api/health'`

Expected: `Content-Type: application/json` 且业务码为 `0`。

Run: `curl -fsS -D - 'http://127.0.0.1:8099/detail.html?strategy=hrs&type=daily'`

Expected: `Content-Type: text/html`，页面引用 `detail.css` 与 `detail.js`。

- [ ] **Step 5: 在 Codex 浏览器验证全局周期联动**

依次打开 daily、weekly、monthly，检查：

- 所有发出的策略列表、详情请求均使用同一 `type`；
- URL 与选中的周期一致；
- 当前策略在全策略列表中高亮；
- 点击其他策略后 `type` 不变；
- 快速点击日、周、月不会回写旧数据；
- 最大回撤和趋势无真实数据时显示 `—`/解释性空状态；
- 控制台没有语法错误、重复 ID 或未处理 Promise。

- [ ] **Step 6: 验证响应式和键盘操作**

在 375、768、1024、1440 宽度截图检查布局；使用 Tab、Enter/Space 操作周期按钮、策略行和重试按钮；检查 focus-visible 与 reduced motion。

- [ ] **Step 7: 检查工作区改动边界**

Run: `git status --short`

Expected: 本功能只新增或修改本计划列出的文件；用户原有未提交改动保持存在且未被覆盖。

- [ ] **Step 8: 最终提交**

```bash
git add dashboard/README.md dashboard/backend/main.py dashboard/backend/models/schemas.py dashboard/backend/tests dashboard/frontend/detail.html dashboard/frontend/css/detail.css dashboard/frontend/js/detail-state.js dashboard/frontend/js/detail.js dashboard/frontend/tests
git commit -m "feat(dashboard): redesign multi-strategy detail workspace"
```

最终提交前若前面已按任务逐次提交，本步骤只提交尚未提交的验证修正，不创建空提交。
