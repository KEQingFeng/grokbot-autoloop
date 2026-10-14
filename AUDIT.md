# 可靠性审计报告 — 2026-10-14（基于真实代码，非文档宣称）

审计对象：`scripts/autoloop.py` v1.1.0（3574 行，27 子命令）+ `tests/selftest.py`（36 场景 / 430 断言）。
方法：`grep`/`sed` 直接读源码与测试，逐条对照《可靠性增强指令》§一–§十三。行号是审计当时的行号。

## 一、已实现且可复用（禁止重造）

| 能力 | 证据 | 结论 |
|---|---|---|
| 原子持久化 | `write_json` L191-199：写 `.tmp` → `flush` → `os.fsync` → `os.replace` | §三「事务/原子写入/防损坏」已满足，新增状态直接复用 |
| 追加式日志 | `append_jsonl` L202-206；`Ctx.ledger` L438-441 写 `ledger.jsonl`；`Ctx.journal` L443+ 写 `log/<day>.md` | §六 的「追加式事件日志 + 不覆盖旧日志」底座已在，但字段不够（见缺口 3） |
| 风险评分 | `risk_score(ctx, rec, rows, churn)` L2561-2586：base 10 + churn/20（≤30）+ 顶层模块数>3 加 15 + diff 无测试文件加 15 + 每轮修复加 10 + 文件名命中 `HIGH_RISK_NAME_RE`（auth/pay/crypto/…）加 25 − 机械类改动 10，上限 100，并返回理由列表 | §八「基于实际 diff 判断风险」的判据已存在，PR 风险分级复用它，不另写一套 |
| 真实 PR 读取 | `pr_list(ctx)` L662-684：`gh pr list --state all --limit 100 --json number,headRefName,state,title,url,baseRefName`，按 `auto/` 前缀过滤，**每 Ctx 缓存 20 s** | §九「相同状态不重复付费调用」已落地；但字段里没有 CI 状态（缺口 2） |
| 红线与闸门 | `FORBIDDEN_GLOBS` L66-77、`SECRET_LINE_RE` L113、`redline_check` L1202+、`verify_slot` L2190+、闸门脚本永远从**控制副本**读 L1171 注释 | §三/§八 的「不得仅凭模型自述」已有机械实现 |
| PR 真值核对 | `sync_prs` L1974+：merged/closed → `done.json`；`adopt_worker_pr` L690+ 按 head branch 复核 worker 自述的 PR | §六「不得把 PR 已创建误报为已合并」的一半已在 |
| 全局熔断 | `fail_gate`=3（L50），连续红 → maintenance（L1905-1908、L2982、L3010） | 有一个粗粒度熔断，但不是「同类错误签名」级别（缺口 4） |
| 粗粒度空转闸 | `finish_grace_s`/`stall_s`/`job_timeout_s`/`idle_ticks`（L1928、config.example.json caps 注释） | v1.1.0 的四道闸，覆盖「worker 侧」空转，不覆盖「控制器侧」（缺口 1） |
| 预算计数 | `cmd_budget` L2939-2957：turns/jobs/PR/连续红/gh 命中/磁盘 对 cap | 是**全局日配额**，不是 per-task 预算（缺口 5） |

## 二、真实缺口（按风险等级）

1. **看门狗与有效进展判定完全不存在（高）** — `grep -n "heartbeat\|watchdog\|last_progress\|no_progress"` 在 `autoloop.py` 里**零命中**。现有判据只有「日志静默 + 无提交」（`stall_s`）、「进程超时」（`job_timeout_s`）、「无事可做 6 轮」（`idle_ticks`）。§四 要求的心跳/心跳异常/无有效进展三级阈值、§五 要求的「模型仍在输出、日志仍在增长、进程仍存活、token 仍在烧都不算进展」都没有实现；控制器自身卡死没有任何检测（`watch` 是前台循环，它死了就没人知道）。
2. **没有任何合并代码路径（高，且是本次新增需求）** — `grep -n "pr merge"` 只命中 L862、L910、L2630 三处**提示词字符串**（"Never `gh pr merge`"、"Never merged automatically."）。`pr_list` 不取 `statusCheckRollup`，所以连「CI 是否通过」都无从判断。§八 的批量审查 + 串行合并 + 合并后回归全部待建。注意：这与 `PROTOCOL.md` §八「不得自动合并 PR」直接冲突，本次按用户 2026-10-14 新指令实现为**显式开关、默认关闭**，并在协议文档里写明这层关系。
3. **事件记录不足以重建时间线（高）** — `ledger.jsonl` 有 `ts` 但无 `event_id`/`phase`/`operation_id`/统一 `task_id` 字段；没有任何命令能回答 §六 那七个问题（何时启动/停在哪步/最后成功操作/最近失败原因/哪些调用耗资源/哪些代码已改已合/从哪恢复）。
4. **任务状态机不是八态（中高）** — 槽位状态是 ad-hoc 字符串：`ACTIVE_STATES = ("developing","verifying","repairing")`、`DONE_STATES = ("pr_open","pr_ready","blocked","idle",None)`（L121-122）。缺 `QUEUED`/`RECOVERING`/`UNKNOWN` 语义，缺「最后有效进展时间」「尝试/失败次数与重试原因」「检查点」「待执行动作」「依赖任务」「完成依据」等 §三 要求的字段；`recover` L3016 会从 git+gh 真值重建槽位，但不会把状态不明者显式标为 `UNKNOWN` 并禁止其自动判成功。
5. **无 per-task 预算、无退避、无同类错误熔断（中）** — 计数器全是全局日配额；没有指数退避；`fail_gate` 只数「连续红」，不识别「同一个错误签名重复 3 次」。§五「禁止通过单纯增加重试次数解决死循环」缺机械保证。
6. **告警不是结构化的（中）** — 只有 `report --patrol` 打印的【异常告警｜…】文本（L3150）。没有 `alerts.jsonl`、没有可配置通知命令、更没有「实际发送结果」记录，违反 §十「不得假定某个外部通知已经成功发送」。
7. **CI 状态未纳入判定（中）** — 见缺口 2；`gh pr checks` / `statusCheckRollup` 在引擎里零引用。

## 三、实施顺序与理由

| 阶段 | 内容 | 为什么排这里 |
|---|---|---|
| S1 | 事件底座：`ctx.event(phase, kind, **fields)` 追加 `events.jsonl`（event_id/task_id/phase/op_id/ts），凭证过滤复用 `SECRET_LINE_RE` | 后面每一阶段的结论都要落进它；先建底座才不会返工 |
| S2 | `review`：PR 待审队列 + 阈值 3/单批 5/最早超 2h 提前 + 脚本级风险分级（复用 `risk_score`）+ 依赖与同文件冲突排序 + 每 PR 独立结论 | 用户本次的头号需求，且**零模型调用**即可完整实现，符合 §九 |
| S3 | `merge`：串行、逐个、合并前重读真值 + CI + 冲突 + 闸门绿，合并后回归并重验，回归即停并保存现场；默认关闭 | 依赖 S2 的风险结论；风险最高，所以放在有完整证据链之后 |
| S4 | 八态任务机 + 字段补齐 + `timeline` 重建 | S2/S3 产生的记录需要正式状态语义承载 |
| S5 | 独立看门狗：心跳 30s/异常 90s/无有效进展 15min + 有效进展事件判据 + 同类错误签名熔断 3 次 | 需要 S1 的事件流和 S4 的状态机做输入 |
| S6 | per-task 预算 + 指数退避 + 结构化告警（含实际发送结果） | 在熔断与状态机之上收口资源与外呼 |
| S7 | 14 项验收测试 + 全量自测 + 文档同步 | §十一 要求实测，不得只交设计 |

## 四、审计当时无法证明的事

- 这台机器上没有 `gh`（`preflight` 实测 `[!!] gh CLI gh 不存在`），所以 S2/S3 的 CI 判定只能用 `tests/fake_gh.py` 替身验证；真实 GitHub 上的分支保护、required checks、`gh pr merge` 的权限行为必须在云电脑上亲手验一次。
- `git config user.name/user.email` 在本机任何层级都未设置，提交作者是主机名兜底身份（`神川奈木 <aaron@shenchuannaimudeMacBook-Air.local>`）。`preflight` 正确报了这一项；改身份是用户的决定，本次不代跑。
- 负载会显著扭曲计时类断言：同一份自测在 load 213 时跑 1063 s，在 load 4 时跑 99.4 s。秒数不是回归信号，断言数才是。
