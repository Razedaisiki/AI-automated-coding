# .agent/FINAL_REPORT.md — 交付报告

## 任务

按 `AI_automated_coding.md` 实施 Supervisor（确定性进程管理器）M0–M5 hardening。

## 交付内容

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 协议冻结：`docs/supervisor-protocol.md`、`supervisor.toml`、`.agent/state.schema.example.json` | 完成 |
| M1 | `supervisor/{config,models,storage,events,lock}.py` —— 原子 JSON 写、JSONL 事件日志、fcntl 独占锁、状态校验 | 完成 |
| M2 | `dsh_runner.py` + `supervisor/launcher.py`（exec 前原子写 process.json，消除 spawn 崩溃窗口）、`prompts.py`、`parent-once` | 完成 |
| M3 | `engine.py` 本地主循环：INITIAL_START / RUNNING / COMPLETED / BLOCKED 分发，exit0/exit!=0 重启策略 | 完成 |
| M4 | 限额（activations/crash/clean/timeouts/active wall time）→ STOPPED_LIMIT、退避、事件审计 | 完成 |
| M5 hardening | 三态恢复（NO/STARTING/RUNNING）+ process.json reconciliation + 收养期限额/超时 + supervisor 身份校验 | 完成 |
| CLI | `python -m supervisor {init,run,parent-once,status,events,stop,resume}` | 完成 |

## 关键设计落地

- 文件所有权：`.agent/state.json`（Parent 写）/ `.supervisor/runtime.json`（Supervisor 写）/ `.agent/PLAN.md`（Parent 写，Supervisor 只读“read it”）。
- 状态判定永远以 fresh 的 `.agent/state.json` 为准；stdout 只作审计。
- `launcher.py`：Supervisor 先持久化 `STARTING_PARENT+token`，launcher 子进程 exec 前原子写 `process.json`（pid/start_id/token）——彻底消除 spawn→persist 微窗口。
- PARENT_STARTING（意图，含 token）→ PARENT_STARTED（确认，含 pid/start_id）审计拆分。
- 收养期：`stop`→杀进程组、`parent timeout`/`wall-time` 继续生效（`PARENT_TIMEOUT`/`PARENT_KILLED`）；`is_dsh_process(cmdline)` 接入恢复路径。
- `supervisor stop`：`supervisor_pid + supervisor_process_start_id` 双重校验，旧文件缺失 sid 时保守拒绝。
- 零第三方运行时依赖（`tomli` 仅 <3.11 回退）；Python 3.8 兼容（含 `is_proc_alive` 僵尸 Z 判定）。

## 测试

`python -m pytest` → **78 passed**

- M1 25 / M2 7（含超时 SIGTERM→SIGKILL） / M3–M5 引擎 21 / CLI 12 / Git 探针 3
- 对抗补充 3：外部杀 Parent、第二 supervisor 锁拒绝 rc=2、预算计费回归
- hardening 7：收养+stop 杀孤儿、STARTING 记录 reconcile/重 spawn、事件时序、收养期 parent timeout、stop 身份校验（错身份/缺失身份 均拒绝）

## 端到端验收（自动化）

1. 杀 Parent（外部 SIGKILL 进程组）→ PARENT_CRASH + crash_restarts 计数 + 进程组清空 → 恢复。
2. 杀 Supervisor（kill -9）→ 重启通过 `process.json`+token+pid/starttime+cmdline 收养，无重复 Parent；孤儿退出后继续。
3. 收养期 parent timeout / wall-time → 杀进程组、计数、STOPPED_LIMIT。
4. `supervisor stop` 错身份 PID → rc=1 不动目标进程。
5. 双 Supervisor 并发 → 第二个 rc=2 拒绝。

## 环境限制

根文件系统只读导致 `~/.dsh` 不可写，真实 `dsh --profile headless` 无法启动（EROFS）；已通过 DshRunner 可配置 executable + `supervisor/launcher.py` 链路验证，接入真实 headless 只需可写 DSH_HOME。

## 后续里程碑

M6 Git 快照已轻量落地；M7 CI、M8 人工评审、V2 token/cost 按文档属后续。