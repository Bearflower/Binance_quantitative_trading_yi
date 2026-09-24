/**
 * Dashboard 主逻辑
 * 页面交互和数据加载
 */

// 当前数据类型（daily/weekly/monthly/yearly）
let currentType = 'daily';

// 图表实例
let trendChart = null;
let posUtilTrendChart = null;

/**
 * 初始化页面
 */
async function init() {
    console.log('Dashboard 初始化中...');

    // 设置事件监听
    setupEventListeners();

    // 加载数据
    await loadData();

    // 独立板块：AI 监控 / 风控（与 overview 并行加载，失败仅该卡回退 "--"）
    loadAiMonitor();
    loadRisk();
}

/**
 * 设置事件监听
 */
function setupEventListeners() {
    // 顶级导航：日/周/月切换（overview + 趋势图联动）
    document.querySelectorAll('.toggle-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            // 更新按钮状态
            document.querySelectorAll('.toggle-btn').forEach(b => {
                b.classList.remove('active');
                b.setAttribute('aria-checked', 'false');
            });

            btn.classList.add('active');
            btn.setAttribute('aria-checked', 'true');

            // 更新数据类型
            currentType = btn.dataset.range;

            // 重新加载数据
            await loadData();
        });
    });

    // 收益率周期切换已移除：收益率模块改为跟随顶部日/周/月/年切换（currentType）
    // 策略卡片点击
    document.querySelectorAll('.strategy-card').forEach(card => {
        card.addEventListener('click', (e) => {
            // 如果点击的是详情链接，让浏览器默认行为处理
            if (e.target.closest('.strategy-detail-link')) return;

            const strategy = card.dataset.strategy;
            window.location.href = `detail.html?strategy=${strategy}&type=${currentType}`;
        });

        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                const strategy = card.dataset.strategy;
                window.location.href = `detail.html?strategy=${strategy}&type=${currentType}`;
            }
        });
    });
}

/**
 * 加载数据
 */
async function loadData() {
    try {
        // 显示加载状态
        showLoading();

        // 并行加载数据
        const [overview, trend] = await Promise.all([
            api.getOverview(currentType),
            api.getTrend(currentType, DashboardConfig.trend.defaultDays)
        ]);

        // 更新合约账户净资产（实时快照，不随日/周/月切换）
        await loadAccountEquity();

        // 更新收益率（跟随顶部日/周/月/年切换）
        await loadReturns(currentType);

        // 更新总览
        updateOverview(overview);

        // 更新策略卡片
        updateStrategyCards(overview.strategies);

        // 更新趋势图
        if (trendChart) {
            trendChart.dispose();
        }
        trendChart = createTrendChart('trend-chart', trend);

        // 更新时间
        updateTimestamp();

        // 隐藏加载状态
        hideLoading();

    } catch (error) {
        console.error('数据加载失败:', error);
        showError(error.message);
    }
}

/**
 * 加载并展示合约账户净资产
 * 实时快照（缓存由后端配置 cache_ttl_account 控制），不随日/周/月切换变化。
 */
async function loadAccountEquity() {
    const equityEl = document.querySelector('#account-equity');
    const timeEl = document.querySelector('#account-equity-time');
    const availEl = document.querySelector('#account-equity-avail');
    const posEl = document.querySelector('#current-positions');
    if (!equityEl) return;

    try {
        const data = await api.getAccountEquity();
        const value = parseFloat(data.total_equity);
        equityEl.textContent = value.toLocaleString('zh-CN', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
        equityEl.className = 'equity-value ' + (value >= 0 ? 'positive' : 'negative');
        if (timeEl) {
            timeEl.textContent = `截止 ${formatEquityTime(data.updated_at)}`;
        }
        if (availEl) {
            const avail = parseFloat(data.available_balance);
            availEl.textContent = `可用 ${avail.toLocaleString('zh-CN', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2
            })}`;
        }
        if (posEl) {
            posEl.textContent = (data.open_positions ?? 0).toLocaleString();
        }
    } catch (error) {
        console.warn('账户净资产加载失败:', error);
        equityEl.textContent = '--';
        equityEl.className = 'equity-value';
        if (timeEl) timeEl.textContent = '--';
        if (availEl) availEl.textContent = '可用 --';
        if (posEl) posEl.textContent = '--';
    }
}

/**
 * 格式化净资产更新时间，如 "08/21 12:34"
 */
function formatEquityTime(isoStr) {
    const date = new Date(isoStr);
    if (isNaN(date.getTime())) return isoStr;
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 更新总览
 */
function updateOverview(data) {
    const unrealizedValue = parseFloat(data.total_unrealized_pnl || 0);
    const realizedValue = parseFloat(data.total_pnl);

    // 总盈亏 = 已实现盈亏 + 浮动盈亏（账户真实盈亏）
    const pnlValue = realizedValue + unrealizedValue;
    const totalPnl = document.querySelector('#total-pnl');
    totalPnl.textContent = formatNumber(pnlValue);
    totalPnl.className = 'stat-value ' + (pnlValue >= 0 ? 'positive' : 'negative');

    const winRate = document.querySelector('#win-rate');
    if (winRate) winRate.textContent = formatPercent(data.win_rate);

    const realized = document.querySelector('#realized-pnl');
    if (realized) {
        realized.textContent = formatNumber(realizedValue);
        realized.className = 'stat-value ' + (realizedValue >= 0 ? 'positive' : 'negative');
    }

    const unrealized = document.querySelector('#total-unrealized-pnl');
    if (unrealized) {
        unrealized.textContent = formatNumber(unrealizedValue);
        unrealized.className = 'stat-value ' + (unrealizedValue >= 0 ? 'positive' : 'negative');
    }

    const commission = document.querySelector('#total-commission');
    if (commission) {
        const commValue = parseFloat(data.total_commission);
        commission.textContent = formatNumber(commValue);
        commission.className = 'stat-value ' + (commValue < 0 ? 'red' : 'positive');
    }
}

/**
 * 更新策略卡片
 */
function updateStrategyCards(strategies) {
    strategies.forEach((strategy) => {
        const card = document.querySelector(`.strategy-card[data-strategy="${strategy.id}"]`);
        if (!card) return;

        const name = card.querySelector('.strategy-name');
        name.textContent = strategy.name;

        const pnl = card.querySelector('[data-metric="pnl"] .value');
        const pnlValue = parseFloat(strategy.total_pnl);
        pnl.textContent = formatNumber(pnlValue);
        pnl.className = 'value ' + (pnlValue >= 0 ? 'positive' : 'negative');

        const winRate = card.querySelector('[data-metric="win_rate"] .value');
        if (winRate) winRate.textContent = formatPercent(strategy.win_rate);

        const orderCount = card.querySelector('[data-metric="order_count"] .value');
        if (orderCount) orderCount.textContent = strategy.order_count.toLocaleString();

        // 持仓量 / 持仓保证金（无上报时后端回 0，展示 0 / 0.00 保持布局稳定）
        const openCount = card.querySelector('[data-metric="open_position_count"] .value');
        if (openCount) openCount.textContent = (strategy.open_position_count ?? 0).toLocaleString();

        const openMargin = card.querySelector('[data-metric="open_margin"] .value');
        if (openMargin) {
            const margin = parseFloat(strategy.open_margin ?? 0);
            openMargin.textContent = (isNaN(margin) ? 0 : margin).toLocaleString('zh-CN', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2
            });
        }
    });
}

/**
 * 加载并展示账户收益率（随周期联动：收益率 / 期初净资产 / 本期净盈亏）
 */
async function loadReturns(period) {
    const yieldEl = document.querySelector('#returns-yield');
    const startEl = document.querySelector('#returns-start');
    const pnlEl = document.querySelector('#returns-pnl');
    const hintEl = document.querySelector('#returns-hint');
    if (!yieldEl) return;

    yieldEl.classList.add('updating');

    try {
        const data = await api.getAccountReturns(period);
        if (data.yield_unavailable) {
            // 历史快照积累中：收益率与期初/盈亏一律 "--"，并提示积累中
            yieldEl.textContent = '--';
            yieldEl.className = 'returns-value';
            if (startEl) startEl.textContent = '--';
            if (pnlEl) pnlEl.textContent = '--';
            if (hintEl) hintEl.textContent = '历史数据积累中';
        } else {
            yieldEl.textContent = data.yield_text || formatSignedNumber(data.yield);
            const pnlValue = parseFloat(data.period_pnl);
            yieldEl.className = 'returns-value ' + (pnlValue >= 0 ? 'positive' : 'negative');
            if (startEl) startEl.textContent = data.period_start_equity != null ? formatUsdt(data.period_start_equity) : '--';
            if (pnlEl) pnlEl.textContent = data.period_pnl != null ? formatUsdt(data.period_pnl) : '--';
            if (hintEl) hintEl.textContent = data.snapshot_date ? `快照日期 ${data.snapshot_date}` : '';
        }
    } catch (error) {
        console.warn('收益率加载失败:', error);
        yieldEl.textContent = '--';
        yieldEl.className = 'returns-value';
        if (startEl) startEl.textContent = '--';
        if (pnlEl) pnlEl.textContent = '--';
        if (hintEl) hintEl.textContent = '';
    } finally {
        yieldEl.classList.remove('updating');
    }
}

/**
 * 加载 AI 监控数据（最近优化建议 + 月度资金分配 + 占用比趋势）
 */
async function loadAiMonitor() {
    const allocationEl = document.querySelector('#capital-allocation');
    const recEl = document.querySelector('#recommendations');
    if (!allocationEl && !recEl) return;

    try {
        const data = await api.getAiMonitor();
        renderCapitalAllocation(allocationEl, data.capital_allocation);
        renderSuggestions(recEl, data.recent_suggestions);
        await loadPositionUtilTrend();
    } catch (error) {
        console.warn('AI 监控加载失败:', error);
        if (allocationEl) allocationEl.textContent = '--';
        if (recEl) recEl.textContent = '--';
    }
}

/**
 * 加载并渲染 各策略占用比历史趋势（近30天按天聚合）
 */
async function loadPositionUtilTrend() {
    const trendEl = document.querySelector('#position-utilization-trend-chart');
    if (!trendEl) return;
    try {
        // 占用比对账每小时更新一次，趋势取近30天
        const trendData = await api.getPositionUtilizationTrend(30);
        if (posUtilTrendChart) {
            posUtilTrendChart.dispose();
            posUtilTrendChart = null;
        }
        if (trendData && trendData.dates && trendData.dates.length) {
            posUtilTrendChart = createPositionUtilTrendChart('position-utilization-trend-chart', trendData);
        } else {
            trendEl.innerHTML = '<div class="ai-empty">暂无占用比趋势数据</div>';
        }
    } catch (error) {
        console.warn('占用比趋势加载失败:', error);
        trendEl.innerHTML = '<div class="ai-empty">占用比趋势加载失败</div>';
    }
}

/**
 * 渲染月度资金分配（本期 active 各策略分配金额 / 比例 / 收益率 / 排名）
 */
function renderCapitalAllocation(container, allocation) {
    if (!container) return;
    if (!allocation || !allocation.entries || allocation.entries.length === 0) {
        container.innerHTML = '<div class="ai-empty">暂无资金分配</div>';
        return;
    }
    const head = allocation.month
        ? `${allocation.month} 期 · 总资金 ${formatUsdt(allocation.total_capital)} USDT`
        : `总资金 ${formatUsdt(allocation.total_capital)} USDT`;
    const rows = allocation.entries.map(entry => {
        const amount = formatUsdt(entry.allocated_amount);
        const ratio = entry.allocated_ratio != null
            ? (parseFloat(entry.allocated_ratio) * 100).toFixed(0) + '%'
            : '--';
        // 占用比 = 该策略持仓保证金 / 分配金额，由后台持仓对账任务落库
        const occ = entry.occupied_ratio != null
            ? (parseFloat(entry.occupied_ratio) * 100).toFixed(1) + '%'
            : '--';
        // 占用金额 = 该策略当前实际占用的保证金（可能为 0 或 null，null 时 formatUsdt 显示 '--'）
        const occupiedAmount = entry.occupied_amount != null ? formatUsdt(entry.occupied_amount) : '--';
        return `<div class="allocation-row">
            <span class="allocation-name">${escapeHtml(entry.strategy_name || '--')}</span>
            <span class="allocation-rank">#${entry.rank != null ? entry.rank : '-'}</span>
            <span class="allocation-ratio">${ratio}</span>
            <span class="allocation-amount">${amount}</span>
            <span class="allocation-occupied">${occupiedAmount}</span>
            <span class="allocation-return occupied">${occ}</span>
        </div>`;
    }).join('');
    container.innerHTML = `<div class="allocation-head">${head}</div>
        <div class="allocation-table">
            <div class="allocation-thead">
                <span>策略</span><span>排名</span><span>分配比例</span><span>分配金额</span><span>占用金额</span><span>占用比</span>
            </div>
            ${rows}
        </div>`;
}

/**
 * 渲染最近优化建议（状态与 ai_tuner 周度调优结果一致）
 * status: success=已调整 / skip=无需调整 / error=异常，见后端 _get_recent_suggestions
 */
function renderSuggestions(container, suggestions) {
    if (!container) return;
    if (!suggestions || suggestions.length === 0) {
        container.innerHTML = '<div class="ai-empty">暂无建议</div>';
        return;
    }
    const statusMap = {
        success: { label: '已调整', cls: 'tag-applied' },
        skip:    { label: '无需调整', cls: 'tag-skip' },
        error:   { label: '异常', cls: 'tag-rejected' },
    };
    container.innerHTML = suggestions.map(sg => {
        const st = statusMap[sg.status] || { label: sg.status || '--', cls: 'tag-skip' };
        const adjustments = (sg.adjustments && sg.adjustments.length)
            ? sg.adjustments.map(escapeHtml).join(' · ')
            : '--';
        return `<div class="suggestion-row">
            <div class="suggestion-head">
                <span class="suggestion-name">${escapeHtml(sg.strategy_name || sg.strategy_id || '--')}</span>
                <span class="suggestion-time">${formatDateTime(sg.created_at)}</span>
            </div>
            <div class="suggestion-adjustments">${adjustments}</div>
            <div class="suggestion-foot">
                <span class="tag ${st.cls}">${st.label}</span>
            </div>
        </div>`;
    }).join('');
}

/**
 * 加载风控数据（持仓占用 / 阈值预警 / 止损统计）
 */
async function loadRisk() {
    const totalEl = document.querySelector('#risk-total-margin');
    if (!totalEl) return;

    try {
        const data = await api.getRisk();
        renderRisk(data);
    } catch (error) {
        console.warn('风控加载失败:', error);
        totalEl.textContent = '--';
        const limitEl = document.querySelector('#risk-margin-limit');
        if (limitEl) limitEl.textContent = '--';
    }
}

/**
 * 渲染风控数字卡 + 占用率进度条 + 预警
 */
function renderRisk(data) {
    const setText = (id, text) => {
        const el = document.querySelector(id);
        if (el) el.textContent = text;
    };
    // 进度条颜色：threshold_exceeded 红色 / approaching_threshold 橙色 / 否则绿色（阈值由后端配置判定，前端不硬编码）
    const barCls = data.threshold_exceeded ? 'bar-danger' : (data.approaching_threshold ? 'bar-warn' : 'bar-normal');
    const setBar = (id, pct) => {
        const bar = document.querySelector(id);
        if (!bar) return;
        const safePct = (pct == null || isNaN(pct)) ? 0 : Math.max(0, Math.min(100, pct));
        bar.style.width = safePct + '%';
        bar.className = 'progress-bar ' + barCls;
    };

    setText('#risk-total-margin', formatUsdt(data.total_position_margin));

    // 持仓上限：为空（null）时显示 "--" 并提示未配置
    if (data.margin_limit == null) {
        setText('#risk-margin-limit', '--');
        const sub = document.querySelector('#risk-margin-limit-sub');
        if (sub) sub.textContent = '未配置持仓上限';
    } else {
        setText('#risk-margin-limit', formatUsdt(data.margin_limit));
        const sub = document.querySelector('#risk-margin-limit-sub');
        if (sub) sub.textContent = 'USDT';
    }

    if (data.limit_occupancy == null) {
        setText('#risk-limit-occupancy', '--');
        setBar('#risk-limit-bar', 0);
    } else {
        setText('#risk-limit-occupancy', data.limit_occupancy.toFixed(1) + '%');
        setBar('#risk-limit-bar', data.limit_occupancy);
    }

    if (data.equity_ratio_occupancy == null) {
        setText('#risk-equity-occupancy', '--');
        setBar('#risk-equity-bar', 0);
    } else {
        setText('#risk-equity-occupancy', data.equity_ratio_occupancy.toFixed(1) + '%');
        setBar('#risk-equity-bar', data.equity_ratio_occupancy);
    }

    setText('#risk-available-margin', data.available_margin != null ? formatUsdt(data.available_margin) : '--');
    setText('#risk-stop-count', data.recent_stop_count != null ? data.recent_stop_count.toLocaleString() : '0');

    renderRiskWarnings(data);
    startPositionCountdown(data.refresh_info);
}

/**
 * 对账周期到点后刷新页面数据
 *
 * 持仓对账由后端周期性发起（写入最新持仓快照）；前端倒计时归零即代表当前对账周期
 * 结束、下一周期对账开始。此时全页面数据重新加载一次：总览（含总盈亏）/趋势图/策略卡/
 * 账户净资产/时间戳（loadData），以及 AI 监控 / 风控，让页面展示对账后的最新数据。
 * refresh_info 会在响应中带回新的 refresh_in，倒计时随之重新对齐。
 * 用 inFlight 标志防止刷新与页面初始加载并发时叠加请求。
 */
let cycleRefreshInFlight = false;
// 对账归零触发的周期刷新期间，禁止用后端 refresh_in 覆盖当前倒计时，
// 从而让倒计时以前端固定周期（interval）递减，避免归零后出现 5:00→3:00 跳变。
let suppressCountdownReset = false;
async function refreshOnCycleEnd() {
    if (cycleRefreshInFlight) return;
    cycleRefreshInFlight = true;
    suppressCountdownReset = true;
    try {
        await loadData();                                    // 总览/趋势/策略卡/净资产/时间戳
        await Promise.all([loadAiMonitor(), loadRisk()]);    // AI 监控 / 风控
    } finally {
        cycleRefreshInFlight = false;
        suppressCountdownReset = false;
    }
}

/**
 * 启动持仓数据刷新倒计时（标记冷却时间，显示距下次更新剩余时长）
 * 以后端 refresh_info（refresh_interval / refresh_in）为起始种子，客户端每秒递减，
 * 归零后按下个周期对齐并刷新页面数据；重复调用会清理上一次定时器（幂等）。
 */
let positionRefreshTimer = null;
function startPositionCountdown(refreshInfo) {
    const el = document.querySelector('#position-refresh-countdown');
    if (!el || !refreshInfo) return;
    // 周期刷新（归零触发）期间：保留现有固定周期倒计时，不用 refresh_in 重置，
    // 避免归零时从新值（通常不足一周期）重新起跳造成时间跳变。
    if (suppressCountdownReset && positionRefreshTimer) return;
    const interval = parseInt(refreshInfo.refresh_interval, 10) || 300;
    let remaining = parseInt(refreshInfo.refresh_in, 10);
    if (isNaN(remaining) || remaining < 0) remaining = interval;

    if (positionRefreshTimer) clearInterval(positionRefreshTimer);
    const render = () => {
        const mm = String(Math.floor(Math.max(remaining, 0) / 60)).padStart(2, '0');
        const ss = String(Math.max(remaining, 0) % 60).padStart(2, '0');
        el.textContent = `持仓 · ${mm}:${ss} 后更新`;
    };
    render();
    positionRefreshTimer = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
            remaining = interval;      // 对齐下一个对账周期
            refreshOnCycleEnd();       // 到点刷新页面数据（展示对账后最新持仓快照）
        }
        render();
    }, 1000);
}

/**
 * 渲染风控预警区（超限 / 逼近阈值 / 大额回撤；阈值均来自后端配置，不硬编码）
 */
function renderRiskWarnings(data) {
    const container = document.querySelector('#risk-warnings');
    if (!container) return;
    const warnings = [];

    if (data.threshold_exceeded) {
        warnings.push('<span class="warn-item warn-danger">⚠️ 持仓占用已超阈值，请注意风控</span>');
    } else if (data.approaching_threshold) {
        warnings.push('<span class="warn-item warn-warn">⚠️ 持仓占用逼近阈值，请留意</span>');
    }

    if (data.drawdown_pct != null && data.daily_drawdown_pct != null
        && data.drawdown_pct >= data.daily_drawdown_pct) {
        warnings.push(`<span class="warn-item warn-danger">⚠️ 大额回撤 ${data.drawdown_pct.toFixed(1)}%</span>`);
    }

    container.innerHTML = warnings.join(' ');
}

/**
 * 格式化 USDT 金额（保留 2 位小数，千分位）
 */
function formatUsdt(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return '--';
    return num.toLocaleString('zh-CN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

/**
 * 格式化带符号百分比，如 "+2.4%" / "-1.3%"
 */
function formatSignedNumber(value, decimals = 1) {
    const num = parseFloat(value);
    if (isNaN(num)) return '--';
    const sign = num > 0 ? '+' : '';
    return sign + num.toFixed(decimals) + '%';
}

/**
 * 格式化时间：YYYY-MM-DD HH:mm
 */
function formatDateTime(isoStr) {
    if (!isoStr) return '--';
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * 转义 HTML，防止注入
 */
function escapeHtml(str) {
    return String(str ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

/**
 * 更新时间戳
 */
function updateTimestamp() {
    const timeElement = document.querySelector('.update-time .value');
    const now = new Date();
    timeElement.textContent = now.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

/**
 * 显示加载状态
 */
function showLoading() {
    document.querySelectorAll('.stat-value, .strategy-metric .value').forEach(el => {
        el.classList.add('updating');
    });
}

/**
 * 隐藏加载状态
 */
function hideLoading() {
    document.querySelectorAll('.stat-value, .strategy-metric .value').forEach(el => {
        el.classList.remove('updating');
    });
}

/**
 * 显示错误
 */
function showError(message) {
    hideLoading();
    alert(`数据加载失败: ${message}`);
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', init);