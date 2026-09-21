/**
 * Dashboard API 客户端
 * 封装所有 API 调用
 */

class DashboardAPI {
    constructor(baseUrl = DashboardConfig.api.baseUrl) {
        this.baseUrl = baseUrl;
    }

    /**
     * 通用请求方法
     */
    async request(path, options = {}) {
        const url = `${this.baseUrl}${path}`;

        try {
            const response = await fetch(url, {
                ...options,
                cache: 'no-cache',  // 禁止浏览器缓存，确保切换日/周/月时数据实时更新
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers
                }
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error?.message || `API错误: ${response.status}`);
            }

            const result = await response.json();
            
            // 检查业务状态码
            if (result.code !== 0) {
                throw new Error(result.message || 'API错误');
            }
            
            // 返回data字段
            return result.data;
        } catch (error) {
            console.error('API请求失败:', path, error);
            throw error;
        }
    }

    /**
     * 获取健康状态
     */
    async getHealth() {
        return this.request('/health');
    }

    /**
     * 获取元数据
     */
    async getMetadata() {
        return this.request('/metadata');
    }

    /**
     * 获取总览数据
     */
    async getOverview(type = 'daily') {
        return this.request(`/overview?type=${type}`);
    }

    /**
     * 获取策略列表
     */
    async getStrategies(type = 'daily') {
        return this.request(`/strategies?type=${type}`);
    }

    /**
     * 获取策略详情
     */
    async getStrategyDetail(strategyId, type = 'daily') {
        return this.request(`/strategies/${strategyId}?type=${type}`);
    }

    /**
     * 获取币种明细
     */
    async getStrategySymbols(strategyId, type = 'daily') {
        return this.request(`/strategies/${strategyId}/symbols?type=${type}`);
    }

    /**
     * 获取趋势数据
     */
    async getTrend(type = 'daily', days = 7) {
        return this.request(`/trend?type=${type}&days=${days}`);
    }

    /**
     * 获取合约账户净资产（实时快照，不随日/周/月切换）
     */
    async getAccountEquity() {
        return this.request('/account/equity');
    }

    /**
     * 获取账户收益率（基于净资产快照增量）
     * @param {string} period 周期：daily / weekly / monthly
     */
    async getAccountReturns(period = 'daily') {
        return this.request(`/account/returns?period=${period}`);
    }

    /**
     * 获取 AI 监控数据（月度资金分配 + 最近优化建议）
     * @param {number} weeks 回溯周数
     * @param {number} limit 返回条数上限
     */
    async getAiMonitor(weeks = DashboardConfig.aiMonitor.weeks, limit = DashboardConfig.aiMonitor.limit) {
        return this.request(`/ai-monitor?weeks=${weeks}&limit=${limit}`);
    }

    /**
     * 获取各策略占用比历史趋势（按天聚合）
     * @param {number} days 回溯天数（默认 30）
     */
    async getPositionUtilizationTrend(days = 30) {
        return this.request(`/position-utilization-trend?days=${days}`);
    }

    /**
     * 获取风控指标（持仓占用 / 阈值预警 / 止损统计）
     * @param {number} days 统计天数
     */
    async getRisk(days = DashboardConfig.risk.days) {
        return this.request(`/risk?days=${days}`);
    }
}

// 导出单例
const api = new DashboardAPI();
