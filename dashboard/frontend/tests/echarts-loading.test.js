// 离线复现浏览器脚本加载顺序，不访问网络，也不依赖真实 DOM。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

function page() {
    const scripts = [], errors = [];
    const context = {
        Promise, Error, setTimeout, clearTimeout,
        document: { createElement: () => ({}), head: {appendChild: script => scripts.push(script)}, getElementById: () => ({}) },
        api: {getOverview: async () => ({strategies: []}), getTrend: async () => ({trends: []})},
        DashboardConfig: {trend: {defaultDays: 7}, chartColors: ['red', 'blue']}, currentType: 'daily', trendChart: null,
        showLoading() {}, hideLoading() {}, updateOverview() {}, updateStrategyCards() {}, updateTimestamp() {},
        loadAccountEquity: async () => {}, loadReturns: async () => {},
        showError: message => errors.push(message), console: {error() {}}, addEventListener() {},
    };
    context.window = context;
    vm.createContext(context);
    const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const loader = html.includes('js/echarts-loader.js')
        ? fs.readFileSync(path.join(root, 'js/echarts-loader.js'), 'utf8')
        : html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
    vm.runInContext(loader, context);
    vm.runInContext(fs.readFileSync(path.join(root, 'js/charts.js'), 'utf8'), context);
    const source = fs.readFileSync(path.join(root, 'js/main.js'), 'utf8');
    const start = source.indexOf('let loadGeneration = 0;');
    assert.notEqual(start, -1, 'loadData generation guard must remain present');
    vm.runInContext(source.slice(start, source.indexOf('/**', start)), context);
    return {context, scripts, errors};
}

test('local failure and delayed CDN wait before rendering, then render once', async () => {
    const {context, scripts, errors} = page();
    assert.equal(typeof context.echartsReady?.then, 'function');
    scripts[0].onerror();
    await Promise.resolve();
    const loading = context.loadData();
    await Promise.resolve();
    assert.equal(context.trendChart, null);
    assert.deepEqual(errors, []);
    let rendered = 0, themed = 0;
    context.echarts = {
        registerTheme() { themed++; },
        graphic: {LinearGradient: function () {}},
        init() { rendered++; return {setOption() {}, resize() {}}; }
    };
    scripts[1].onload();
    await loading;
    assert.equal(rendered, 1);
    assert.equal(themed, 1);
    assert.deepEqual(errors, []);
});

test('all unavailable sources show one actionable error without calling charts', async () => {
    const {context, scripts, errors} = page();
    assert.equal(typeof context.echartsReady?.then, 'function');
    const loading = context.loadData();
    for (let index = 0; index < 3; index++) {
        scripts[index].onerror();
        await Promise.resolve();
    }
    await loading;
    assert.equal(context.trendChart, null);
    assert.equal(errors.length, 1);
    assert.match(errors[0], /图表库/);
});
