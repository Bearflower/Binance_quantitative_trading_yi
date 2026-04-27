# 统一基础设施服务文档索引

**项目**: 统一基础设施服务 (common_service)  
**版本**: v1.0  
**创建日期**: 2026-04-20  
**最后更新**: 2026-04-20

---

## 📂 文档结构

```
binance_common_service/
├── README.md                          # 项目总览（根目录）
├── docs/
│   ├── README.md                      # 本文档（文档索引）
│   │
│   ├── requirements/                  # 需求文档
│   │   └── 统一基础设施服务需求分析.md
│   │
│   ├── designs/                       # 设计文档
│   │   └── 技术架构设计.md
│   │
│   ├── plans/                         # 计划文档
│   │   └── 实施计划.md
│   │
│   └── reports/                       # 报告文档（后续）
│       └── (待创建)
│
└── src/                               # 源代码（后续创建）
```

---

## 📋 文档清单

### 0. 入门文档

| 文档名称 | 路径 | 说明 | 状态 |
|---------|------|------|------|
| README.md | `/README.md` | 项目总览、快速开始 | ✅ 已完成 |
| 文档索引 | `/docs/README.md` | 本文档，文档导航 | ✅ 已完成 |

### 1. 需求文档

| 文档名称 | 路径 | 说明 | 状态 |
|---------|------|------|------|
| 需求分析 | `docs/requirements/统一基础设施服务需求分析.md` | 完整需求规格说明 | ✅ 已完成 |

**包含内容**:
- 项目概述
- 业务背景
- 功能性需求（K 线数据服务 + 通知服务）
- 非功能性需求（性能、可靠性、安全性）
- 技术约束
- 验收标准

### 2. 设计文档

| 文档名称 | 路径 | 说明 | 状态 |
|---------|------|------|------|
| 技术架构 | `docs/designs/技术架构设计.md` | 系统架构、技术选型 | ✅ 已完成 |

**包含内容**:
- 架构概述
- 系统架构（服务拆分、模块职责）
- 技术架构（技术栈、目录结构）
- 数据架构（数据库设计、Redis 设计）
- 接口设计（API 详细定义）
- 部署架构（Docker Compose、环境变量）
- 安全设计
- 监控与日志

### 3. 计划文档

| 文档名称 | 路径 | 说明 | 状态 |
|---------|------|------|------|
| 实施计划 | `docs/plans/实施计划.md` | 详细实施路线图 | ✅ 已完成 |

**包含内容**:
- 实施概述
- 阶段划分（5 个阶段）
- 详细任务清单（WBS）
- 时间估算（12-15 个工作日）
- 资源需求
- 风险管理
- 验收标准

### 4. 报告文档（待创建）

| 文档名称 | 路径 | 说明 | 状态 |
|---------|------|------|------|
| 阶段报告 | `docs/reports/` | 各阶段完成报告 | ⏳ 待创建 |
| 测试报告 | `docs/reports/` | 集成测试报告 | ⏳ 待创建 |
| 部署报告 | `docs/reports/` | 生产环境部署报告 | ⏳ 待创建 |

---

## 🎯 快速导航

### 按角色查看

**项目经理**:
- [需求分析](docs/requirements/统一基础设施服务需求分析.md) - 了解项目范围
- [实施计划](docs/plans/实施计划.md) - 跟踪进度

**架构师**:
- [技术架构](docs/designs/技术架构设计.md) - 系统设计
- [需求分析](docs/requirements/统一基础设施服务需求分析.md) - 技术约束

**开发工程师**:
- [技术架构](docs/designs/技术架构设计.md) - 实现细节
- [实施计划](docs/plans/实施计划.md) - 任务清单
- [README](README.md) - 快速开始

**测试工程师**:
- [需求分析](docs/requirements/统一基础设施服务需求分析.md) - 验收标准
- [实施计划](docs/plans/实施计划.md) - 测试计划

**运维工程师**:
- [技术架构](docs/designs/技术架构设计.md) - 部署架构
- [README](README.md) - 部署步骤

### 按阶段查看

**Phase 1 - 基础框架**:
- [技术架构](docs/designs/技术架构设计.md) - 目录结构
- [实施计划](docs/plans/实施计划.md#phase-1-基础框架搭建-day-1-2)

**Phase 2 - 通知服务**:
- [技术架构](docs/designs/技术架构设计.md#52-通知服务-api) - API 设计
- [实施计划](docs/plans/实施计划.md#phase-2-通知服务开发-day-3-5)

**Phase 3 - K 线数据**:
- [技术架构](docs/designs/技术架构设计.md#51-k-线数据服务-api) - API 设计
- [实施计划](docs/plans/实施计划.md#phase-3-k-线数据服务开发-day-6-9)

**Phase 4 - 集成测试**:
- [需求分析](docs/requirements/统一基础设施服务需求分析.md#8-验收标准) - 验收标准
- [实施计划](docs/plans/实施计划.md#phase-4-集成测试-day-10-11)

**Phase 5 - 部署上线**:
- [技术架构](docs/designs/技术架构设计.md#6-部署架构) - 部署指南
- [实施计划](docs/plans/实施计划.md#phase-5-部署上线-day-12)
- [README](README.md) - 快速部署

---

## 📊 文档状态

### 已完成 ✅

- [x] README.md - 项目总览
- [x] 文档索引 - 本文档
- [x] 需求分析文档
- [x] 技术架构文档
- [x] 实施计划文档

### 进行中 🔄

- [ ] 源代码实现
- [ ] 单元测试用例
- [ ] API 文档（Swagger）

### 待创建 ⏳

- [ ] 阶段完成报告
- [ ] 测试报告
- [ ] 部署报告
- [ ] 运维手册
- [ ] 用户使用指南

---

## 🔗 外部链接

### 技术文档

- [FastAPI 官方文档](https://fastapi.tiangolo.com/)
- [PostgreSQL 文档](https://www.postgresql.org/docs/)
- [Redis 文档](https://redis.io/docs/)
- [APScheduler 文档](https://apscheduler.readthedocs.io/)

### 币安 API

- [币安合约 API 文档](https://binance-docs.github.io/apidocs/futures/cn/)
- [币安 API 限流规则](https://binance-docs.github.io/apidocs/futures/cn/#websockets)

### 飞书开放平台

- [飞书机器人 API](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN)

---

## 📝 文档规范

### 命名规范

- 文件名：使用中文，清晰描述内容
- 日期格式：YYYY-MM-DD（如 2026-04-20）
- 版本号：v1.0, v1.1, v2.0（语义化版本）

### 文档模板

所有文档应包含：
- 标题
- 版本信息
- 创建/更新日期
- 目录（长文档）
- 正文内容
- 修订历史（可选）

### 更新流程

1. 修改文档
2. 更新版本号
3. 更新修订历史
4. 提交 Git

---

## 🤔 常见问题

**Q: 文档在哪里？**  
A: 所有文档都在 `/docs/` 目录下，按类型分子目录。

**Q: 如何开始开发？**  
A: 先阅读 [README](README.md) 了解项目，然后查看 [技术架构](docs/designs/技术架构设计.md) 和 [实施计划](docs/plans/实施计划.md)。

**Q: API 接口在哪里？**  
A: 详见 [技术架构设计 - 接口设计章节](docs/designs/技术架构设计.md#5-接口设计)。

**Q: 如何部署？**  
A: 详见 [README - 快速开始](README.md#-快速开始)。

**Q: 实施计划是什么？**  
A: 详见 [实施计划文档](docs/plans/实施计划.md)，包含 5 个阶段、12-15 个工作日的详细任务。

---

## 📞 联系方式

- 项目仓库：[GitHub](https://github.com/your-repo)
- 问题反馈：GitHub Issues
- 文档维护：项目团队

---

**最后更新**: 2026-04-20
