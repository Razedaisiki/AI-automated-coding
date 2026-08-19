# .agent/FINAL_REPORT.md — 交付报告

## 任务

按 `AI_automated_coding.md` 实施 Supervisor（确定性进程管理器）M0–M5 hardening，
并落实两轮评审意见（第二轮：Parent lease、整组终止、收养恢复语义、STOPPING、文档同步）。

## 交付内容

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 协议冻结：`docs/supervisor-protocol.md`（规范性）、`supervisor.toml`、`.agent/state.schema.example.json` | 完成 |
| M1 | `supervisor/{config,models,storage,events,lock}.py` —— 原子 JSON 写、JSONL 事件日志、fcntl 独占锁、状态校验 | 完成 |
| M2 | `dsh_runner.py` + `supervisor/launcher.py`（exec 前原子写 process.json）、`prompts.py`、`parent-once` | 完成 |
| M3 | `engine.py` 本地主循环：INITIAL_START / RUNNING / COMPLETED / BLOCKED 分发，重启策略 | 完成 |
| M4 | 限额（activations/crash/clean/timeouts/active wall time）→ STOPPED_LIMIT、退避、事件审计 | 完成 |
| M5 hardening R1 | 三态恢复 + process.json reconciliation + 收养期限额/超时 + supervisor 身份校验 + 文件/事件统一 | 完成 |
| M5 hardening R2 | **Parent lease** + **整组终止（PGID 确认）** + 收养恢复语义 + **STOPPING** + 文档/依赖声明 | 完成 |
| CLI | `python -m supervisor {init,run,parent-once,status,events,stop,resume}` | 完成 |

## 关键设计落地

- 文件所有权：`.agent/state.json`（Parent 写）/ `.supervisor/runtime.json`（Supervisor 写）/
  `.agent/PLAN.md`（Parent 写，Supervisor 只读“read it”）。状态判定以 fresh
  `.agent/state.json` 为准；stdout 只作审计。
- **Parent lease（唯一性）**：Supervisor spawn 前 `flock` `.supervisor/parent.lock`，
  把已锁 FD 经 `pass_fds`/`SUPERVISOR_PARENT_LOCK_FD` 交给 launcher → `os.execvp`
  后续由 DSH 继承持有；DSH 死亡时内核关 FD 自动释放。拿不到租约 = 存在活着的旧
  activation → **绝不 spawn 第二个 Parent**。`process.json` 只负责身份发现
  （pid/starttime/token），`parent.lock` 负责唯一性 —— 二者分工封死
  `spawn → child writes process.json` 窗口。
- **整组终止（P0-2）**：`process_group_alive`（killpg + /proc 排除僵尸）为判据；
  SIGTERM 整组 → grace → SIGKILL → **确认整个 PGID 消失**，忽略 SIGTERM 的子进程
  也必须在同一轮被清，绝不只看 leader PID。
- 收养恢复语义（P1）：收养期 parent timeout → `timeouts += 1` + 退避 +
  `RECOVER_AFTER_PARENT_TIMEOUT`；orphan 自退且状态未知 → 保守
  `RECOVER_AFTER_PARENT_CRASH`；内部 kill 原因用 `KillReason`
  （OPERATOR_STOP/PARENT_TIMEOUT/MAX_ACTIVE_WALL_TIME/STALE_GROUP_CLEANUP），
  不复用终态 `StopReason`。
- **STOPPING（P2）**：operator-stop 收尾（终止宽限期）期间 runtime 落盘
  `STOPPING`；在此窗口崩溃后重启，Supervisor 完成那次未完成的 stop
  （pid 未知时经 process.json 找回真身杀组）→ `STOPPED_OPERATOR`，绝不 spawn。
- PARENT_STARTING（意图，含 token）→ PARENT_STARTED（确认，含 pid/start_id）审计拆分。
- `supervisor stop`：`supervisor_pid + supervisor_process_start_id` 双重校验。
- 运行依赖：Python 3.8+；3.8–3.10 用 `tomli`（`pyproject.toml` 已声明），3.11+
  用标准库 `tomllib`。无其他第三方运行时依赖。

## 测试

`python -m pytest -q` → **86 passed**

- M1 25 / M2 7 / M3–M5 引擎 21 / CLI 12 / Git 探针 3 / 对抗 3 / hardening R1 7
- hardening R2 8：Parent lease（占用禁 spawn→释放后重 spawn；FD 继承+自动释放）、
  整组终止（忽略 SIGTERM 子进程被清）、收养 timeout 语义、orphan 自退语义、
  STOPPING 落盘 + 崩溃恢复完成收尾（含 STARTING pid 未知经记录找回）

## 端到端验收（自动化）

1. 杀 Parent（外部 SIGKILL 进程组）→ PARENT_CRASH + crash_restarts + 进程组清空 → 恢复。
2. 杀 Supervisor（kill -9）→ 重启经 process.json+token+pid/starttime+cmdline 收养，
   无重复 Parent；孤儿退出后继续。
3. lease 被占用 → 重启绝不 spawn（PARENT_LEASE_HELD）；租约释放后才重 spawn。
4. 收养期 parent timeout / wall-time → 杀整组、计数、正确恢复语义/STOPPED_LIMIT。
5. operator-stop 收尾期间 STOPPING 落盘；STOPPING 崩溃重启完成收尾，绝不 spawn。
6. `supervisor stop` 错身份/缺失身份 → rc=1 不动目标进程；双 Supervisor 并发 rc=2 拒绝。

## 环境限制

根文件系统只读导致 `~/.dsh` 不可写，真实 `dsh --profile headless` 无法启动（EROFS）；
已通过 DshRunner 可配置 executable（fake_dsh 零 token 脚本）+ launcher 真实链路验证，
接入真实 headless 只需可写 DSH_HOME。

## 后续里程碑

M6 read-only Git 探测 / activation 前后快照已部分落地（`git_probe.py`），
但完整 M6 验收不在本轮范围；M7 CI、M8 人工评审、V2 token/cost 属后续。