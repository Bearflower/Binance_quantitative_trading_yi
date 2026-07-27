/**
 * Dashboard 图表封装
 * ECharts 配置和初始化
 */

// 金融科技深色主题
const fintechDarkTheme = {
    backgroundColor: 'transparent',

    textStyle: {
        fontFamily: 'Inter, -apple-system, sans-serif',
        fontSize: 14,
        color: '#94A3B8'
    },

    title: {
        textStyle: {
            fontFamily: 'Inter, -apple-system, sans-serif',
            fontSize: 18,
            fontWeight: 600,
            color: '#F8FAFC'
        },
        subtextStyle: {
            fontSize: 14,
            color: '#94A3B8'
        }
    },

    legend: {
        textStyle: {
            fontFamily: 'Inter, -apple-system, sans-serif',
            fontSize: 14,
            color: '#94A3B8'
        },
        pageTextStyle: {
            color: '#94A3B8'
        },
        pageIconColor: '#F59E0B',
        pageIconInactiveColor: '#64748B'
    },

    tooltip: {
        backgroundColor: '#1E293B',
        borderColor: '#334155',
        borderWidth: 1,
        textStyle: {
            fontFamily: 'Inter, -apple-system, sans-serif',
            fontSize: 14,
            color: '#F8FAFC'
        },
        extraCssText: 'border-radius: 8px; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);'
    },

    categoryAxis: {
        axisLine: {
            lineStyle: {
                color: '#334155'
            }
        },
        axisTick: {
            lineStyle: {
                color: '#334155'
            }
        },
        axisLabel: {
            fontFamily: 'Inter, -apple-system, sans-serif',
            fontSize: 12,
            color: '#94A3B8'
        },
        splitLine: {
            lineStyle: {
                color: '#272F42',
                type: 'dashed'
            }
        }
    },

    valueAxis: {
        axisLine: {
            lineStyle: {
                color: '#334155'
            }
        },
        axisTick: {
            lineStyle: {
                color: '#334155'
            }
        },
        axisLabel: {
            fontFamily: 'Fira Code, monospace',
            fontSize: 12,
            color: '#94A3B8'
        },
        splitLine: {
            lineStyle: {
                color: '#272F42',
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
                shadowColor: 'rgba(245, 158, 11, 0.3)'
            }
        }
    },

    color: DashboardConfig.chartColors
};

// 注册主题
if (typeof echarts !== 'undefined') {
    echarts.registerTheme('fintech-dark', fintechDarkTheme);
}

/**
 * 创建趋势图
 */
function createTrendChart(containerId, data) {
    const chart = echarts.init(
        document.getElementById(containerId),
        'fintech-dark'
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
                    { offset: 0, color: 'rgba(245, 158, 11, 0.25)' },
                    { offset: 1, color: 'rgba(245, 158, 11, 0.02)' }
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
                    color: '#64748B'
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
                color: '#94A3B8',
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
