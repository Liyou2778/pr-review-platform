# 项目状态（最终）

> 2026-11-18。代码已推送到 <https://github.com/Liyou2778/pr-review-platform>（66 个文件）。
> 真实评审已在 <https://github.com/Liyou2778/pr-review-demo/pull/1> 跑通：1 条 review + 4 条行内评论。
> Docker 引擎可用（29.9.3），但 `postgres:16-alpine` / `python:3.12-slim` 受网络限制拉不下来；
> 主链路走便携版 PG 17.6 + Redis 5，功能等价。

## 一、完成度

| 阶段 | 内容 | 证据 |
|---|---|---|
| P0 | 平台骨架：验签、幂等、队列、worker、规则引擎、行号、去重、反馈、指标 | 40 tests |
| P1 | OCR + DeepSeek：workspace 克隆 → review → 归一化 → 落库 | e2e 19s / $0.019 |
| P1.5 | 评论回写：真实 PR 上 1 review + 4 行内评论；失败可补发 | demo PR #1 |
| P2 | 20 用例 bench（Py/TS/Java）、两级门禁、录制回放、误报聚类、看板 | 见下表 |
| P2.5 | 规则候选 draft→approved/rejected 人工门禁 | 契约测试 |
| P3 | 仓库策略、resolved 状态、投递重试、Prometheus、JSON 日志、保留脚本、worker 并发 | 8 个运维测试 |

## 二、实测数字

| 指标 | 值 |
|---|---|
| 契约测试 | 23 passed（SQLite + Postgres 双方言） |
| 规则层 bench | 20 用例，tp=25 fp=0 fn=0，P/R/F1 = 1.000 |
| 真实模型录制 bench | 20 用例，P=0.769 R=0.800 F1=0.784，$0.0265/次 |
| 真实模型单 PR | 19.1s，62k/1.9k tokens，$0.0189 |
| 平台处理 | 规则引擎 P95 37ms |
| 安全 | 变更接口 `X-API-Token`、clone 白名单、repo 级 token、prod 启动校验（5 个测试） |
| 门禁 | F1 降 1%（规则）/ 3%（模型）或低于地板即 exit 1 |

## 三、没走完的两件事

1. **Docker compose 全链路**：`docker compose config` 通过、redis/alpine 镜像已拉取，
   postgres/python 基础镜像因网络限速拉取失败。配好可用镜像源或代理后：
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\services.ps1 stop
   docker compose up --build
   ```
2. **GitHub 真评论**：需要 fine-grained PAT（Pull requests RW）。填 `.env` 后
   `GITHUB_DRY_RUN=false`，用 `/api/replay` 跑真实公开 PR 即可。

## 四、关键文件

| 文件 | 作用 |
|---|---|
| `README.md` | 总览、快速开始、API、设计决策 |
| `docs/environment.md` | 环境安装与踩坑（PG/Redis/OCR/模型） |
| `docs/demo-script.md` | 2 分钟演示脚本 |
| `docs/resume.md` | 简历 bullet（中文/英文）+ 诚实边界 |
| `docs/review-2026-11-18.md` | 交叉检查报告：已修 5 项 + P0/P1/P2 backlog |
| `eval/bench/cases.json` | 20 个标注用例 |
| `eval/bench/recorded-ocr.json` | 真实模型录制输出（CI 无 key 回归） |
| `.github/workflows/ci.yml` | 测试 + 两级 bench + 门禁 |
