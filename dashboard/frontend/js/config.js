/**
 * Dashboard 前端配置
 * 集中管理所有可配置参数
 */

const DashboardConfig = {
    // API 配置
    api: {
        baseUrl: '/api',  // API 基础地址
        timeout: 30000    // 请求超时时间（毫秒）
    },
    
    // 趋势图配置
    trend: {
        defaultDays: 7,   // 默认显示天数
        maxDays: 30       // 最大显示天数
    },
    
    // 图表主题颜色
    chartColors: [
        '#F59E0B', // Primary 金色
        '#8B5CF6', // Accent 紫色
        '#10B981', // Success 绿色
        '#3B82F6', // Info 蓝色
        '#EF4444', // Destructive 红色
        '#FBBF24', // Secondary 亮金色
        '#06B6D4', // Cyan
        '#EC4899'  // Pink
    ],
    
    // CDN 配置
    cdn: {
        echarts: 'https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js',
        fonts: 'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500;600&display=swap'
    },
    
    // UI 配置
    ui: {
        animationDuration: 300,  // 动画持续时间（毫秒）
        debounceDelay: 300       // 防抖延迟（毫秒）
    }
};

// 冻结配置对象，防止意外修改
Object.freeze(DashboardConfig);
Object.freeze(DashboardConfig.api);
Object.freeze(DashboardConfig.trend);
Object.freeze(DashboardConfig.chartColors);
Object.freeze(DashboardConfig.cdn);
Object.freeze(DashboardConfig.ui);
