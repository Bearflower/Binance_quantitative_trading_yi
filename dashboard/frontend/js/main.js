/**
 * Dashboard 主逻辑
 * 页面交互和数据加载
 */

// 当前数据类型（daily/weekly/monthly）
let currentType = 'daily';

// 图表实例
let trendChart = null;

/**
 * 初始化页面
 */
async function init() {
    console.log('Dashboard 初始化中...');

    // 设置事件监听
    setupEventListeners();

    // 加载数据
    await loadData();
}

/**
 * 设置事件监听
 */
function setupEventListeners() {
    // 日/周/月切换
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
 * 更新总览
 */
function updateOverview(data) {
    const totalPnl = document.querySelector('#total-pnl');
    const pnlValue = parseFloat(data.total_pnl);
    totalPnl.textContent = formatNumber(pnlValue);
    totalPnl.className = 'stat-value ' + (pnlValue >= 0 ? 'positive' : 'negative');

    const winRate = document.querySelector('#win-rate');
    if (winRate) winRate.textContent = formatPercent(data.win_rate);

    const closedCount = document.querySelector('#closed-count');
    if (closedCount) closedCount.textContent = data.total_closed.toLocaleString();

    const orderCount = document.querySelector('#order-count');
    if (orderCount) orderCount.textContent = data.total_orders.toLocaleString();

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
    });
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