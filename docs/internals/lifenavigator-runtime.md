# Life Navigator Runtime（实现说明）

> 本文是 `Life_Navigator_Runtime_升级方案.md` 的落地记录。它描述的是**实际源码**，
> 不是对模块的假设。v1.5 的 Life Model（`life_model/`、Dream pass B、`life_review`）
> 已经存在于本仓库，本次**没有重写**它，而是在其上增加了持续运行的认知 Runtime。

---

## 0. Phase 0：源码盘点与调用链

### 0.1 实际入口

| 职责 | 实际位置 | 状态 |
|---|---|---|
| Agent 主循环 | `jenny/agent/loop.py`、`runner.py` | 已有 |
| 每轮 turn 的持久化 | `jenny/agent/turn_persistence.py`、`session/manager.py` | 已有 |
| Context 构建 | `jenny/agent/context.py` | 已有 |
| Life Model 存储 | `jenny/life_model/store.py::LifeModelStore` | 已有（v1.5） |
| Life Model 注入 | `agent/context.py::build_system_prompt` + `templates/agent/life_model_section.md` | 已有 |
| Dream pass A（memory） | `jenny/agent/memory.py`、`runtime/cron_dispatch.py::_dream_turn` | 已有 |
| Dream pass B（Life Model） | `jenny/agent/life_model_cycle.py` | 已有 |
| `life_review`（周度、可说话） | `jenny/agent/life_review.py` + cron `life_review` | 已有 |
| 事件入口 | `jenny/agent/tools/record_event.py` | 已有 |
| Cron 调度 | `jenny/runtime/cron_dispatch.py`、`jenny/runtime/container.py` | 已有 |
| Android 服务/网关 | `jenny/android_entry.py` → `runtime/container.py::GatewayContainer` | 已有 |
| 工具注册 | `jenny/agent/tools/loader.py` | 已有 |
| Memory | `jenny/agent/memory.py::MemoryStore` | 已有 |

### 0.2 实际调用链（本次改造后）

```text
用户消息
  → AgentLoop (_process_message / runner)
  → AgentHook: NavigatorPerceptionHook.after_run
  → NavigatorTurnObserver.observe_turn
  → PerceptionManager.record_turn
      → navigator/observations.jsonl
      → 更正/退让时：navigator/events.jsonl + 自动失效当前假设 + Self-Model 记录
  （本轮不调用 LLM）

每小时 cron: job "navigator"
  → CronDispatcher._run_navigator
  → NavigatorRuntime.run_cycle
      → PerceptionManager.capture_time / drain_inbox
      → StateAggregator.build
          → navigator/snapshots.jsonl（7 维、证据 ID、无总分）
      → OutcomeTracker.evaluate_pending
      → TriggerEngine.evaluate
          ├─ no_trigger / low_priority → 结束（不调用 LLM）
          └─ normal / important / urgent
               → NavigatorCore.analyze（空工具集、silent、结构化 JSON）
               → PermissionLayer.authorize
                    ├─ analysis_only（不打扰）
                    └─ 允许说话
                        → ExpressionLayer.compose（空工具集、silent）
                        → ChannelDeliverer（record + proactive）
                        → OutcomeTracker.record_intervention（pending）
      → 下一次 cycle 评估结果 → Self-Model → 修正下一次判断
```

### 0.3 与升级方案逐项对照

| 方案要求 | 状态 | 实际实现 |
|---|---|---|
| Perception Layer | 部分已有 → 已补 | 对话/时间/外部 JSON inbox；Android 采集器接口就绪（见 §6） |
| State Aggregator | 缺失 → 已补 | `navigator/aggregator.py`，7 维事实 + 证据 |
| Trigger Engine | 缺失 → 已补 | `navigator/trigger.py`，5 级调度优先级 + 节流 |
| Navigator Core | 部分已有（Dream/life_review）→ 已补独立层 | `navigator/core.py`，结构化输入/输出 |
| Decision / Permission | 缺失 → 已补 | `navigator/permissions.py`，act 永不自动执行 |
| Outcome Tracker | 缺失 → 已补 | `navigator/outcome.py` |
| Navigator Self-Model | 缺失 → 已补 | `navigator/self_model.jsonl`，只记录 Navigator 自己 |
| 规则版本 | 缺失 → 已补 | `navigator/rules.py` + `rules.jsonl` |
| 统一数据模型 | 缺失 → 已补 | `Observation / Event / StateSnapshot / Hypothesis / Outcome` |
| Life Model 隔离 | 已有 | 未改动 `LifeModelStore`、deny-list、Dream 边界 |
| 不重复造 Jenny | 遵守 | Navigator 无工具、无独立聊天、无第二套 scheduler |

---

## 1. 设计原则

1. **本地代码感知和整理现实，LLM 理解现实。**
   - 观察、清洗、去重、聚合、触发、节流、权限、结果记录全部在 Python。
   - LLM 只看到聚合后的**事实 + 证据 ID**，看不到原始传感器 payload。
2. **事实 ≠ 推断。**
   - `StateSnapshot` 是事实；`Hypothesis` 是推断；推断永远不会自动升级为事实。
   - `current_state.md` 是 Dream 维护的模型状态，Navigator 把它当“模型维护的状态”，
     不当作事实引用。
3. **持续感知，不是持续思考。**
   - 每个用户 turn 只写一条本地观察。
   - LLM 分析由 trigger 决定，默认一天最多 3 次，且通常一次都不会发生。
4. **判断与执行分离。**
   - Navigator Core 的工具集是空的，物理上无法执行动作。
   - `act` 永远进入 `requires_confirmation`，不自动执行。
5. **拒绝不是用户的问题。**
   - 同类干预连续被拒绝/无效 → 写 Navigator 自己的 `overreminding` 笔记，下一次降低权重。

---

## 2. 数据分层与文件布局

```text
navigator/
├── observations.jsonl    # 原始观察（有界、脱敏、非 LLM 输入）
├── events.jsonl          # 有意义的事实性事件（纠正、退让、环境变化等）
├── snapshots.jsonl       # 7 维状态快照
├── hypotheses.jsonl      # 假设（append-only 状态更新，按 id 去重）
├── outcomes.jsonl        # 干预 → 反应 → 效果
├── self_model.jsonl      # Navigator 对自己的认识（不是用户模型）
├── decisions.jsonl       # Core 的结构化输出（审计用）
├── trigger_log.jsonl     # 每次 trigger 评估（有界）
├── rules.jsonl           # 规则版本（受控变更）
├── trigger_state.json    # 冷却/每日上限状态
├── environment.md        # 用户维护的 Environment Model（只读）
└── inbox/                # Android/外部采集器投放 JSON 的入口
    ├── processed/
    └── failed/
```

`navigator/` 与 `life_model/`、`memory/` 完全分离。 Navigator 不写
`life_model/`；Life Model 仍是长期认知层，由 Dream 维护。 主 Agent 的普通文件工具
也照旧不能写 `life_model/`（v1.5 deny-list 未改动）。

### 2.1 核心类型

- `Observation`: `id, timestamp, source, kind, value, metadata`
- `NavigatorEvent`: `id, timestamp, type, summary, source_ids, metadata`
- `StateSnapshot`: `timestamp, window_start, window_end, dimensions, metrics, evidence_ids`
- `Dimension`: `key, facts, evidence_ids, change`（`unknown/stable/changed`，不是评分）
- `Hypothesis`: `id, statement, evidence_ids, confidence, status, ...`
- `TriggerDecision`: `priority, reasons, evidence_ids, allow_speak, throttled, next_eligible_at`
- `NavigatorDecision`: `observed_facts, hypotheses, confidence, state_assessment, conflicts,
  recommended_action, need_user_discussion, reasoning_evidence_ids`
- `OutcomeRecord`: `intervention_id, delivered_text, reaction, effectiveness, ...`
- `SelfModelNote`: 只描述 Navigator 的判断/表达/干预问题
- `RuleVersion`: `old_rule, new_rule, reason, evidence, discussion_result, effective_time`

### 2.2 七个维度

`body_state`、`emotional_state`、`behavioral_state`、`cognitive_state`、
`environment_state`、`development_state`、`navigator_effectiveness`。

没有 `health_score`，也没有任何综合分数字段。 `metrics` 只保存触发用的确定性数字
（消息数、短回复率、睡眠均值、被拒次数……），不注入用户可见文本。

---

## 3. Trigger Engine

输出（**调度优先级，不是用户严重程度**）：

```text
no_trigger < low_priority < normal < important < urgent
```

- `low_priority`：记录，不调用 LLM。
- `normal` 及以上：调用 Navigator Core。
- `allow_speak` 是独立答案：可以为“值得分析”但“不应该打扰”。

触发信号：

- 多维同时变化（`>=2` 个维度 `changed`）
- 多日持续偏移（`drift_min_days` 个不同日期出现维度变化）
- 高价值事件（goal/milestone/setback/environment_change/intervention_rejected）
- 上一次干预被拒绝/无效 → 重新考虑方式，而不是重复催促
- 用户明确要求关注的话题（`watch_topics`）被提到 → important；若同时有高价值事件 → urgent
- 用户主动对话：只提供 `low_priority` / 不打扰的上下文，不因一句话就额外打扰
- 用户更正：本地立即处理（失效假设 + Self-Model），不靠 LLM
- 用户说“我没事/只是累”：24h 内降级并保持安静，除非有新的高价值证据

节流：

- 两次分析之间至少 `min_minutes_between_analyses`（默认 180 分钟）
- 每天最多 `max_analyses_per_day`（默认 3 次）
- 安静时段 `quiet_start_hour`–`quiet_end_hour`（默认 23:00–07:00）
- `urgent` 可越过冷却/上限；安静时段仍不主动发消息（可以在后台分析）

---

## 4. Navigator Core 与 Expression

### 4.1 输入

- 当前 7 维状态（事实 + 证据 ID）
- 最近事件
- 最近 Navigator 干预及结果
- 活跃假设（明确标注“不是事实”）
- Navigator Self-Model
- Life Model 四个可注入文件
- Environment Model（`navigator/environment.md`）
- 最近对话（默认个人会话最后 12 条）
- 允许引用的证据 ID 全集

不加载全部历史，不加载原始传感器数据。

### 4.2 输出

严格 JSON：

```json
{
  "observed_facts": ["..."],
  "hypotheses": [{"statement": "...", "confidence": "medium", "evidence_ids": ["..."]}],
  "confidence": "medium",
  "state_assessment": "...",
  "conflicts": ["..."],
  "recommended_action": {
    "kind": "observe | discuss | suggest | act",
    "description": "...",
    "risk": "none | low | medium | high",
    "reversible": true
  },
  "need_user_discussion": true,
  "reasoning_evidence_ids": ["..."]
}
```

解析规则：

- 容忍 markdown fence / 前后 prose / 轻微 JSON 损坏（`json_repair`）
- 未知 evidence id 会被丢弃并记录在 `invalid_evidence_ids`
- 没有可追溯证据的 hypothesis 会被丢弃
- 解析失败 → `parse_ok=False`，保持沉默
- `confidence=low` 时权限层不会允许主动消息（Case B）

### 4.3 表达

Expression 层是第二个 silent、空工具 LLM turn。它只负责措辞：
用户语言、1–3 句、不诊断、不提“数据/模型/置信度”、不重复已经无效的干预方式。
权限层在它之前已经决定“能不能说”。

---

## 5. 权限、结果与自我修正

### 5.1 权限分层

```text
observe / discuss            → 允许（讨论永远允许）
low-risk reversible suggest  → 允许
medium risk / 计划式建议      → 降级为 discuss
act / high risk / irreversible
                             → requires_confirmation，不执行
```

高风险关键词（财务、辞职、退学、关系、删除、对外发送、系统设置、医疗等）是
**安全网**；第一判据仍是 Core 的 `kind/risk/reversible`。
Phase 1 没有动作执行器：Core 没有工具，所以“绕过权限层执行”在物理上不存在。

### 5.2 结果追踪

每次主动消息写入 `OutcomeRecord(pending)`，窗口默认 48h。窗口结束后：

- 无回复 → `no_response`，效果 `unknown`
- 短回复/“知道了” → `acknowledged`，效果 `unknown`（参与 ≠ 改善）
- 明确推回（“别提醒了”“烦”）→ `rejected`，效果 `ineffective`
- 用户更正 → `corrected`，效果 `side_effect`
- 用户展开讨论 → `engaged`，效果仍 `unknown`（接触不等于有效）

同类干预 30 天内 2 次被拒/无效 → `SelfModelNote(kind="overreminding")`：
“降低这类建议的权重、换方式”，而不是“用户不听话”。

### 5.3 用户更正（Case F）

用户消息命中明确的更正模式（“你搞错了”“我没说过”“你误解了”等）时：

1. 立即写入事实性事件 `user_correction`
2. **立即失效**当前所有 observing/supported 假设
3. 写 `SelfModelNote(kind="misjudgement")`
4. 把最近 pending 干预标为 `corrected / side_effect`
5. 不把任何“用户讨厌……”写进用户模型

整个过程不调用 LLM，也不依赖 prompt 自律。

### 5.4 规则版本

`navigator/rules.py::RuleBook` 记录 `old_rule/new_rule/reason/evidence/discussion_result/
effective_time`。运行时**不会自动改规则**：没有工具、没有后台自动晋升路径。
未来若要做自我进化，调用方必须先与用户讨论，再显式 `record_change()`。

---

## 6. Android / 外部感知接入

### 6.1 已实现（P0 + 基础系统状态）

- 对话（每 turn，本地指标）
- 时间（每日上下文）
- **通知**：`JennyNotificationListener.kt` 在用户授予“通知使用权”后收集通知，
  写入 app-private 有界队列；Navigator 每小时 drain。写入 `navigator/` 的 observation
  只保留 package/app/category/ongoing，**不保留标题和正文**；敏感应用进一步只留包名/应用名。
- **App 使用**：`PerceptionBridge.kt` 用 `UsageEvents` 计算本 cycle 的前台分钟数，
  不是累计日桶，所以不会每小时重复上报同一数字。需要“使用情况访问”特殊权限。
- **系统状态**：电量、充电、亮屏、省电模式、网络类型、uptime，每 cycle 一条事实。
- **粗粒度位置**：复用现有 `LocationBridge`/`location.py` 的 last-known + 反向地理编码；
  写入 Navigator 前由隐私层把经纬度四舍五入到约 1km。
- 外部 JSON inbox（`navigator/inbox/`），供未来 Health Connect / 第三方采集器使用。

权限入口（Android 系统设置，不是运行时弹窗）：

- 通知：设置 → 通知 → 设备与应用通知 → 允许 Jenny
- 使用情况：设置 → 应用 → 特殊应用权限 → 使用情况访问 → 允许 Jenny

未授予时对应采集静默跳过；其余功能不受影响。`/navigator` 会显示
`Android access: notifications=on/off, usage=on/off`，可以直接确认状态。

### 6.2 尚未实现：健康数据

睡眠 / 步数 / 心率 / 运动需要 Health Connect（或设备专用传感器路径）和运行时
授权流程；本版本没有接入。采集器就绪后只需把 JSON/JSONL 丢进
`navigator/inbox/`，例如：

```json
{"source":"android","kind":"notification","timestamp":"2026-10-26T22:00:00+08:00",
 "value":{"package":"com.example.app","app":"Example","title":"...","text":"..."}}
```

```json
{"source":"health","kind":"sleep","value":{"sleep_minutes":390}}
```

### 6.3 隐私过滤

隐私过滤在 `PerceptionManager._privacy_filter` 单点执行：

- `capture_notifications` / `capture_app_usage` / `capture_health` / `capture_location` /
  `capture_screen` / `capture_system` 开关
- 敏感应用默认（微信、QQ、支付宝、银行等）只保留包名/应用名/计数，不保留标题与正文
- `blocked_packages` 里的应用在写入存储前就被丢弃，等同于按应用关闭
- 健康数据只保留白名单字段（`sleep_minutes/steps/resting_hr/...`）
- 位置只保留两位小数（≈1km）
- 屏幕内容默认关闭，不持续存储；`capture_system` 只保留亮屏/电量等状态，不读屏幕内容
- 所有外部 payload 深度/长度/列表长度有界

通知的原始内容只存在于 app-private 的有界队列里，最多约 1MB/300 条，Python
drain 后即删除。监听服务默认**不采集**；第一个启用的 Navigator cycle 会写入
`capture_notifications=true` 才开通。关闭通知采集后，下一个 cycle 会停止监听并清空
队列；要彻底停止则撤销系统“通知使用权”。

---

## 7. 配置

`workspace/config.json` → `agents.defaults.navigator`（snake_case 与 camelCase
都接受；应用自己写回时用 camelCase）：

```json
{
  "enabled": true,
  "interval_min": 60,
  "min_minutes_between_analyses": 180,
  "max_analyses_per_day": 3,
  "quiet_start_hour": 23,
  "quiet_end_hour": 7,
  "observation_window_days": 7,
  "outcome_window_hours": 48,
  "drift_min_days": 2,
  "dismissal_cooldown_hours": 24,
  "watch_topics": ["专升本", "找工作"],
  "capture_conversation": true,
  "capture_conversation_text": true,
  "capture_notifications": true,
  "capture_app_usage": true,
  "capture_health": false,
  "capture_location": true,
  "capture_screen": false,
  "capture_system": true,
  "redact_sensitive_content": true,
  "sensitive_packages": ["com.tencent.mm", "..."],
  "blocked_packages": [],
  "delivery_channel": "websocket",
  "delivery_chat_id": "default"
}
```

- `enabled=true` 只表示“运行本地循环”；大多数 cycle 不会调用 LLM。
- `watch_topics` 是用户显式要求关注的话题；只有它 + 高价值事件才会 `urgent`。
- `capture_health=false` 是默认值：健康数据最小必要采集，不因“能采”就全采。
- 关闭 `enabled=false` 后 cron job 不再被注册；已注册的 job 在 dispatch 时也会退出。

### 7.1 查看状态

在聊天里输入 `/navigator`：只读，不调用 LLM、不触发分析，显示最近一次 snapshot 的
七个维度、待评估干预和 Navigator Self-Model。适合确认 Runtime 是否在正常工作，
或看看它最近把什么当成了“事实”。

`/navigator notifications` / `/navigator usage` 会打开 Android 对应的系统授权页，
方便授予通知使用权 / 使用情况访问；授权后下一个 cycle 自动生效。

---

这两者职责不同：Navigator 是持续 Runtime（感知、触发、结构化判断、结果追踪），
`life_review` 是 v1.5 留下的周度反思任务，默认关闭。若同时开启，两者可能各自
产生一次主动消息；建议只保留 Navigator，或把 `life_review.interval_h` 拉长，
避免同一周里出现两套“主动关心”。

## 8. 验收案例映射

| 案例 | 机制 |
|---|---|
| A 普通学习日 | 无 changed 维度/无高价值事件 → `no_trigger`，不调用 LLM |
| B 短期疲劳 | 单一身体维度变化；`confidence=low` 被权限层拒绝主动消息 |
| C 压力持续积累 | 多日 drift + 多维同时变化 → `important`；事实→判断→讨论 |
| D 提醒产生反效果 | 上次 outcome rejected/ineffective → `important`；同类重复 → self-model 降权 |
| E 目标成立但路线失败 | Core prompt 要求区分执行/资源/环境/目标/路线；高价值事件触发讨论 |
| F 用户明确纠正 | 本地失效假设 + `misjudgement` 笔记；不进入用户事实 |
| G “我没事，只是累” | `user_dismissal` → 24h 降级；仅新证据重开 |
| H 状态稳定 | 无探索触发器；不会因“有能力探索”而频繁推荐新方向 |

---

## 9. 测试

新增测试（`tests/navigator/`）：

- `test_models_store.py` — JSONL 往返、append-only 状态更新、规则版本
- `test_perception.py` — turn 指标、去重、更正/退让事件、隐私过滤、inbox
- `test_android_perception.py` — Kotlin JSON → observation、敏感应用脱敏、权限状态
- `test_aggregator.py` — 7 维、事实时间范围、无分数、证据
- `test_trigger.py` — A–H 关键分支、冷却/上限/安静时段/urgent
- `test_permissions.py` — 低风险允许、act 需确认、高风险关键词
- `test_core.py` — JSON 解析、fence、未知证据过滤、垃圾输入沉默
- `test_expression.py` — 去 fence/引号、模型自述过滤、长度上限
- `test_outcome.py` — 无回复/敷衍/推回/更正/重复拒绝笔记
- `test_runtime.py` — 无触发器不调用 LLM、权限先于说话、投递失败不记录结果
- `test_full_cycle.py` — 真实 Core + Expression + 假 provider 的完整一轮
- `test_hook.py` — 只观察用户 turn、忽略内部会话/合成行

同时更新：

- `tests/runtime/test_container_registers_configured_jobs.py`（新增 navigator job）
- `tests/command/test_builtin_navigator.py`（`/navigator` 只读状态）
- `tests/utils/test_asset_manifests.py`（新模板已在 manifest）

运行：

```bash
python -m pytest tests/navigator -q
python -m pytest tests/runtime/test_container_registers_configured_jobs.py -q
python -m pytest -q
```

---

## 10. 本阶段明确未做

1. Health Connect 采集器（睡眠 / 步数 / 心率 / 运动）；通知与 App 使用已接入，健康数据仍走 `navigator/inbox/`。
2. WebUI 设置面板中的 Navigator 开关（当前用 `config.json` 或工作区文件编辑）。
3. 自动修改核心规则（这是有意的：规则变更必须先与用户讨论并记录版本）。
4. 真正的动作执行器（Phase 1 的权限层只允许“说”，`act` 永远需确认）。
5. 探索能力的机会扫描（稳定期不推荐新方向；后续可作为独立低权重模块）。
6. 把 Navigator hypotheses/self-model 注入主 Agent prompt（保持长期模型边界；
   Life Model 仍是唯一注入主 prompt 的长期认知层）。
