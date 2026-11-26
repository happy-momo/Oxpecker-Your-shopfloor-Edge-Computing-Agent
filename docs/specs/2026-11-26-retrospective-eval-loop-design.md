# Retrospective Evaluation Loop（回顾式评估环）设计文档

日期：2026-11-26
状态：已确认（方案 A：模板出题 + LLM 评审）
目标版本：0.7.0

---

## 1. 问题与目标

Oxpecker 的灵魂主张是"越用越准"——学习闭环每日把运行历史蒸馏进每资产的
`MEMORY.md` 知识库。但当前没有任何机制度量"学到的知识是否让后续回答变好了"。
知识资产的价值停留在信仰层面，无法用数据证明。

本设计引入**回顾式评估环（retrospective evaluation loop）**：

- 每日 cron 自动从历史快照中**分层随机抽取**诊断窗口
- 用"当前知识库"回答**当时**的诊断问题（答题模型不知道告警/反馈）
- 由 LLM 评审员对照**真值材料**（快照确定性结论 + 同期告警 + 后续反馈）判定
  回答 verdict
- 产出**准确率时间序列**，经 `/api/stats` 暴露——"越用越准"从主张变成可度量资产

### 评估目标（已确认）

度量**知识价值**：测试"当前 MEMORY.md 知识"能否让模型把快照解读得更准
（是否正常/是否已知先兆/机理），而非解读保真度（模型读 JSON 的能力）。

### 决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| 出题方式 | 方案 A：模板出题 + LLM 评审 | 出题/采样/真值全确定性，符合"事实层永不来自模型"原则；题目可复现；成本最低 |
| 评分方式 | LLM 评审员（structured verdict） | 能处理开放式诊断回答；解析失败诚实记 unknown |
| 触发方式 | 每日 cron 自动（eval_cron） | 数据连续性是"准确率曲线"故事的前提；默认关闭、显式开启 |
| 模型成本 | 全本地（routine role） | 答题+评审各一次调用 × 5 题 × N 资产，本地零成本 |
| 结果呈现 | /api/stats 聚合 + evals.jsonl 明细台账 | 可追溯是项目气质；评审理由是调试知识库的线索 |

---

## 2. 核心数据流

```
每日 eval_cron 触发（默认 0 4 * * *，learn 之后）
    │
    ▼
Sampler（确定性）
  从每资产快照归档分层随机抽 N 个窗口（默认 5）
  ≥2 incident（warn/crit）+ ≥2 nominal（info）+ 剩余随机
  每窗口生成 1 道诊断题（模板，确定性）
    │
    ▼
Answerer（LLM，routine role）
  复用 QAEngine context 构建：
    资产档案 + 抽中窗口的历史快照 + 当前 MEMORY.md
  （不注入：告警台账、反馈 —— 那是真值材料，只给评审员）
    │
    ▼
Reviewer（LLM，routine role）
  输入：题目 + 答案 + 真值材料包
  输出：{"verdict": "correct|partial|wrong|unknown", "reason": "..."}
  解析失败 → unknown（诚实失败，不计入准确率）
    │
    ▼
Ledger（落盘）
  workspace/assets/<id>/evals.jsonl 追加写（每行一次评估全记录）
  /api/stats 新增 "evaluation" 区块
```

**关键原则**：
- 出题、采样、真值打包全部确定性代码——只有答题和评审两个环节碰模型
- 答题不知道告警和反馈的存在，评审只看真值材料——信息隔离保证测的是
  知识而非转述
- unknown 不计入准确率分母——诚实失败，不虚增也不虚减
- 防串通：评审 prompt 不注入 MEMORY.md、不知道答题模型身份——评审员盲于
  被评系统

---

## 3. 模块结构

```
oxpecker/eval/
  __init__.py
  sampler.py      # 确定性出题：分层采样窗口 + 模板生成 EvalCase
  reviewer.py     # 评审员：真值材料包 + LLM structured verdict
  engine.py       # EvalEngine：编排一次评估 pass（sample→answer→review→落盘）
  prompts.py      # 答题/评审 prompt 模板（集中管理）

oxpecker/server/
  router_stats.py  # 修改：新增 "evaluation" 区块

oxpecker/core/scheduler.py  # 修改：run_due_jobs 增加 eval 分支
oxpecker/qa/engine.py       # 重构：提取 ask_with_snapshot 共用方法
oxpecker/cli.py             # 修改：新增 eval 子命令
```

### 数据结构

```python
# sampler.py
@dataclass
class EvalCase:
    asset_id: str
    window_end: str          # 抽中快照的 window_end（ISO）
    snapshot: dict           # 完整快照 dict（答案与真值都从这里读）
    question: str            # 模板生成的诊断问题
    question_kind: str       # "incident" | "nominal"

# reviewer.py
@dataclass
class EvalVerdict:
    verdict: str             # correct | partial | wrong | unknown
    reason: str

# evals.jsonl 一行
{
  "ts": 1764123456.0,
  "asset_id": "pump1",
  "window_end": "2026-11-25T14:32:00+00:00",
  "question_kind": "incident",
  "question": "…",
  "answer": "…（截断至 2000 字符）",
  "verdict": "correct",
  "reason": "回答正确识别了温度超 warn 带且趋势上行…",
  "answer_provider": "ollama-qwen",
  "review_provider": "ollama-qwen",
  "degraded": false
}
```

### 职责边界

| 模块 | 职责 | 依赖 | 不碰什么 |
|---|---|---|---|
| sampler.py | 读快照归档、分层抽样、模板出题（纯确定性） | AssetStore | 不碰 LLM |
| reviewer.py | 组装真值材料包、调评审 LLM、防御式解析 verdict | LLMGateway | 不做采样 |
| engine.py | 编排 + 台账落盘 + runlog | 全部 | 不实现出题/评审细节 |
| prompts.py | prompt 模板常量 | 无 | — |

---

## 4. 采样与出题细则

### 分层采样

```python
seed = sha256(f"{asset_id}:{eval_date}")[:8]   # 同日重跑可复现
candidates = [s for s in snapshots if s.get("sample_count", 0) > 0]  # 排除空窗
incident = [s for s in candidates if overall_level in ("warning", "critical")]
nominal  = [s for s in candidates if overall_level == "info"]
# 分层：incident ≥2 + nominal ≥2（可满足时），剩余随机补足到 N
```

边界情况：
- 历史不足（candidates < 3）：跳过该资产，runlog `eval`/`no_data`——不硬凑题目
- 全是 nominal（新设备）：incident 槽位改用 nominal 题模板（"是否正常/有无需
  关注处"本身就是有效考点）
- retention 已裁掉的快照天然不会被抽到（只在现存文件里抽）

### 题目模板（按考点分流，确定性选择）

| 条件 | 模板 | 考点 |
|---|---|---|
| overall_level >= warning | "根据当前数据与已知设备知识，该设备在 `<窗口时间>` 前后状态如何？哪些指标异常、可能的机理是什么？" | 先兆识别价值 |
| overall_level == info | "根据当前数据与已知设备知识，该设备此时是否正常运行？有无值得关注的早期信号？" | "确认正常"价值（防评估偏向故障题） |

---

## 5. 真值材料与评审

### 真值材料包（reviewer 输入，全部确定性提取）

```python
{
  "snapshot_verdict": {
    "overall_level": snap["overall_level"],
    "metrics_in_warn": [...],   # 超 warn 带的指标名+值
    "metrics_in_crit": [...],
    "trends": [...],            # 非零趋势指标+方向
  },
  "alerts_in_window": [...],    # alerts.jsonl 中 ts ∈ [window_start-15min, window_end+15min]
  "feedback_after": [...],      # feedback.jsonl 中 ts ∈ [window_end, window_end+24h]
}
```

### 评审 prompt 要点

- 评审员不知道答题模型是谁、看不到 MEMORY.md——只对照真值材料
- 输出严格单 JSON：`{"verdict": "...", "reason": "一句话"}`
- 解析复用 gateway 的防御式 JSON 提取（fence 剥离 + 平衡花括号扫描）
- 失败 → unknown

### 评分聚合

- correct=1.0, partial=0.5, wrong=0.0, unknown=剔除分母
- `accuracy = Σ分值 / (总题数 - unknown数)`；分母 0 → `null`（诚实缺省）
- eval_summary 滚动保留最近 30 天（与 stats 曲线窗口对齐）

---

## 6. 错误处理与降级

| 故障 | 行为 | runlog |
|---|---|---|
| 无历史快照/不足 | 跳过资产 | `eval`/`no_data` |
| 答题 LLM 全 provider 失败 | 整 pass 失败，summary 不更新 | `eval`/`provider_failed` |
| 答题超时（单题） | 跳过该题，verdict=unknown | 该题记录落盘 |
| 评审失败/解析失败 | verdict=unknown，题目与答案仍落盘 | — |
| 整 pass 异常 | 不杀 supervisor 循环（与 deep_report/learn 同构） | `eval`/`exception` |

降级路径与现有 gateway 一致：routine role → 本地优先，失败转在线，
`degraded: true` 落盘。

---

## 7. 配置与集成

`AdviserConfig` 新增（全部带默认值，旧配置零迁移）：

```python
eval_cron: str = ""                # 空 = 不启用（默认关闭，显式开启）
eval_cases_per_asset: int = 5
eval_reviewer_role: str = "routine"   # 预留：未来可切 online
```

- Supervisor.run_due_jobs：eval 分支与 deep_report/learn 完全同构
  （cron 匹配 + slot 去重 + 异常吞掉 + runlog `kind="eval"`）
- CLI：`oxpecker eval config.json [asset]` 手动跑一次（调试/演示）
- loader.py：显式加载三个新字段

### /api/stats "evaluation" 区块

```json
"evaluation": {
  "enabled": true,
  "last_run_ts": 1764123456.0,
  "last_run_accuracy": 0.8,
  "accuracy_7d": [{"date": "2026-11-20", "n": 5, "correct": 4}],
  "per_asset": {"pump1": {"accuracy": 0.8, "n": 25}},
  "verdict_counts": {"correct": 20, "partial": 3, "wrong": 1, "unknown": 1}
}
```

未启用时区块返回 `{"enabled": false}`；启用但无数据时各指标 `null`。

---

## 8. 测试计划

| 测试文件 | 覆盖 |
|---|---|
| test_eval_sampler.py | 分层比例、种子复现性、候选不足跳过、空窗排除、模板选择 |
| test_eval_reviewer.py | verdict JSON 解析（fence/前言）、真值材料时间窗过滤、评审失败→unknown |
| test_eval_engine.py | 端到端 mock gateway：采样→答题→评审→台账→聚合；pass 级失败；单题超时 |
| test_eval_stats.py | 聚合正确性、unknown 剔除、空数据 null、未启用形态 |
| test_eval_supervisor.py | cron 触发、slot 去重、异常不杀循环、runlog 记录 |

预计新增 ~20 测试。mock 策略与 test_server.py/test_e2e_flow.py 一致（MockGateway）。
现有 221 测试不得破坏。

---

## 9. 实施顺序（TDD）

1. sampler.py + 测试（纯确定性，无依赖，先行）
2. reviewer.py + prompts.py + 测试
3. 提取 QAEngine.ask_with_snapshot（重构，现有 QA 测试守护行为不变）
4. engine.py 编排 + 台账 + 测试
5. Supervisor cron 集成 + CLI 命令 + 测试
6. /api/stats 区块 + 测试
7. 全量回归 + CHANGELOG

## 10. 未来演进（本设计不做，但格式预留）

- 人工标注集导入：evals.jsonl 行格式可扩展 `source: "human"` 字段
- 保真度题型：纯确定性数字比对题作为第二维度
- 知识库版本对照：同一批题目在不同 git 版本的 MEMORY.md 上重跑，直接量化
  "这次学习让准确率从 X 到 Y"
