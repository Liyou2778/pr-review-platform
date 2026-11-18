# 2 分钟演示脚本（面试现场版）

> 前提：`dev.cmd services` 已起 PG/Redis，`dev.cmd api` 与 `dev.cmd worker` 在跑。
> 全流程不依赖外网（规则引擎），需要真实模型时再开 `OCR_MODE=cli`。

## 0:00–0:20 一句话开场

「这不是又一个 AI 评审工具，而是把 AI 评审变成**能进 CI、能被度量、能持续变好**的服务。
三个数字：处理时延 P95 37ms、单 PR 真实模型成本 $0.019、20 用例 bench 有可回归的 F1。」

## 0:20–0:50 平台链路

1. 终端执行 `dev.cmd replay` → 打印 run_id。
2. 浏览器开 `http://127.0.0.1:8210/api/runs/3`：
   - 指出 `stats.engine`、`stats.findings`、`stats.new_findings`、`stats.suppressed_duplicates`。
3. 强调：**同一 PR 二次评审 new_findings=0**，评论不会刷屏。

## 0:50–1:20 看板

打开 `http://127.0.0.1:8210/dashboard`：

- 延迟卡片（P50/P95）、成本卡片、误报率卡片；
- findings 严重度分布；
- 右下「误报聚类」→ 这是规则候选的来源。

## 1:20–1:45 误报闭环

```powershell
# 标记一条误报
POST /api/findings/7/feedback {"verdict":"false_positive"}
# 聚类
GET /api/feedback/clusters
# 提候选 → 审批
POST /api/rules/candidates {"rule_key":"py-eval-usage"}
POST /api/rules/candidates/1/decision {"status":"approved"}
```

强调：**规则改动永远走人工门禁**，不会自动把误报「优化掉」从而漏掉真问题。

## 1:45–2:00 质量门禁

```powershell
dev.cmd bench      # 20 用例，F1 1.000（规则层）
dev.cmd bench-ocr  # 录制回放，F1 0.784（真实模型）
dev.cmd gate       # 不过阈值直接 exit 1
```

收尾：「所以我能回答面试官三个问题：**准不准**（bench）、**贵不贵**（成本看板）、
**敢不敢全量开**（门禁 + 人工审批 + 幂等 + 失败不丢结果）。」

## 备用：真实模型演示（约 20s，$0.02）

```powershell
$env:OCR_MODE='cli'; dev.cmd e2e
```

输出会展示 engine=ocr、tokens、cost、findings 的 severity/rule/行号。
