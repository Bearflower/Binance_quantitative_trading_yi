// 所有图表入口等待同一个 Promise，CDN 回退也遵守同一就绪时序。
window.echartsReady = new Promise((resolve, reject) => {
    const sources = [
        'js/vendor/echarts.min.js',
        'https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js',
        'https://cdn.bootcdn.net/ajax/libs/echarts/5.5.0/echarts.min.js'
    ];
    function load(index) {
        if (window.echarts) {
            resolve(window.echarts);
            return;
        }
        if (index >= sources.length) {
            reject(new Error('图表库加载失败，请检查网络后刷新页面'));
            return;
        }
        const script = document.createElement('script');
        script.src = sources[index];
        script.onload = () => window.echarts ? resolve(window.echarts) : load(index + 1);
        script.onerror = () => load(index + 1);
        document.head.appendChild(script);
    }
    load(0);
});
// 错误统一由页面初始化或刷新时展示，避免页面尚未就绪时出现未处理拒绝。
window.echartsReady.catch(() => {});
