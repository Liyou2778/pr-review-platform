# 简历写法（只用本项目的真实数字）

> 规则：每条 bullet 都要能被面试官追问到「怎么测的、样本多大、基线是什么」。
> 下面所有数字都来自本仓库的实测输出，复现命令在括号里。
>
> 公开证据：仓库 <https://github.com/Liyou2778/pr-review-platform>（CI 绿）、
> 真实评审 PR <https://github.com/Liyou2778/pr-review-demo/pull/1>（1 review + 4 行内评论）。

## 一、中文版（主项目，4 条）

> **企业级 PR 评审 Agent 平台｜个人项目（基于阿里开源 Open Code Review v1.13.11）**
>
> - 把单机评审 CLI 做成组织级服务：GitHub Webhook HMAC 验签 + `delivery_id` 幂等 + Redis 队列 + worker 原子 claim，`(repo, pr, head_sha)` 唯一约束保证重投不重跑，跨 run `dedup_key` 去重避免重复评论；规则引擎 5 个 run/12 条 findings，P95 处理 37ms。（`pytest -q`、`GET /api/dashboard`）
> - 接入上游 `open-code-review` 与 DeepSeek：自建 git workspace 克隆、结果归一化（`critical→blocker`、`content/start_line` 映射、severity 词表对齐）与成本估算，单 PR 端到端 19s、62k/1.9k tokens、$0.0189。（`python scripts/e2e_ocr.py`）
> - 建 20 用例标注 bench（Python/TypeScript/Java）与两级 CI 门禁：确定性引擎 F1 1.000（`tp=25 fp=0 fn=0`），真实模型录制回归 P=0.769 / R=0.800 / F1=0.784，F1 下降超阈值即阻断合并；23 个契约测试在 SQLite 与 Postgres 双方言通过。（`scripts/bench.py`、`scripts/gate.py`）
> - 设计误报闭环：反馈落库 → 按 `rule_key` 聚类 → 规则候选 `draft→approved/rejected` 人工门禁；评论投递失败不影响评审结果（findings 先落库、`comment_status` 单独记录，`POST /api/runs/{id}/post` 可补发、不重复计费）。

## 二、英文版

> **Enterprise PR Review Agent Platform** — personal project on Alibaba's Open Code Review v1.13.11
> - Turned a single-machine review CLI into an org-level service: HMAC-verified GitHub webhooks + delivery-id idempotency + Redis queue + atomic worker claim; unique `(repo, pr, head_sha)` prevents re-runs and `dedup_key` prevents duplicate comments (P95 37 ms on the deterministic engine).
> - Integrated the upstream OCR CLI with DeepSeek: git workspace checkout, output normalisation (`critical→blocker`, `content/start_line` mapping, severity vocabulary) and per-run cost estimation; one PR end-to-end in 19 s for $0.019.
> - Built a 20-case labelled bench (Python/TypeScript/Java) and a two-tier CI gate: deterministic engine F1 1.000, recorded real-model run P=0.769 / R=0.800 / F1=0.784; merges blocked on regression. 23 contract tests pass on both SQLite and Postgres.
> - Implemented the false-positive loop: feedback → cluster by rule → rule candidates with a human `draft→approved/rejected` gate; comment delivery failures never lose the review (findings persist, `comment_status` recorded separately).

## 三、面试可讲的 6 个技术点

| 点 | 一句话 | 追问时展开 |
|---|---|---|
| 行号映射 | diff hunk 的新文件行号只由 `+`/` ` 推进，`-` 不推进 | 举 `@@ -10,3 +12,5 @@` 的例子，说明漂移会让评论落到第 1 行 |
| 幂等 | 唯一约束是幂等的事实来源，不是应用状态 | webhook 重投 vs 新 commit 的区别：一个 run 变多，一个变多 run |
| 评论失败不算 run 失败 | 评审结果是产品，通知是可重放动作 | findings 先落库，`comment_status` 记录失败原因 |
| 队列只是优化 | DB 是事实来源，worker 有 DB 兜底扫描 | Redis 故障时延迟变差但不丢任务（实测捞回） |
| 两级门禁 | 确定性层必须满分，模型层允许波动但设地板 | 模型层用「录制输出」回归，CI 不需要 key |
| 误报闭环 | 误报是数据，不是噪音 | 聚类 → 候选 → 人工审批 → 灰度，绝不自动改规则 |

## 四、诚实边界（必须能说出来）

- 确定性规则引擎的 F1=1.000 是**在自建 20 条标注集上**，且用例与规则同源，不代表泛化能力。
- 真实模型那组 P/R/F1=0.769/0.800/0.784 才是模型能力的近似值；样本 20 条，只做方向性结论。
- 多缺陷用例模型常合并成一条评论（跨行区间），匹配规则按「一条真值只能被匹配一次」计，会表现为 FN——这是有意保守的口径。
- GitHub 真评论链路已在代码与契约测试中验证（payload、token、posted 标记、失败不丢结果），真实仓库评论需 PAT，属于未跑项。
