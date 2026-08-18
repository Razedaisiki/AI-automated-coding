# .agent/FINAL_REPORT.md — 交付报告

## 任务

按 `AI_automated_coding.md` 实施 Supervisor（确定性进程管理器）M0–M5。

## 交付内容

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 协议冻结：`docs/supervisor-protocol.md`、`supervisor.toml`、`.agent/state.schema.example.json` | 完成 |
| M1 | `supervisor/{config,models,storage,events,lock}.py` —— 原子 JSON 写、JSONL 事件日志、fcntl 独占锁、状态校验 | 完成 |
| M2 | `dsh_runner.py`（exec 数组、独立进程组、SIGTERM→grace→SIGKILL、/proc 进程身份）、`prompts.py`、`parent-once` | 完成 |
| M3 | `engine.py` 本地主循环：INITIAL_START / RUNNING / COMPLETED / BLOCKED 分发，exit0/exit!=0 重启策略 | 完成 |
| M4 | 限额（activations/crash/clean/timeouts/active wall time）→ STOPPED_LIMIT、退避、事件审计 | 完成 |
| M5 | 崩溃恢复：runtime 恢复、orphan Parent 收养（绝不启动第二个 Agent）、operator SIGTERM | 完成 |
| CLI | `python -m supervisor {init,run,parent-once,status,events,stop,resume}` | 完成 |

## 关键设计落地

- 文件所有权严格分离：`.agent/state.json`（Parent 写）/ `.supervisor/runtime.json`（Supervisor 写）。
- 状态判定永远以 fresh 的 `.agent/state.json` 为准；stdout 只作审计，不做机器协议。
- 每次循环最先 `enforce_limits()`；stop_reason 固定枚举。
- 运行时在 spawn 后立即持久化 pid + `/proc/<pid>/stat` starttime，Supervisor 自身崩溃后按进程身份收养孤儿。
- 全程零 LLM：测试用 `FakeParentRunner`（进程内）和 `tests/fixtures/fake_dsh.py`（可执行脚本）。
- 零第三方运行时依赖（`tomli` 仅 Python < 3.11 时回退）；Python 3.8 兼容。

## 测试

`python3 -m pytest` → **71 passed（11.45s）**

- M1 存储/事件/锁/配置/模型 25
- M2 DshRunner 7（含超时 SIGTERM→SIGKILL 升级、fake dsh 集成）
- M3–M5 引擎 FSM 21（文档五十一 01–09、15–24 场景，fake runner）
- CLI 12（含 kill -9 后重启收养孤儿、SIGTERM 停止并清进程组 端到端）
- Git 探针 3
- 对抗补充 3（外部杀 Parent 检测为 crash、第二个 supervisor 被锁拒绝 rc=2、预算计费回归）

## 端到端验收（自动化）

1. 可随时杀掉一个 Parent（外部 SIGKILL）→ Supervisor 检测 PARENT_CRASH、计数、重启。
2. 可杀掉 Supervisor 本身（kill -9）→ 重启后收养还活着的 Parent，绝无第二个 Agent；孤儿退出后从
   `.agent/state.json` + Git 状态继续至 COMPLETED。
3. 超时 → 先 SIGTERM 进程组，宽限后 SIGKILL；进程组必清。
4. 双 Supervisor 并发 → 第二个被拒（"Supervisor already running for this repository."，rc=2）。

## 环境限制（非缺陷，已在文档五十四预判）

- 当前环境根文件系统只读，`~/.dsh` 不可写，真实 `dsh --profile headless` 无法启动（EROFS）。
  DshRunner 的 executable/profile 可配置；接入真实 headless 需可写的 DSH_HOME 并为 headless profile 配置插件。

## 后续里程碑（按文档规划，未纳入本轮）

M7 CI 轮询（GitHub provider，先 Fake）、M8 人工评审自动接入 GitHub、V2 token/cost 计量、多任务调度。

## 结论

M0–M5 交付完成，71 项测试全绿，验收场景全部自动化覆盖。按文档顺序下一步是 M6 之后的 CI（M7）。