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
    
    // 图表主题颜色（对齐新版亮色 UI：明朗青绿主色 + 紫罗兰强调）
    chartColors: [
        '#0FA47F', // Primary 青绿（盈亏主线）
        '#7C6BF0', // Accent 紫罗兰
        '#2563EB', // Info 天蓝
        '#E5484D', // Destructive 珊瑚红
        '#D97706', // Secondary 琥珀金
        '#06B6D4', // Cyan
        '#8B5CF6', // Indigo
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
