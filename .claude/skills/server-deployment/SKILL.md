---
name: server-deployment
description: 服务器自动化部署（主项目专用）。提供 SSH 免密登录配置、项目打包、上传、Docker 部署自动化与部署后验证的完整流程。部署项目到远程服务器、管理 Docker 容器、排查部署问题（部署幻觉、文件遗漏、容器未更新）时使用。
---

# 服务器自动化部署（入口）

**完整技能文档（必读）**：`skills/server-deployment/SKILL.md`（本目录下的完整版）
**部署规则（强制）**：`.claude/rules/deployment.md` —— 变更范围分析 + 防部署幻觉五层验证 + 部署确认报告

触发本技能时，先完整阅读上述文档，再按项目部署规则执行。

## 关键信息速查

- **生产服务器**：`43.156.242.184`（root，SSH 密钥 `/Users/yl/vscode/inspection_automation/docs/only.pem`，权限必须 600，禁止提交进 Git）
- **一键部署入口**：`./one_click_deploy.sh`（打包→上传→部署→验证一体）
- **部署验证**：`./verify_deployment.sh`
- **变更范围分析**：按 `git diff` 对照变更-容器映射表，按需重建（`shared/` 变更 → 重建全部策略容器 + ai-tuner；`.env` 变更 → 仅重启；hrs 的 Dockerfile 是 `COPY . /app/`，任何项目文件变更都要重建 hrs）

## 铁律

1. 部署后必须验证"容器内代码 == 本地代码"（VERSION 文件 + MD5 对比），**容器在运行 ≠ 运行的是新代码**
2. 部署后 `docker ps` 确认所有服务在运行（尤其 kline-monitor 易被 `set -e` 中断后遗漏启动）
3. 本地 macOS 计算 MD5 用 `md5 -q`，服务器 Linux 用 `md5sum`
