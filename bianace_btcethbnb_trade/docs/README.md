# 项目文档目录

本目录存放项目的所有文档，按照以下结构组织:

## 目录结构

```
docs/
├── deployment/     # 部署类文档 - 服务器部署、Docker 配置等
├── design/         # 设计类文档 - 系统设计、模块设计、接口设计等
├── proposals/      # 方案类文档 - 技术方案、使用指南、实施总结等
├── archive/        # 归档文档 - 历史版本报告、过时文档等
│   ├── v6.12/      # v6.12 版本相关文档
│   ├── v6.13/      # v6.13 版本相关文档
│   ├── v6.13.2/    # v6.13.2 版本相关文档
│   ├── v6.13.3/    # v6.13.3 版本相关文档
│   ├── v6.14.0/    # v6.14.0 版本相关文档
│   └── other/      # 其他历史文档
└── README.md       # 本文档 (文档目录索引)
```

## 各目录说明

### deployment/ (部署文档)
- 服务器部署指南
- Docker 配置说明
- 环境搭建文档
- 自动化部署脚本说明

### design/ (设计文档)
- 系统架构设计
- 模块详细设计
- 接口定义文档
- 数据库设计

### proposals/ (方案文档)
- 技术方案设计
- 使用指南
- 快速开始
- 实施总结
- 任务清单
- **项目需求迭代文档** (主需求文档，由根目录 readme.md 迁移)

### archive/ (归档文档)
- 历史版本报告（按版本号分类）
- 过时的技术文档
- 已完成任务的详细报告
- 问题分析和修复记录

## 核心文档索引

### 项目主文档

| 文档名称 | 路径 | 说明 |
|---------|------|------|
| **项目需求迭代文档** | [proposals/项目需求迭代文档.md](proposals/项目需求迭代文档.md) | 项目主需求文档，包含完整功能说明、版本迭代历史、使用指南 |
| **技术架构文档** | [design/技术架构文档.md](design/技术架构文档.md) | 系统技术架构设计、模块依赖、数据流、部署架构 |
| **重构第一阶段实施报告** | [reports/重构第一阶段实施报告.md](reports/重构第一阶段实施报告.md) | 配置统一与基础设施重构实施报告（2026-04-27） |

### 快速开始

| 文档名称 | 路径 | 适用场景 |
|---------|------|---------|
| 快速开始指南 | [proposals/QUICKSTART.md](proposals/QUICKSTART.md) | 新手快速上手 |
| 回测模块使用指南 | [proposals/回测模块使用指南.md](proposals/回测模块使用指南.md) | 使用回测功能 |
| 通用模块使用说明 | [proposals/通用模块使用说明.md](proposals/通用模块使用说明.md) | 飞书通知、K 线数据服务使用说明 |
| 如何修改执行时间 | [proposals/如何修改执行时间.md](proposals/如何修改执行时间.md) | 修改调度器执行时间 |
| AI 集成使用指南 | [proposals/AI 集成使用指南.md](proposals/AI 集成使用指南.md) | DeepSeek AI 集成使用 |

### 设计文档

| 文档名称 | 路径 | 说明 |
|---------|------|------|
| PM账户订单查询接口实现 | [design/PM账户订单查询接口实现.md](design/PM账户订单查询接口实现.md) | PM 账户订单查询接口设计 |
| 交易结果跟踪系统 - 需求分析 | [design/交易结果跟踪系统 - 需求分析.md](design/交易结果跟踪系统 - 需求分析.md) | 交易结果跟踪系统需求 |
| 交易结果跟踪系统 - 实现清单 | [design/交易结果跟踪系统 - 实现清单.md](design/交易结果跟踪系统 - 实现清单.md) | 交易结果跟踪系统实现清单 |
| 交易结果跟踪系统 - 实现总结 | [design/交易结果跟踪系统 - 实现总结.md](design/交易结果跟踪系统 - 实现总结.md) | 交易结果跟踪系统实现总结 |

### 部署文档

| 文档名称 | 路径 | 说明 |
|---------|------|------|
| 双数据源部署文档 | [deployment/double_data_source_deployment.md](deployment/double_data_source_deployment.md) | 双数据源部署方案 |
| 部署报告 20260422 | [deployment/deployment_report_20260422.md](deployment/deployment_report_20260422.md) | 部署验证报告 |

### 版本发布说明

| 版本 | 发布说明 | 说明 |
|------|---------|------|
| v6.14.0 | [proposals/v6140_release_notes.md](proposals/v6140_release_notes.md) | 通用 K 线服务集成 |

### 新增基础设施文档 (v1.0.0)

| 文档名称 | 路径 | 说明 |
|---------|------|------|
| 统一配置管理器 | [../config/config_manager.py](../config/config_manager.py) | 统一配置管理器，支持 YAML 配置、环境变量覆盖、配置验证 |
| 统一配置文件 | [../config/config.yaml](../config/config.yaml) | 统一配置文件，整合所有非敏感配置 |
| 自定义异常类 | [../utils/exceptions.py](../utils/exceptions.py) | 自定义异常类，提供清晰的错误分类 |
| 统一错误处理器 | [../utils/error_handler.py](../utils/error_handler.py) | 统一错误处理器，支持日志记录、错误统计、飞书通知 |
| 统一日志配置 | [../utils/logger.py](../utils/logger.py) | 统一日志配置，支持多处理器和日志滚动 |

### 重构模块文档 (v1.0.0 - 2026-04-27)

**阶段二：核心模块重构**

| 模块名称 | 路径 | 重构说明 |
|---------|------|---------|
| 调度器模块 | [../scheduler/](../scheduler/) | 将 scheduler_new.py (904行) 拆分为6个子模块 |
| 信号模块 | [../core/signal/](../core/signal/) | 将 signal_detector.py (502行) 拆分为3个子模块 |
| 评分模块 | [../core/scoring/](../core/scoring/) | 重构为工厂模式，支持版本管理 |
| 数据模块 | [../core/data/](../core/data/) | 将 data_fetcher.py (563行) 拆分为3个子模块 |

**阶段三：服务层重构**

| 模块名称 | 路径 | 重构说明 |
|---------|------|---------|
| 服务基类 | [../services/base.py](../services/base.py) | 创建统一的BaseService，规范服务层接口 |
| 数据仓库 | [../models/repository.py](../models/repository.py) | 引入Repository Pattern，分离数据访问逻辑 |
| 数据实体 | [../models/entities.py](../models/entities.py) | 具体数据仓库实现 |

**重构成果**:
- ✅ 104个测试通过
- ✅ 代码质量评分：97.7/100
- ✅ 模块职责更清晰
- ✅ 可测试性显著提升
- ✅ 缓存性能优化（TTL + LRU）
- ✅ 并发数据获取支持

## 归档文档索引

### v6.14.0 版本文档 (2026-04-21)
- K 线服务修复进展报告
- K 线服务修复验证报告
- K 线服务重新对接完成报告
- K 线服务验证报告
- 胜率统计 Bug 修复报告
- 胜率统计修复完成报告
- 胜率统计功能完整修复报告
- CHANGELOG_20260421_kline_integration

### v6.13.3 版本文档
- v6133_deployment_verification.md
- v6133_backtest_analysis.md
- v6133_backtest_comparison.md
- v6133_bug_fix_report.md
- v6133_optimization_plan.md
- v6133_vs_v6132_comparison.md

### v6.13.2 版本文档 (2026-04-13)
- v6132 部署验证报告
- v6132 限价单优化方案
- document_update_v6132.md
- limit_order_optimization_v6132_deployment.md

### v6.13 版本文档 (2026-04-10)
- v613 动态仓位调整更新报告
- v613 回测报告
- v613 回测数据问题分析
- v613_vs_v6131 对比报告
- v613_vs_v6131_夏普比率与回撤深度对比

### v6.12 版本文档 (2026-04-07)
- v612频率控制更新报告
- v612_scoring_engine_verification.md
- 500U 合约交易规范_v6.12.md

### 其他历史文档
- 各类部署报告
- 问题分析报告
- Bug 修复报告
- 优化报告

**完整归档文档列表请查看 `archive/` 目录**

## 文档管理原则

1. **唯一 README 原则**: 项目根目录不再保留 readme.md，主需求文档存储在 `docs/proposals/项目需求迭代文档.md`
2. **分类存储**: 所有文档按类型分类存储到对应子目录
3. **版本归档**: 历史版本文档按版本号归档到 `archive/` 目录，便于历史追溯
4. **及时归档**: 新增文档应及时归类到相应目录，过时文档移至归档目录
5. **索引维护**: 本文档 (docs/README.md) 应维护核心文档索引，便于查找
6. **不删除原则**: 不删除任何文档，只移动到归档目录，保留完整历史记录

## 文档整理记录

**整理日期**: 2026-04-27

**整理内容**:
1. 创建 `archive/` 归档目录，按版本号分类存储历史文档
2. 将 `reports/` 目录下 47 个历史报告归档到对应版本目录
3. 合并重复文档，保留更完整的"技术架构文档.md"
4. 归类根目录下的文档到合适的子目录
5. 更新文档索引，建立清晰的文档导航

**整理结果**:
- 活跃文档: 18 个（deployment: 2, design: 5, proposals: 7, README: 1, archive 索引: 3）
- 归档文档: 47 个（按版本分类存储）
- 文档结构更清晰，便于查找和维护
