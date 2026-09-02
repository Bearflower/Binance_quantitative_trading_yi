/**
 * Dashboard 图表封装
 * ECharts 配置和初始化
 */

// 金融科技亮色主题
const fintechLightTheme = {
    backgroundColor: 'transparent',

    textStyle: {
        fontFamily: 'DM Sans, -apple-system, sans-serif',
        fontSize: 14,
        color: '#5B6B82'
    },

    title: {
        textStyle: {
            fontFamily: 'DM Sans, -apple-system, sans-serif',
            fontSize: 18,
            fontWeight: 600,
            color: '#1A2333'
        },
        subtextStyle: {
            fontSize: 14,
            color: '#93A1B4'
        }
    },

    legend: {
        textStyle: {
            fontFamily: 'DM Sans, -apple-system, sans-serif',
            fontSize: 14,
            color: '#5B6B82'
        },
        pageTextStyle: {
            color: '#5B6B82'
        },
        pageIconColor: '#D97706',
        pageIconInactiveColor: '#94A3B8'
    },

    tooltip: {
        backgroundColor: '#ffffff',
        borderColor: 'rgba(15, 23, 42, 0.08)',
        borderWidth: 1,
        textStyle: {
            fontFamily: 'DM Sans, -apple-system, sans-serif',
            fontSize: 14,
            color: '#1A2333'
        },
        extraCssText: 'border-radius: 10px; box-shadow: 0 10px 30px rgba(20, 33, 61, 0.08);'
    },

    categoryAxis: {
        axisLine: {
            lineStyle: {
                color: 'rgba(15, 23, 42, 0.10)'
            }
        },
        axisTick: {
            lineStyle: {
                color: 'rgba(15, 23, 42, 0.10)'
            }
        },
        axisLabel: {
            fontFamily: 'DM Sans, -apple-system, sans-serif',
            fontSize: 12,
            color: '#93A1B4'
        },
        splitLine: {
            lineStyle: {
                color: 'rgba(15, 23, 42, 0.05)',
                type: 'dashed'
            }
        }
    },

    valueAxis: {
        axisLine: {
            lineStyle: {
                color: 'rgba(15, 23, 42, 0.10)'
            }
        },
        axisTick: {
            lineStyle: {
                color: 'rgba(15, 23, 42, 0.10)'
            }
        },
        axisLabel: {
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 12,
            color: '#93A1B4'
        },
        splitLine: {
            lineStyle: {
                color: 'rgba(15, 23, 42, 0.06)',
                type: 'dashed'
            }
        }
    },

    line: {
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        itemStyle: {
            borderWidth: 2
        },
        lineStyle: {
            width: 3
        },
        emphasis: {
            itemStyle: {
                borderWidth: 3,
                shadowBlur: 10,
                shadowColor: 'rgba(15, 164, 127, 0.3)'
            }
        }
    },

    color: DashboardConfig.chartColors
};

// 注册主题
if (typeof echarts !== 'undefined') {
    echarts.registerTheme('fintech-light', fintechLightTheme);
}

/**
 * 创建趋势图
 */
function createTrendChart(containerId, data) {
    const chart = echarts.init(
        document.getElementById(containerId),
        'fintech-light'
    );

    // 兼容新旧数据格式
    let dates = [];
    let series = [];

    if (data.trends && Array.isArray(data.trends)) {
        // 新格式: { trends: [{date, total_pnl, order_count, win_rate}] }
        dates = data.trends.map(t => t.date);
        series = [{
            name: '总盈亏',
            type: 'line',
            data: data.trends.map(t => parseFloat(t.total_pnl) || 0),
            emphasis: { focus: 'series' },
            itemStyle: { color: DashboardConfig.chartColors[0] },
            lineStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
                    { offset: 0, color: DashboardConfig.chartColors[0] },
                    { offset: 1, color: DashboardConfig.chartColors[1] }
                ])
            },
            areaStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    { offset: 0, color: 'rgba(15, 164, 127, 0.16)' },
                    { offset: 1, color: 'rgba(15, 164, 127, 0.01)' }
                ])
            }
        }];
    } else {
        // 旧格式兼容: { dates: [...], strategies: { name: [...] } }
        dates = data.dates || [];
        series = Object.entries(data.strategies || {}).map(([name, points]) => ({
            name,
            type: 'line',
            data: points.map(p => parseFloat(p.total_pnl) || 0),
            emphasis: { focus: 'series' }
        }));
    }

    const option = {
        grid: {
            top: 40,
            right: 40,
            bottom: 40,
            left: 60,
            containLabel: true
        },

        tooltip: {
            trigger: 'axis',
            axisPointer: {
                type: 'cross',
                crossStyle: {
                    color: '#94A3B8'
                }
            }
        },

        legend: {
            data: series.map(s => s.name),
            top: 0,
            right: 0,
            itemWidth: 16,
            itemHeight: 8,
            itemGap: 16
        },

        xAxis: {
            type: 'category',
            data: dates
        },

        yAxis: {
            type: 'value',
            name: '盈亏 (USDT)',
            nameTextStyle: {
                fontSize: 12,
                color: '#5B6B82',
                padding: [0, 0, 0, -40]
            },
            axisLabel: {
                formatter: (value) => {
                    if (Math.abs(value) >= 1000) {
                        return (value / 1000).toFixed(1) + 'K';
                    }
                    return value.toFixed(2);
                }
            }
        },

        series: series
    };

    chart.setOption(option);

    // 响应式
    window.addEventListener('resize', () => {
        chart.resize();
    });

    return chart;
}

/**
 * 格式化数字
 */
function formatNumber(value, decimals = 2) {
    const num = parseFloat(value);
    if (isNaN(num)) return '0';

    const sign = num >= 0 ? '+' : '';
    return sign + num.toLocaleString('zh-CN', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    });
}

/**
 * 格式化百分比
 */
function formatPercent(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return '0.0%';
    return num.toFixed(1) + '%';
}