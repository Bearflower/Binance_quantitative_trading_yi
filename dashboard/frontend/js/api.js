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
}

// 导出单例
const api = new DashboardAPI();
