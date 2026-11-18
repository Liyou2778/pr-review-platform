# ADR-0002：幂等与去重是两个不同的问题

- 状态：已采纳
- 日期：2026-11-18

## 背景

上线第一天就会遇到两类重复：

1. GitHub 会重投同一个 webhook（超时、5xx 都会重试）；
2. 同一个 PR 推了新 commit，评审会再跑一次，同样的缺陷会被再评一遍。

把两者当成一个「去重」问题处理，会得到错误的系统：要么漏评新 commit，要么重复刷评论。

## 决策

拆成三层，各自有明确的事实来源：

| 层 | 键 | 事实来源 | 效果 |
|---|---|---|---|
| webhook 幂等 | `(provider, delivery_id)` | 数据库唯一约束 | 重投不新建 run |
| run 唯一 | `(repo_id, pr_number, head_sha)` | 数据库唯一约束 | 同一 commit 不重复评审；新 commit 是新 run |
| 评论去重 | `dedup_key = hash(repo, pr, path, line, rule)` | findings 表 + `posted` 标记 | 同一位置同一规则只发一次 |

关键区分：`new_findings`（是否以前出现过）和「是否需要投递」（是否 `posted`）是**两个指标**。
如果把去重建在「是否出现过」上，一次投递失败会让这条 finding 永远补发不出去。

## 后果

- 好处：失败可重放（`POST /api/runs/{id}/post`），且不会重复评论。
- 代价：多一列 `posted` 状态与一个补发接口；测试要覆盖「dry-run 后再补发」这条路径。
