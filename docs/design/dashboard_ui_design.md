# 金融科技数据看板 UI 设计文档

> **版本**: v1.0  
> **更新日期**: 2026-06-02  
> **设计师**: UI 设计师  
> **项目**: Binance 量化交易数据看板

---

## 目录

1. [设计系统概述](#1-设计系统概述)
2. [HTML 结构设计](#2-html-结构设计)
3. [CSS 样式设计](#3-css-样式设计)
4. [交互设计](#4-交互设计)
5. [ECharts 配置](#5-echarts-配置)
6. [设计稿描述](#6-设计稿描述)
7. [响应式设计](#7-响应式设计)
8. [无障碍设计](#8-无障碍设计)

---

## 1. 设计系统概述

### 1.1 设计理念

**核心原则**: 专业、精确、高端、可信赖

- **深色主题**: 降低视觉疲劳，突出数据信息
- **金色主色调**: 传达财富、信任、专业的品牌形象
- **高对比度**: 确保 WCAG AAA 标准，提升可读性
- **数据优先**: 清晰的信息层级，快速传达关键指标

### 1.2 配色方案

#### 语义化颜色 Token

```css
:root {
  /* 品牌色 */
  --color-primary: #F59E0B;          /* 金色 - 代表财富和信任 */
  --color-on-primary: #0F172A;       /* 金色上的文字 */
  --color-secondary: #FBBF24;        /* 亮金色 - 次要强调 */
  --color-accent: #8B5CF6;           /* 紫色 - 科技感、CTA按钮 */
  
  /* 背景色 */
  --color-background: #0F172A;       /* 深蓝黑背景 */
  --color-surface: #1E293B;          /* 卡片背景 */
  --color-muted: #272F42;            /* 次要背景、禁用状态 */
  
  /* 文字色 */
  --color-foreground: #F8FAFC;       /* 主要文字 */
  --color-text-secondary: #94A3B8;   /* 次要文字 */
  --color-text-muted: #64748B;       /* 辅助文字 */
  
  /* 边框与分割线 */
  --color-border: #334155;           /* 默认边框 */
  --color-border-hover: #475569;     /* 悬停边框 */
  
  /* 语义色 */
  --color-success: #10B981;          /* 盈利绿色 */
  --color-destructive: #EF4444;      /* 亏损红色 */
  --color-warning: #F59E0B;          /* 警告金色 */
  --color-info: #3B82F6;             /* 信息蓝色 */
  
  /* 焦点环 */
  --color-ring: #F59E0B;             /* 焦点环颜色 */
  --ring-width: 3px;                 /* 焦点环宽度 */
}
```

#### 颜色使用规范

| 场景 | 颜色 | 说明 |
|------|------|------|
| 盈利数字 | `--color-success` | 正收益显示 |
| 亏损数字 | `--color-destructive` | 负收益显示 |
| 主要CTA | `--color-accent` | 查看详情按钮 |
| 卡片背景 | `--color-surface` | 策略卡片背景 |
| 悬停状态 | `--color-border-hover` | 边框高亮 |

### 1.3 字体系统

```css
:root {
  /* 字体家族 */
  --font-family-base: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-family-mono: 'Fira Code', 'SF Mono', Monaco, 'Cascadia Code', monospace;
  
  /* 字体大小 */
  --font-size-xs: 0.75rem;     /* 12px */
  --font-size-sm: 0.875rem;    /* 14px */
  --font-size-base: 1rem;      /* 16px */
  --font-size-lg: 1.125rem;    /* 18px */
  --font-size-xl: 1.25rem;     /* 20px */
  --font-size-2xl: 1.5rem;     /* 24px */
  --font-size-3xl: 1.875rem;   /* 30px */
  --font-size-4xl: 2.25rem;    /* 36px */
  --font-size-5xl: 3rem;       /* 48px - 大数字 */
  
  /* 字重 */
  --font-weight-light: 300;
  --font-weight-normal: 400;
  --font-weight-medium: 500;
  --font-weight-semibold: 600;
  --font-weight-bold: 700;
  
  /* 行高 */
  --line-height-tight: 1.25;
  --line-height-normal: 1.5;
  --line-height-relaxed: 1.75;
  
  /* 字间距 */
  --letter-spacing-tight: -0.025em;
  --letter-spacing-normal: 0;
  --letter-spacing-wide: 0.025em;
}
```

#### 字体使用规范

| 元素 | 字体大小 | 字重 | 说明 |
|------|---------|------|------|
| 大数字（盈亏） | `--font-size-5xl` | `--font-weight-bold` | 核心指标 |
| 页面标题 | `--font-size-3xl` | `--font-weight-semibold` | 策略名称 |
| 卡片标题 | `--font-size-xl` | `--font-weight-medium` | 区域标题 |
| 正文 | `--font-size-base` | `--font-weight-normal` | 描述文字 |
| 辅助文字 | `--font-size-sm` | `--font-weight-normal` | 标签、提示 |
| 数字（表格） | `--font-size-base` | `--font-weight-medium` | 使用等宽字体 |

### 1.4 间距系统

```css
:root {
  /* 基础间距单位: 4px */
  --spacing-0: 0;
  --spacing-1: 0.25rem;   /* 4px */
  --spacing-2: 0.5rem;    /* 8px */
  --spacing-3: 0.75rem;   /* 12px */
  --spacing-4: 1rem;      /* 16px */
  --spacing-5: 1.25rem;   /* 20px */
  --spacing-6: 1.5rem;    /* 24px */
  --spacing-8: 2rem;      /* 32px */
  --spacing-10: 2.5rem;   /* 40px */
  --spacing-12: 3rem;     /* 48px */
  --spacing-16: 4rem;     /* 64px */
  --spacing-20: 5rem;     /* 80px */
}
```

### 1.5 阴影系统

```css
:root {
  /* 卡片阴影 */
  --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1);
  --shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1);
  
  /* 发光效果（用于强调） */
  --glow-primary: 0 0 20px rgba(245, 158, 11, 0.3);
  --glow-accent: 0 0 20px rgba(139, 92, 246, 0.3);
  --glow-success: 0 0 20px rgba(16, 185, 129, 0.3);
  --glow-destructive: 0 0 20px rgba(239, 68, 68, 0.3);
}
```

### 1.6 圆角系统

```css
:root {
  --radius-sm: 0.25rem;    /* 4px */
  --radius-md: 0.5rem;     /* 8px */
  --radius-lg: 0.75rem;    /* 12px */
  --radius-xl: 1rem;       /* 16px */
  --radius-2xl: 1.5rem;    /* 24px */
  --radius-full: 9999px;   /* 全圆 */
}
```

### 1.7 动画时长

```css
:root {
  --duration-fast: 150ms;
  --duration-normal: 250ms;
  --duration-slow: 350ms;
  --duration-slower: 500ms;
  
  --easing-default: cubic-bezier(0.4, 0, 0.2, 1);
  --easing-in: cubic-bezier(0.4, 0, 1, 1);
  --easing-out: cubic-bezier(0, 0, 0.2, 1);
  --easing-in-out: cubic-bezier(0.4, 0, 0.2, 1);
}
```

---

## 2. HTML 结构设计

### 2.1 首页（index.html）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="Binance 量化交易数据看板">
  <title>数据看板 - Binance 量化交易</title>
  
  <!-- 字体 -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500;600&display=swap" rel="stylesheet">
  
  <!-- 样式 -->
  <link rel="stylesheet" href="styles.css">
  
  <!-- ECharts（本地优先加载，CDN作为降级方案） -->
  <script src="js/vendor/echarts.min.js"></script>
  <script>
  if (typeof echarts === 'undefined') {
      var s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js';
      s.onerror = function() {
          var s2 = document.createElement('script');
          s2.src = 'https://cdn.bootcdn.net/ajax/libs/echarts/5.5.0/echarts.min.js';
          document.head.appendChild(s2);
      };
      document.head.appendChild(s);
  }
  </script>
</head>
<body class="dashboard-home">
  <!-- 跳过导航链接（无障碍） -->
  <a href="#main-content" class="skip-link">跳到主要内容</a>
  
  <!-- 顶部导航栏 -->
  <header class="navbar" role="banner">
    <div class="navbar-container">
      <!-- Logo -->
      <div class="navbar-brand">
        <img src="logo.svg" alt="Binance Logo" class="navbar-logo">
        <span class="navbar-title">量化交易看板</span>
      </div>
      
      <!-- 导航操作区 -->
      <nav class="navbar-actions" role="navigation" aria-label="主导航">
        <!-- 日/周/月切换 -->
        <div class="toggle-group" role="radiogroup" aria-label="时间范围">
          <button 
            class="toggle-btn active" 
            role="radio" 
            aria-checked="true"
            data-range="day"
          >
            日
          </button>
          <button 
            class="toggle-btn" 
            role="radio" 
            aria-checked="false"
            data-range="week"
          >
            周
          </button>
          <button 
            class="toggle-btn" 
            role="radio" 
            aria-checked="false"
            data-range="month"
          >
            月
          </button>
        </div>
        
        <!-- 最后更新时间 -->
        <div class="update-time" aria-live="polite">
          <span class="update-time-label">最后更新:</span>
          <time class="update-time-value" datetime="2026-06-02T15:30:00">
            2026-06-02 15:30:00
          </time>
        </div>
      </nav>
    </div>
  </header>
  
  <!-- 主要内容区 -->
  <main id="main-content" class="main-content">
    <!-- 总览卡片区域 -->
    <section class="overview-section" aria-labelledby="overview-title">
      <h2 id="overview-title" class="section-title sr-only">总览</h2>
      
      <div class="overview-card">
        <div class="overview-card-header">
          <h3 class="overview-card-title">总账户收益</h3>
          <span class="overview-card-badge" aria-label="实时更新">实时</span>
        </div>
        
        <div class="overview-card-body">
          <!-- 总盈亏（核心指标） -->
          <div class="metric-primary">
            <span class="metric-label">总盈亏</span>
            <div class="metric-value-group">
              <span class="metric-value positive" data-value="125680.50">
                +125,680.50
              </span>
              <span class="metric-unit">USDT</span>
            </div>
          </div>
          
          <!-- 次要指标 -->
          <div class="metrics-grid">
            <div class="metric-item">
              <span class="metric-label">总胜率</span>
              <span class="metric-value">68.5%</span>
            </div>
            <div class="metric-item">
              <span class="metric-label">总平仓数</span>
              <span class="metric-value">1,245</span>
            </div>
            <div class="metric-item">
              <span class="metric-label">总委托数</span>
              <span class="metric-value">2,890</span>
            </div>
          </div>
        </div>
      </div>
    </section>
    
    <!-- 策略卡片区域 -->
    <section class="strategies-section" aria-labelledby="strategies-title">
      <h2 id="strategies-title" class="section-title">策略概览</h2>
      
      <div class="strategies-grid">
        <!-- BTC_ETH 策略卡片 -->
        <article class="strategy-card" data-strategy="btc_eth">
          <div class="strategy-card-header">
            <div class="strategy-icon">
              <svg><!-- BTC/ETH 图标 --></svg>
            </div>
            <h3 class="strategy-name">BTC_ETH 策略</h3>
          </div>
          
          <div class="strategy-card-body">
            <!-- 盈亏（大数字） -->
            <div class="strategy-metric-primary">
              <span class="metric-label">当日盈亏</span>
              <div class="metric-value-group">
                <span class="metric-value positive">+45,230.80</span>
                <span class="metric-unit">USDT</span>
              </div>
            </div>
            
            <!-- 次要指标 -->
            <div class="strategy-metrics">
              <div class="metric-item">
                <span class="metric-label">胜率</span>
                <span class="metric-value">72.3%</span>
              </div>
              <div class="metric-item">
                <span class="metric-label">平仓数</span>
                <span class="metric-value">456</span>
              </div>
              <div class="metric-item">
                <span class="metric-label">委托数</span>
                <span class="metric-value">890</span>
              </div>
            </div>
            
            <!-- 迷你趋势图 -->
            <div class="strategy-sparkline" aria-hidden="true">
              <canvas id="sparkline-btc-eth"></canvas>
            </div>
          </div>
          
          <div class="strategy-card-footer">
            <a href="detail.html?strategy=btc_eth" class="btn btn-primary">
              查看详情
              <svg class="btn-icon"><!-- 箭头图标 --></svg>
            </a>
          </div>
        </article>
        
        <!-- NEW_COIN 策略卡片 -->
        <article class="strategy-card" data-strategy="new_coin">
          <!-- 结构同上 -->
        </article>
        
        <!-- HRS 策略卡片 -->
        <article class="strategy-card" data-strategy="hrs">
          <!-- 结构同上 -->
        </article>
      </div>
    </section>
    
    <!-- 收益趋势图 -->
    <section class="trend-section" aria-labelledby="trend-title">
      <div class="trend-header">
        <h2 id="trend-title" class="section-title">收益趋势</h2>
        <div class="trend-controls">
          <button class="btn btn-ghost active" data-period="7d">近7天</button>
          <button class="btn btn-ghost" data-period="4w">近4周</button>
          <button class="btn btn-ghost" data-period="3m">近3月</button>
        </div>
      </div>
      
      <div class="trend-chart-container">
        <div id="trend-chart" class="trend-chart" role="img" aria-label="收益趋势图"></div>
      </div>
    </section>
  </main>
  
  <!-- 页脚 -->
  <footer class="footer" role="contentinfo">
    <p class="footer-text">© 2026 Binance 量化交易系统</p>
  </footer>
  
  <!-- 脚本 -->
  <script src="dashboard.js"></script>
</body>
</html>
```

### 2.2 详情页（detail.html）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <!-- 同首页 -->
</head>
<body class="dashboard-detail">
  <!-- 跳过导航链接 -->
  <a href="#main-content" class="skip-link">跳到主要内容</a>
  
  <!-- 顶部导航栏 -->
  <header class="navbar" role="banner">
    <div class="navbar-container">
      <!-- 返回按钮 -->
      <a href="index.html" class="btn btn-ghost btn-back" aria-label="返回首页">
        <svg class="btn-icon"><!-- 返回箭头 --></svg>
        <span>返回</span>
      </a>
      
      <!-- 策略名称 -->
      <h1 class="navbar-title">BTC_ETH 策略详情</h1>
      
      <!-- 日/周/月切换 -->
      <div class="toggle-group" role="radiogroup" aria-label="时间范围">
        <!-- 同首页 -->
      </div>
    </div>
  </header>
  
  <!-- 主要内容区 -->
  <main id="main-content" class="main-content">
    <!-- 策略概览卡片 -->
    <section class="detail-overview" aria-labelledby="detail-overview-title">
      <h2 id="detail-overview-title" class="sr-only">策略概览</h2>
      
      <div class="detail-overview-card">
        <div class="detail-metrics-grid">
          <div class="detail-metric-item">
            <span class="metric-label">总盈亏</span>
            <div class="metric-value-group">
              <span class="metric-value positive">+45,230.80</span>
              <span class="metric-unit">USDT</span>
            </div>
          </div>
          
          <div class="detail-metric-item">
            <span class="metric-label">胜率</span>
            <span class="metric-value">72.3%</span>
          </div>
          
          <div class="detail-metric-item">
            <span class="metric-label">平仓数</span>
            <span class="metric-value">456</span>
          </div>
          
          <div class="detail-metric-item">
            <span class="metric-label">委托数</span>
            <span class="metric-value">890</span>
          </div>
        </div>
      </div>
    </section>
    
    <!-- 币种明细表格 -->
    <section class="detail-table-section" aria-labelledby="table-title">
      <h2 id="table-title" class="section-title">币种明细</h2>
      
      <div class="table-container">
        <table class="data-table" role="grid">
          <thead>
            <tr>
              <th scope="col" class="sortable" data-sort="symbol">
                交易对
                <svg class="sort-icon"><!-- 排序图标 --></svg>
              </th>
              <th scope="col" class="sortable" data-sort="orders">
                委托
                <svg class="sort-icon"><!-- 排序图标 --></svg>
              </th>
              <th scope="col" class="sortable" data-sort="filled">
                成交
                <svg class="sort-icon"><!-- 排序图标 --></svg>
              </th>
              <th scope="col" class="sortable" data-sort="closed">
                平仓
                <svg class="sort-icon"><!-- 排序图标 --></svg>
              </th>
              <th scope="col" class="sortable" data-sort="pnl">
                盈亏
                <svg class="sort-icon"><!-- 排序图标 --></svg>
              </th>
              <th scope="col" class="sortable" data-sort="winrate">
                胜率
                <svg class="sort-icon"><!-- 排序图标 --></svg>
              </th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr class="table-row" data-symbol="BTCUSDT" tabindex="0">
              <td class="cell-symbol">
                <span class="symbol-name">BTCUSDT</span>
              </td>
              <td class="cell-number">125</td>
              <td class="cell-number">118</td>
              <td class="cell-number">56</td>
              <td class="cell-number positive">+12,450.30</td>
              <td class="cell-number">75.0%</td>
              <td class="cell-action">
                <button class="btn btn-ghost btn-sm" aria-label="查看 BTCUSDT 详情">
                  <svg><!-- 查看图标 --></svg>
                </button>
              </td>
            </tr>
            <!-- 更多行... -->
          </tbody>
        </table>
      </div>
    </section>
  </main>
  
  <!-- 币种详情弹窗 -->
  <div class="modal" id="symbol-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" hidden>
    <div class="modal-backdrop"></div>
    <div class="modal-container">
      <div class="modal-header">
        <h3 id="modal-title" class="modal-title">BTCUSDT 详情</h3>
        <button class="btn btn-ghost btn-close" aria-label="关闭">
          <svg><!-- 关闭图标 --></svg>
        </button>
      </div>
      
      <div class="modal-body">
        <!-- 详细统计数据 -->
        <div class="modal-stats">
          <div class="modal-stat-item">
            <span class="stat-label">总盈亏</span>
            <span class="stat-value positive">+12,450.30 USDT</span>
          </div>
          <div class="modal-stat-item">
            <span class="stat-label">胜率</span>
            <span class="stat-value">75.0%</span>
          </div>
          <div class="modal-stat-item">
            <span class="stat-label">委托数</span>
            <span class="stat-value">125</span>
          </div>
          <div class="modal-stat-item">
            <span class="stat-label">成交数</span>
            <span class="stat-value">118</span>
          </div>
          <div class="modal-stat-item">
            <span class="stat-label">平仓数</span>
            <span class="stat-value">56</span>
          </div>
        </div>
        
        <!-- 迷你趋势图 -->
        <div class="modal-chart">
          <h4 class="modal-chart-title">近7日盈亏趋势</h4>
          <div id="modal-sparkline" class="sparkline-chart"></div>
        </div>
      </div>
    </div>
  </div>
  
  <!-- 页脚 -->
  <footer class="footer" role="contentinfo">
    <p class="footer-text">© 2026 Binance 量化交易系统</p>
  </footer>
  
  <!-- 脚本 -->
  <script src="dashboard.js"></script>
  <script src="detail.js"></script>
</body>
</html>
```

---

## 3. CSS 样式设计

### 3.1 全局样式（styles.css）

```css
/* ========================================
   全局重置与基础样式
   ======================================== */

*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html {
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}

body {
  font-family: var(--font-family-base);
  font-size: var(--font-size-base);
  font-weight: var(--font-weight-normal);
  line-height: var(--line-height-normal);
  color: var(--color-foreground);
  background-color: var(--color-background);
  min-height: 100vh;
}

/* 跳过导航链接（无障碍） */
.skip-link {
  position: absolute;
  top: -40px;
  left: 0;
  background: var(--color-primary);
  color: var(--color-on-primary);
  padding: var(--spacing-2) var(--spacing-4);
  z-index: 1000;
  transition: top var(--duration-fast) var(--easing-out);
}

.skip-link:focus {
  top: 0;
  outline: none;
  box-shadow: var(--glow-primary);
}

/* 屏幕阅读器专用 */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ========================================
   导航栏
   ======================================== */

.navbar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  backdrop-filter: blur(12px);
}

.navbar-container {
  display: flex;
  align-items: center;
  justify-content: space-between;
  max-width: 1440px;
  margin: 0 auto;
  padding: var(--spacing-4) var(--spacing-6);
  gap: var(--spacing-6);
}

.navbar-brand {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
}

.navbar-logo {
  width: 32px;
  height: 32px;
}

.navbar-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-semibold);
  color: var(--color-foreground);
}

.navbar-actions {
  display: flex;
  align-items: center;
  gap: var(--spacing-6);
}

/* 日/周/月切换按钮组 */
.toggle-group {
  display: inline-flex;
  background: var(--color-muted);
  border-radius: var(--radius-lg);
  padding: var(--spacing-1);
}

.toggle-btn {
  padding: var(--spacing-2) var(--spacing-4);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-fast) var(--easing-out);
  min-height: 44px;
  min-width: 44px;
}

.toggle-btn:hover {
  color: var(--color-foreground);
}

.toggle-btn.active {
  color: var(--color-on-primary);
  background: var(--color-primary);
  box-shadow: var(--shadow-sm);
}

.toggle-btn:focus-visible {
  outline: var(--ring-width) solid var(--color-ring);
  outline-offset: 2px;
}

/* 更新时间 */
.update-time {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.update-time-value {
  font-family: var(--font-family-mono);
  color: var(--color-text-secondary);
}

/* ========================================
   主要内容区
   ======================================== */

.main-content {
  max-width: 1440px;
  margin: 0 auto;
  padding: var(--spacing-6);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-8);
}

.section-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-semibold);
  color: var(--color-foreground);
  margin-bottom: var(--spacing-4);
}

/* ========================================
   总览卡片
   ======================================== */

.overview-card {
  background: linear-gradient(135deg, var(--color-surface) 0%, var(--color-muted) 100%);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-6);
  box-shadow: var(--shadow-lg);
}

.overview-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-6);
}

.overview-card-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-medium);
  color: var(--color-foreground);
}

.overview-card-badge {
  padding: var(--spacing-1) var(--spacing-3);
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-medium);
  color: var(--color-success);
  background: rgba(16, 185, 129, 0.1);
  border: 1px solid var(--color-success);
  border-radius: var(--radius-full);
}

.overview-card-body {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-6);
}

/* 核心指标 */
.metric-primary {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-2);
}

.metric-label {
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: var(--letter-spacing-wide);
}

.metric-value-group {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-2);
}

.metric-value {
  font-family: var(--font-family-mono);
  font-size: var(--font-size-5xl);
  font-weight: var(--font-weight-bold);
  line-height: 1;
  transition: color var(--duration-fast) var(--easing-out);
}

.metric-value.positive {
  color: var(--color-success);
}

.metric-value.negative {
  color: var(--color-destructive);
}

.metric-unit {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-muted);
}

/* 指标网格 */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-6);
}

.metric-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-1);
}

.metric-item .metric-value {
  font-size: var(--font-size-2xl);
}

/* ========================================
   策略卡片
   ======================================== */

.strategies-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-6);
}

.strategy-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-5);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
  transition: all var(--duration-normal) var(--easing-out);
  cursor: pointer;
}

.strategy-card:hover {
  border-color: var(--color-border-hover);
  transform: translateY(-4px);
  box-shadow: var(--shadow-xl);
}

.strategy-card:focus-within {
  border-color: var(--color-primary);
  box-shadow: var(--glow-primary);
}

.strategy-card-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
}

.strategy-icon {
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-muted);
  border-radius: var(--radius-lg);
}

.strategy-icon svg {
  width: 24px;
  height: 24px;
  color: var(--color-primary);
}

.strategy-name {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-semibold);
  color: var(--color-foreground);
}

.strategy-card-body {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
  flex: 1;
}

/* 策略主要指标 */
.strategy-metric-primary {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-1);
}

.strategy-metric-primary .metric-value {
  font-size: var(--font-size-3xl);
}

/* 策略次要指标 */
.strategy-metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-3);
}

.strategy-metrics .metric-item .metric-value {
  font-size: var(--font-size-lg);
}

/* 迷你趋势图 */
.strategy-sparkline {
  height: 40px;
  background: var(--color-muted);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.strategy-card-footer {
  padding-top: var(--spacing-3);
  border-top: 1px solid var(--color-border);
}

/* ========================================
   按钮
   ======================================== */

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-2);
  padding: var(--spacing-3) var(--spacing-5);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  border-radius: var(--radius-lg);
  border: none;
  cursor: pointer;
  transition: all var(--duration-fast) var(--easing-out);
  min-height: 44px;
  text-decoration: none;
}

.btn:focus-visible {
  outline: var(--ring-width) solid var(--color-ring);
  outline-offset: 2px;
}

.btn-primary {
  color: var(--color-on-primary);
  background: var(--color-accent);
}

.btn-primary:hover {
  background: #7C3AED;
  transform: translateY(-1px);
  box-shadow: var(--glow-accent);
}

.btn-ghost {
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
}

.btn-ghost:hover {
  color: var(--color-foreground);
  border-color: var(--color-border-hover);
  background: var(--color-muted);
}

.btn-sm {
  padding: var(--spacing-2) var(--spacing-3);
  min-height: 36px;
}

.btn-icon {
  width: 16px;
  height: 16px;
}

/* ========================================
   趋势图区域
   ======================================== */

.trend-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-6);
}

.trend-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-6);
}

.trend-controls {
  display: flex;
  gap: var(--spacing-2);
}

.trend-chart-container {
  height: 400px;
}

.trend-chart {
  width: 100%;
  height: 100%;
}

/* ========================================
   详情页样式
   ======================================== */

/* 详情概览 */
.detail-overview-card {
  background: linear-gradient(135deg, var(--color-surface) 0%, var(--color-muted) 100%);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-6);
}

.detail-metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--spacing-6);
}

.detail-metric-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-2);
  text-align: center;
}

.detail-metric-item .metric-value {
  font-size: var(--font-size-3xl);
}

/* 数据表格 */
.table-container {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  overflow: hidden;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
}

.data-table thead {
  background: var(--color-muted);
  border-bottom: 2px solid var(--color-border);
}

.data-table th {
  padding: var(--spacing-4) var(--spacing-5);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-semibold);
  color: var(--color-text-secondary);
  text-align: left;
  text-transform: uppercase;
  letter-spacing: var(--letter-spacing-wide);
}

.data-table th.sortable {
  cursor: pointer;
  user-select: none;
}

.data-table th.sortable:hover {
  color: var(--color-foreground);
}

.sort-icon {
  width: 14px;
  height: 14px;
  margin-left: var(--spacing-1);
  opacity: 0.5;
}

.data-table tbody tr {
  border-bottom: 1px solid var(--color-border);
  transition: background var(--duration-fast) var(--easing-out);
}

.data-table tbody tr:hover {
  background: rgba(245, 158, 11, 0.05);
}

.data-table tbody tr:focus-visible {
  outline: var(--ring-width) solid var(--color-ring);
  outline-offset: -3px;
  background: rgba(245, 158, 11, 0.08);
}

.data-table td {
  padding: var(--spacing-4) var(--spacing-5);
  font-size: var(--font-size-base);
  color: var(--color-foreground);
}

.cell-symbol {
  font-weight: var(--font-weight-medium);
}

.cell-number {
  font-family: var(--font-family-mono);
  text-align: right;
}

.cell-number.positive {
  color: var(--color-success);
}

.cell-number.negative {
  color: var(--color-destructive);
}

/* ========================================
   弹窗
   ======================================== */

.modal {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-6);
}

.modal[hidden] {
  display: none;
}

.modal-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(15, 23, 42, 0.8);
  backdrop-filter: blur(8px);
}

.modal-container {
  position: relative;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-2xl);
  width: 100%;
  max-width: 600px;
  max-height: 90vh;
  overflow: auto;
  box-shadow: var(--shadow-xl);
  animation: modal-enter var(--duration-normal) var(--easing-out);
}

@keyframes modal-enter {
  from {
    opacity: 0;
    transform: scale(0.95) translateY(10px);
  }
  to {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-5) var(--spacing-6);
  border-bottom: 1px solid var(--color-border);
}

.modal-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-semibold);
  color: var(--color-foreground);
}

.modal-body {
  padding: var(--spacing-6);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-6);
}

.modal-stats {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--spacing-4);
}

.modal-stat-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-1);
  padding: var(--spacing-4);
  background: var(--color-muted);
  border-radius: var(--radius-lg);
}

.modal-chart {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-3);
}

.modal-chart-title {
  font-size: var(--font-size-base);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-secondary);
}

.sparkline-chart {
  height: 120px;
  background: var(--color-muted);
  border-radius: var(--radius-md);
}

/* ========================================
   页脚
   ======================================== */

.footer {
  border-top: 1px solid var(--color-border);
  padding: var(--spacing-6);
  text-align: center;
}

.footer-text {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

/* ========================================
   加载状态
   ======================================== */

.skeleton {
  background: linear-gradient(
    90deg,
    var(--color-muted) 25%,
    var(--color-surface) 50%,
    var(--color-muted) 75%
  );
  background-size: 200% 100%;
  animation: skeleton-loading 1.5s ease-in-out infinite;
  border-radius: var(--radius-md);
}

@keyframes skeleton-loading {
  0% {
    background-position: 200% 0;
  }
  100% {
    background-position: -200% 0;
  }
}

/* ========================================
   数字变化动画
   ======================================== */

.metric-value.updating {
  animation: number-flash var(--duration-slow) var(--easing-out);
}

@keyframes number-flash {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}

/* ========================================
   Reduced Motion
   ======================================== */

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
  
  .strategy-card:hover {
    transform: none;
  }
  
  .btn-primary:hover {
    transform: none;
  }
}
```

### 3.2 响应式样式

```css
/* ========================================
   响应式设计
   ======================================== */

/* 平板 (768px - 1023px) */
@media (max-width: 1023px) {
  .strategies-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  
  .detail-metrics-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  
  .metrics-grid {
    grid-template-columns: repeat(3, 1fr);
  }
  
  .navbar-container {
    padding: var(--spacing-3) var(--spacing-4);
  }
  
  .main-content {
    padding: var(--spacing-4);
  }
}

/* 手机 (< 768px) */
@media (max-width: 767px) {
  .strategies-grid {
    grid-template-columns: 1fr;
  }
  
  .detail-metrics-grid {
    grid-template-columns: 1fr;
  }
  
  .metrics-grid {
    grid-template-columns: 1fr;
    gap: var(--spacing-4);
  }
  
  .navbar-container {
    flex-wrap: wrap;
    gap: var(--spacing-3);
  }
  
  .navbar-actions {
    width: 100%;
    justify-content: space-between;
  }
  
  .overview-card {
    padding: var(--spacing-4);
  }
  
  .metric-value {
    font-size: var(--font-size-4xl);
  }
  
  .strategy-metric-primary .metric-value {
    font-size: var(--font-size-2xl);
  }
  
  .trend-chart-container {
    height: 300px;
  }
  
  .modal-container {
    max-width: 100%;
    margin: var(--spacing-4);
  }
  
  .modal-stats {
    grid-template-columns: 1fr;
  }
  
  /* 表格横向滚动 */
  .table-container {
    overflow-x: auto;
  }
  
  .data-table {
    min-width: 600px;
  }
}
```

---

## 4. 交互设计

### 4.1 交互状态

#### 按钮状态

| 状态 | 视觉表现 | 说明 |
|------|---------|------|
| **默认** | 基础颜色 | 正常显示 |
| **悬停** | 颜色加深 + 微升起 | 鼠标悬停 |
| **按下** | 颜色再加深 + 缩小 | 鼠标按下 |
| **焦点** | 3px 金色焦点环 | 键盘聚焦 |
| **禁用** | 透明度 0.5 + 禁止光标 | 不可操作 |
| **加载** | 禁用 + 旋转图标 | 异步操作中 |

#### 卡片状态

| 状态 | 视觉表现 | 说明 |
|------|---------|------|
| **默认** | 基础样式 | 正常显示 |
| **悬停** | 边框高亮 + 上升 4px + 阴影增强 | 鼠标悬停 |
| **焦点** | 金色边框 + 发光效果 | 键盘聚焦/内部元素聚焦 |
| **选中** | 金色边框 + 背景微亮 | 当前选中 |

#### 表格行状态

| 状态 | 视觉表现 | 说明 |
|------|---------|------|
| **默认** | 基础样式 | 正常显示 |
| **悬停** | 背景微亮（金色 5% 透明度） | 鼠标悬停 |
| **焦点** | 3px 焦点环 + 背景更亮 | 键盘聚焦 |
| **选中** | 金色左边框 3px | 当前选中 |

### 4.2 动画设计

#### 页面入场动画

```css
/* 策略卡片依次入场 */
.strategy-card {
  animation: card-enter var(--duration-normal) var(--easing-out) backwards;
}

.strategy-card:nth-child(1) { animation-delay: 0ms; }
.strategy-card:nth-child(2) { animation-delay: 50ms; }
.strategy-card:nth-child(3) { animation-delay: 100ms; }

@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
```

#### 数字变化动画

```javascript
// 数字变化时的闪烁效果
function animateNumberChange(element, newValue) {
  element.classList.add('updating');
  
  // 格式化数字
  const formattedValue = formatNumber(newValue);
  element.textContent = formattedValue;
  
  // 移除动画类
  setTimeout(() => {
    element.classList.remove('updating');
  }, 350);
}
```

#### 弹窗动画

```css
/* 弹窗入场 */
.modal-container {
  animation: modal-enter var(--duration-normal) var(--easing-out);
}

@keyframes modal-enter {
  from {
    opacity: 0;
    transform: scale(0.95) translateY(10px);
  }
  to {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
}

/* 弹窗退场 */
.modal-container.closing {
  animation: modal-exit var(--duration-fast) var(--easing-in) forwards;
}

@keyframes modal-exit {
  from {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
  to {
    opacity: 0;
    transform: scale(0.95) translateY(10px);
  }
}
```

### 4.3 交互行为

#### 日/周/月切换

```javascript
class RangeToggle {
  constructor(container) {
    this.buttons = container.querySelectorAll('.toggle-btn');
    this.currentRange = 'day';
    
    this.buttons.forEach(btn => {
      btn.addEventListener('click', () => this.toggle(btn));
    });
  }
  
  toggle(activeBtn) {
    // 更新按钮状态
    this.buttons.forEach(btn => {
      btn.classList.remove('active');
      btn.setAttribute('aria-checked', 'false');
    });
    
    activeBtn.classList.add('active');
    activeBtn.setAttribute('aria-checked', 'true');
    
    // 更新数据范围
    this.currentRange = activeBtn.dataset.range;
    this.updateData();
  }
  
  async updateData() {
    // 显示加载状态
    showLoading();
    
    // 获取新数据
    const data = await fetchData(this.currentRange);
    
    // 更新UI
    updateMetrics(data);
    updateCharts(data);
    
    // 隐藏加载状态
    hideLoading();
  }
}
```

#### 卡片点击

```javascript
class StrategyCard {
  constructor(card) {
    this.card = card;
    this.detailBtn = card.querySelector('.btn-primary');
    
    // 整个卡片可点击
    card.addEventListener('click', (e) => {
      // 如果点击的是按钮，让按钮的默认行为处理
      if (e.target.closest('.btn-primary')) return;
      
      // 否则导航到详情页
      const strategy = card.dataset.strategy;
      window.location.href = `detail.html?strategy=${strategy}`;
    });
    
    // 键盘支持
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        const strategy = card.dataset.strategy;
        window.location.href = `detail.html?strategy=${strategy}`;
      }
    });
  }
}
```

#### 表格排序

```javascript
class SortableTable {
  constructor(table) {
    this.table = table;
    this.headers = table.querySelectorAll('th.sortable');
    this.currentSort = { column: null, direction: 'asc' };
    
    this.headers.forEach(header => {
      header.addEventListener('click', () => this.sort(header));
    });
  }
  
  sort(header) {
    const column = header.dataset.sort;
    
    // 切换排序方向
    if (this.currentSort.column === column) {
      this.currentSort.direction = 
        this.currentSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
      this.currentSort.column = column;
      this.currentSort.direction = 'asc';
    }
    
    // 更新排序图标
    this.updateSortIcons(header);
    
    // 排序数据
    this.sortData();
  }
  
  updateSortIcons(activeHeader) {
    this.headers.forEach(header => {
      const icon = header.querySelector('.sort-icon');
      icon.classList.remove('asc', 'desc');
    });
    
    const activeIcon = activeHeader.querySelector('.sort-icon');
    activeIcon.classList.add(this.currentSort.direction);
  }
  
  sortData() {
    const tbody = this.table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    
    rows.sort((a, b) => {
      const aVal = this.getCellValue(a, this.currentSort.column);
      const bVal = this.getCellValue(b, this.currentSort.column);
      
      const modifier = this.currentSort.direction === 'asc' ? 1 : -1;
      return (aVal - bVal) * modifier;
    });
    
    // 重新排列行
    rows.forEach(row => tbody.appendChild(row));
  }
}
```

#### 弹窗管理

```javascript
class Modal {
  constructor(modal) {
    this.modal = modal;
    this.backdrop = modal.querySelector('.modal-backdrop');
    this.closeBtn = modal.querySelector('.btn-close');
    this.previousFocus = null;
    
    this.closeBtn.addEventListener('click', () => this.close());
    this.backdrop.addEventListener('click', () => this.close());
    
    // ESC 键关闭
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !this.modal.hidden) {
        this.close();
      }
    });
  }
  
  open() {
    this.previousFocus = document.activeElement;
    this.modal.hidden = false;
    
    // 焦点陷阱
    this.trapFocus();
    
    // 焦点移到弹窗
    this.closeBtn.focus();
    
    // 禁止背景滚动
    document.body.style.overflow = 'hidden';
  }
  
  close() {
    // 添加退场动画
    const container = this.modal.querySelector('.modal-container');
    container.classList.add('closing');
    
    setTimeout(() => {
      this.modal.hidden = true;
      container.classList.remove('closing');
      
      // 恢复焦点
      if (this.previousFocus) {
        this.previousFocus.focus();
      }
      
      // 恢复背景滚动
      document.body.style.overflow = '';
    }, 150);
  }
  
  trapFocus() {
    const focusableElements = this.modal.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    
    const firstFocusable = focusableElements[0];
    const lastFocusable = focusableElements[focusableElements.length - 1];
    
    this.modal.addEventListener('keydown', (e) => {
      if (e.key !== 'Tab') return;
      
      if (e.shiftKey) {
        if (document.activeElement === firstFocusable) {
          e.preventDefault();
          lastFocusable.focus();
        }
      } else {
        if (document.activeElement === lastFocusable) {
          e.preventDefault();
          firstFocusable.focus();
        }
      }
    });
  }
}
```

---

## 5. ECharts 配置

### 5.1 主题配置

```javascript
// 金融科技深色主题
const fintechDarkTheme = {
  // 背景色
  backgroundColor: 'transparent',
  
  // 文字样式
  textStyle: {
    fontFamily: 'Inter, -apple-system, sans-serif',
    fontSize: 14,
    color: '#94A3B8' // --color-text-secondary
  },
  
  // 标题
  title: {
    textStyle: {
      fontFamily: 'Inter, -apple-system, sans-serif',
      fontSize: 18,
      fontWeight: 600,
      color: '#F8FAFC' // --color-foreground
    },
    subtextStyle: {
      fontSize: 14,
      color: '#94A3B8'
    }
  },
  
  // 图例
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
  
  // 提示框
  tooltip: {
    backgroundColor: '#1E293B', // --color-surface
    borderColor: '#334155', // --color-border
    borderWidth: 1,
    textStyle: {
      fontFamily: 'Inter, -apple-system, sans-serif',
      fontSize: 14,
      color: '#F8FAFC'
    },
    extraCssText: 'border-radius: 8px; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);'
  },
  
  // 坐标轴
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
        color: '#272F42', // --color-muted
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
  
  // 线图
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
  
  // 颜色
  color: [
    '#F59E0B', // Primary 金色
    '#8B5CF6', // Accent 紫色
    '#10B981', // Success 绿色
    '#3B82F6', // Info 蓝色
    '#EF4444', // Destructive 红色
    '#FBBF24', // Secondary 亮金色
    '#06B6D4', // Cyan
    '#EC4899'  // Pink
  ]
};

// 注册主题
echarts.registerTheme('fintech-dark', fintechDarkTheme);
```

### 5.2 收益趋势图配置

```javascript
function createTrendChart(containerId, data) {
  const chart = echarts.init(
    document.getElementById(containerId),
    'fintech-dark'
  );
  
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
      },
      formatter: (params) => {
        const date = params[0].axisValue;
        let html = `<div style="margin-bottom: 8px; font-weight: 600;">${date}</div>`;
        
        params.forEach(param => {
          const color = param.color;
          const name = param.seriesName;
          const value = formatNumber(param.value);
          const sign = param.value >= 0 ? '+' : '';
          
          html += `
            <div style="display: flex; align-items: center; gap: 8px; margin-top: 4px;">
              <span style="display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: ${color};"></span>
              <span style="flex: 1;">${name}</span>
              <span style="font-family: 'Fira Code', monospace; font-weight: 600;">${sign}${value}</span>
            </div>
          `;
        });
        
        return html;
      }
    },
    
    legend: {
      data: ['BTC_ETH', 'NEW_COIN', 'HRS'],
      top: 0,
      right: 0,
      itemWidth: 16,
      itemHeight: 8,
      itemGap: 16
    },
    
    xAxis: {
      type: 'category',
      data: data.dates,
      axisPointer: {
        label: {
          formatter: (params) => {
            return `日期: ${params.value}`;
          }
        }
      }
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
          return value;
        }
      }
    },
    
    series: [
      {
        name: 'BTC_ETH',
        type: 'line',
        data: data.btc_eth,
        emphasis: {
          focus: 'series'
        }
      },
      {
        name: 'NEW_COIN',
        type: 'line',
        data: data.new_coin,
        emphasis: {
          focus: 'series'
        }
      },
      {
        name: 'HRS',
        type: 'line',
        data: data.hrs,
        emphasis: {
          focus: 'series'
        }
      }
    ]
  };
  
  chart.setOption(option);
  
  // 响应式
  window.addEventListener('resize', () => {
    chart.resize();
  });
  
  return chart;
}
```

### 5.3 迷你趋势图（Sparkline）配置

```javascript
function createSparkline(canvasId, data, color = '#F59E0B') {
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  
  // 设置画布尺寸
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  
  // 计算数据范围
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  
  // 绘制渐变区域
  const gradient = ctx.createLinearGradient(0, 0, 0, rect.height);
  gradient.addColorStop(0, `${color}40`); // 25% 透明度
  gradient.addColorStop(1, `${color}05`); // 5% 透明度
  
  ctx.beginPath();
  ctx.moveTo(0, rect.height);
  
  data.forEach((value, index) => {
    const x = (index / (data.length - 1)) * rect.width;
    const y = rect.height - ((value - min) / range) * rect.height * 0.8 - rect.height * 0.1;
    ctx.lineTo(x, y);
  });
  
  ctx.lineTo(rect.width, rect.height);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();
  
  // 绘制线条
  ctx.beginPath();
  data.forEach((value, index) => {
    const x = (index / (data.length - 1)) * rect.width;
    const y = rect.height - ((value - min) / range) * rect.height * 0.8 - rect.height * 0.1;
    
    if (index === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.stroke();
}
```

---

## 6. 设计稿描述

### 6.1 首页视觉设计

#### 整体布局

```
┌─────────────────────────────────────────────────────────────┐
│  导航栏 (sticky)                                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Logo  量化交易看板          [日|周]  最后更新: ...     │  │
│  └───────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  总览卡片区域                                                 │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 总账户收益                              [实时]         │  │
│  │                                                         │  │
│  │ 总盈亏                                                  │  │
│  │ +125,680.50 USDT  ← 大数字，绿色                        │  │
│  │                                                         │  │
│  │ 总胜率        总平仓数      总委托数                    │  │
│  │ 68.5%         1,245         2,890                       │  │
│  └───────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  策略概览                                                     │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐           │
│  │ BTC_ETH     │ │ NEW_COIN    │ │ HRS         │           │
│  │             │ │             │ │             │           │
│  │ 当日盈亏    │ │ 当日盈亏    │ │ 当日盈亏    │           │
│  │ +45,230.80  │ │ -12,450.30  │ │ +8,920.50   │           │
│  │             │ │             │ │             │           │
│  │ 胜率 平仓 委托│ │ 胜率 平仓 委托│ │ 胜率 平仓 委托│           │
│  │ 72%  456 890│ │ 58% 234 567│ │ 65% 345 678│           │
│  │             │ │             │ │             │           │
│  │ [趋势图]    │ │ [趋势图]    │ │ [趋势图]    │           │
│  │             │ │             │ │             │           │
│  │ [查看详情]  │ │ [查看详情]  │ │ [查看详情]  │           │
│  └─────────────┘ └─────────────┘ └─────────────┘           │
├─────────────────────────────────────────────────────────────┤
│  收益趋势                                                     │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 收益趋势                    [近7天] [近4周] [近3月]    │  │
│  │                                                         │  │
│  │ [ECharts 折线图]                                        │  │
│  │                                                         │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

#### 视觉层次

1. **第一层级（最重要）**: 总盈亏数字
   - 字号: 48px
   - 字重: 700 (Bold)
   - 颜色: 盈利绿色 / 亏损红色
   - 位置: 总览卡片中心

2. **第二层级**: 策略卡片盈亏数字
   - 字号: 30px
   - 字重: 700 (Bold)
   - 颜色: 盈利绿色 / 亏损红色
   - 位置: 各策略卡片顶部

3. **第三层级**: 次要指标（胜率、平仓数等）
   - 字号: 24px (总览) / 18px (策略卡片)
   - 字重: 500 (Medium)
   - 颜色: 主要文字色

4. **第四层级**: 标签、提示文字
   - 字号: 14px
   - 字重: 400 (Normal)
   - 颜色: 次要文字色
   - 大写 + 字间距

#### 色彩应用

| 元素 | 颜色 | 说明 |
|------|------|------|
| 背景 | `#0F172A` | 深蓝黑，降低视觉疲劳 |
| 卡片背景 | `#1E293B` | 比背景稍亮，层次分明 |
| 盈利数字 | `#10B981` | 绿色，传达积极信号 |
| 亏损数字 | `#EF4444` | 红色，传达警示信号 |
| 主要文字 | `#F8FAFC` | 浅色，高对比度 |
| 次要文字 | `#94A3B8` | 中灰，不抢眼 |
| 边框 | `#334155` | 低对比度，不干扰 |
| CTA按钮 | `#8B5CF6` | 紫色，科技感 |

### 6.2 详情页视觉设计

#### 整体布局

```
┌─────────────────────────────────────────────────────────────┐
│  导航栏                                                       │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ [← 返回]  BTC_ETH 策略详情              [日|周]        │  │
│  └───────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  策略概览                                                     │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  总盈亏          胜率          平仓数        委托数   │  │
│  │  +45,230.80      72.3%         456           890      │  │
│  │  USDT                                                   │  │
│  └───────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  币种明细                                                     │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 交易对    委托   成交   平仓   盈亏          胜率  操作 │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │ BTCUSDT   125    118    56    +12,450.30    75%   [👁] │  │
│  │ ETHUSDT   98     95     42    +8,920.50     70%   [👁] │  │
│  │ ...                                                     │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

#### 表格设计

- **表头**: 深色背景，大写字母，字间距加宽
- **数据行**: 
  - 悬停: 金色 5% 透明度背景
  - 焦点: 3px 焦点环
  - 数字: 等宽字体，右对齐
  - 盈亏: 绿色/红色
- **排序**: 点击表头，图标切换升序/降序

#### 弹窗设计

```
┌─────────────────────────────────────┐
│  BTCUSDT 详情                   [×] │
├─────────────────────────────────────┤
│  总盈亏          胜率               │
│  +12,450.30 USDT  75.0%             │
│                                     │
│  委托数          成交数             │
│  125             118                │
│                                     │
│  平仓数                              │
│  56                                  │
│                                     │
│  近7日盈亏趋势                       │
│  [迷你折线图]                        │
└─────────────────────────────────────┘
```

### 6.3 间距规范

| 元素 | 间距 | 说明 |
|------|------|------|
| 页面边距 | 24px | main-content padding |
| 卡片内边距 | 20px | padding |
| 卡片间距 | 24px | gap |
| 区域间距 | 32px | section gap |
| 指标间距 | 16px | metric gap |
| 按钮间距 | 8px | button gap |
| 表格单元格 | 20px | padding |

---

## 7. 响应式设计

### 7.1 断点系统

| 断点 | 宽度范围 | 设备类型 |
|------|---------|---------|
| `sm` | < 768px | 手机 |
| `md` | 768px - 1023px | 平板 |
| `lg` | ≥ 1024px | 桌面 |

### 7.2 布局变化

#### 桌面 (≥ 1024px)

- 策略卡片: 3列网格
- 总览指标: 3列网格
- 详情指标: 4列网格
- 表格: 完整显示

#### 平板 (768px - 1023px)

- 策略卡片: 2列网格
- 总览指标: 3列网格（保持）
- 详情指标: 2列网格
- 表格: 完整显示

#### 手机 (< 768px)

- 策略卡片: 1列网格
- 总览指标: 1列网格
- 详情指标: 1列网格
- 表格: 横向滚动
- 导航栏: 换行布局
- 弹窗: 全宽

### 7.3 字体缩放

| 元素 | 桌面 | 平板 | 手机 |
|------|------|------|------|
| 大数字 | 48px | 42px | 36px |
| 策略盈亏 | 30px | 26px | 24px |
| 页面标题 | 30px | 26px | 24px |
| 卡片标题 | 20px | 18px | 18px |
| 正文 | 16px | 16px | 16px |

---

## 8. 无障碍设计

### 8.1 键盘导航

- **Tab 顺序**: 符合视觉顺序
- **焦点环**: 3px 金色焦点环，清晰可见
- **跳过链接**: "跳到主要内容"链接
- **弹窗焦点陷阱**: Tab 键在弹窗内循环
- **ESC 关闭**: ESC 键关闭弹窗

### 8.2 屏幕阅读器支持

- **语义化标签**: `header`, `main`, `nav`, `section`, `article`, `footer`
- **ARIA 标签**: `aria-label`, `aria-labelledby`, `aria-describedby`
- **角色定义**: `role="dialog"`, `role="radiogroup"`, `role="grid"`
- **状态通知**: `aria-live="polite"` 用于动态内容更新
- **表格标记**: `scope="col"`, `aria-sort`

### 8.3 颜色对比度

| 元素 | 对比度 | 标准 |
|------|--------|------|
| 主要文字 | 15.3:1 | AAA |
| 次要文字 | 4.6:1 | AA |
| 盈利数字 | 5.1:1 | AA |
| 亏损数字 | 4.7:1 | AA |
| 焦点环 | 3.2:1 | AA (大元素) |

### 8.4 动画

- **Reduced Motion**: 尊重 `prefers-reduced-motion`
- **禁用动画**: 用户设置后，所有动画禁用
- **保留过渡**: 仅保留颜色变化，移除位移/缩放

### 8.5 触摸目标

- **最小尺寸**: 44x44px
- **间距**: 触摸目标间距 ≥ 8px
- **扩展区域**: 小图标使用 `hitSlop` 扩展触摸区域

---

## 附录

### A. 文件结构

```
dashboard/frontend/
├── index.html              # 首页（总览仪表板）
├── detail.html             # 详情页（单个策略）
├── css/
│   └── style.css           # 全局样式
└── js/
    ├── api.js              # API调用封装
    ├── charts.js           # 图表配置（ECharts封装）
    ├── config.js           # 前端配置（API地址、图表颜色、CDN地址等）
    ├── main.js             # 首页主逻辑
    └── vendor/
        └── echarts.min.js  # ECharts本地库文件（优先加载）
```

### B. 依赖库

| 库 | 版本 | 用途 |
|----|------|------|
| ECharts | 5.x | 数据可视化 |
| Inter | - | 主字体 |
| Fira Code | - | 等宽字体 |

### C. 浏览器支持

- Chrome ≥ 90
- Firefox ≥ 88
- Safari ≥ 14
- Edge ≥ 90

---

**文档版本**: v1.0  
**最后更新**: 2026-06-02  
**设计师**: UI 设计师
