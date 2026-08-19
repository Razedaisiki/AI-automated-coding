# .agent/FINAL_REPORT.md — 交付报告

## 任务

按 `AI_automated_coding.md` 实施 Supervisor（确定性进程管理器）M0–M5 hardening，
并落实四轮评审意见（R2：Parent lease、整组终止、收养恢复语义、STOPPING；
R3：STOPPING 二次崩溃安全、lease-aware stop 收尾；R4：lease FD handoff、
kill-failure 保留身份 + fail-closed）。

## 交付内容

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 协议冻结：`docs/supervisor-protocol.md`（规范性）、`supervisor.toml`、`.agent/state.schema.example.json` | 完成 |
| M1 | `supervisor/{config,models,storage,events,lock}.py` —— 原子 JSON 写、JSONL 事件日志、fcntl 独占锁、状态校验 | 完成 |
| M2 | `dsh_runner.py` + `supervisor/launcher.py`（exec 前原子写 process.json）、`prompts.py`、`parent-once` | 完成 |
| M3 | `engine.py` 本地主循环：INITIAL_START / RUNNING / COMPLETED / BLOCKED 分发，重启策略 | 完成 |
| M4 | 限额（activations/crash/clean/timeouts/active wall time）→ STOPPED_LIMIT、退避、事件审计 | 完成 |
| M5 hardening R1 | 三态恢复 + process.json reconciliation + 收养期限额/超时 + supervisor 身份校验 + 文件/事件统一 | 完成 |
| M5 hardening R2 | Parent lease + 整组终止（PGID 确认）+ 收养恢复语义 + STOPPING + 文档/依赖声明 | 完成 |
| M5 hardening R3 | **durable STOPPING** + **lease-aware stop 收尾** + **终止失败失败收场** + **PGID 击杀身份可验证** | 完成 |
| M5 hardening R4 | **lease FD handoff**（close 不 LOCK_UN）+ **kill-failure 保留身份 + fail-closed** + **PARENT_KILL_FAILED 审计** + 统一 stop 收尾 | 完成 |
| CLI | `python -m supervisor {init,run,parent-once,status,events,stop,resume}` | 完成 |

## 关键设计落地

- 文件所有权：`.agent/state.json`（Parent 写）/ `.supervisor/runtime.json`（Supervisor 写）/
  `.agent/PLAN.md`（Parent 写，Supervisor 只读“read it”）。状态判定以 fresh
  `.agent/state.json` 为准；stdout 只作审计。
- **Parent lease（唯一性，FD handoff）**：Supervisor spawn 前 `flock`
  `.supervisor/parent.lock`，把已锁 FD 经 `pass_fds`/`SUPERVISOR_PARENT_LOCK_FD`
  交给 launcher → `os.execvp` 后续由 DSH 继承持有。激活收尾时 Supervisor 只
  `close` 自己的 FD 副本、**绝不 `LOCK_UN`**（flock 绑定 OFD，LOCK_UN 会连 DSH
  的租约一起解）。DSH 存活 → 租约仍被占用；直到最后一个持有该 OFD 的 FD 关闭，
  内核才自动释放。拿不到租约 = 存在活着的旧 activation → **绝不 spawn 第二个
  Parent**。`process.json` 只负责身份发现。
- **kill-failure fail-closed（R4）**：所有 `terminate_process_group(False)` 路径
  （收养 stop/超时、激活 timeout group_survived、stale 清理、取消、stop 收尾）统一
  转 `STOPPED_ERROR`（`SUPERVISOR_INTERNAL_ERROR`）且**保留 `current_parent`
  身份**（activation_id/pid/start_id/token），绝不 restart、绝不清空身份、绝不写
  `STOPPED_OPERATOR`。审计区分成败：终止成功 `PARENT_KILLED` / 失败
  `PARENT_KILL_FAILED`。
- **统一 stop 收尾 reconciliation（R4）**：operator-stop 取消激活与"崩溃后恢复
  STOPPING"走同一套：PID 已验证 → 杀组；PID 未知 → `process.json`（token 匹配）；
  record 未知 → `parent.lock` 租约；确认没有 activation 后才 `STOPPED_OPERATOR`。
- **整组终止（P0-2）**：`process_group_alive`（killpg + /proc 排除僵尸）为判据；
  SIGTERM 整组 → grace → SIGKILL → **确认整个 PGID 消失**；确认失败 → 显式失败。
- 收养恢复语义（P1）：收养期 parent timeout → `timeouts += 1` + 退避 +
  `RECOVER_AFTER_PARENT_TIMEOUT`；orphan 自退且状态未知 → 保守
  `RECOVER_AFTER_PARENT_CRASH`；内部 kill 原因用 `KillReason`，不复用终态
  `StopReason`。
- **STOPPING 二次崩溃安全（R3）**：runtime 恢复 STOPPING 时磁盘继续保持
  STOPPING（不复位 BOOTING）直到真正 `STOPPED_OPERATOR`。
- **PGID 击杀身份可验证（R3）**：`start_id` 缺失/不符 → 不按裸 pid 杀组。
- PARENT_STARTING（意图，含 token）→ PARENT_STARTED（确认，含 pid/start_id）审计拆分。
- `supervisor stop`：`supervisor_pid + supervisor_process_start_id` 双重校验。
- 运行依赖：Python 3.8+；3.8–3.10 用 `tomli`（`pyproject.toml` 已声明），3.11+
  用标准库 `tomllib`。无其他第三方运行时依赖。

## 测试

`python -m pytest -q` → **96 passed**

- M1 25 / M2 7 / M3–M5 引擎 21 / CLI 12 / Git 探针 3 / 对抗 3 / hardening R1 7 /
  hardening R2 8 / stopping-consistency+handoff R3–R4 10

## 端到端验收（自动化）

1. 杀 Parent（外部 SIGKILL 进程组）→ PARENT_CRASH + crash_restarts + 进程组清空 → 恢复。
2. 杀 Supervisor（kill -9）→ 重启经 process.json+token+pid/starttime+cmdline 收养，
   无重复 Parent；孤儿退出后继续。
3. lease 被占用 → 重启绝不 spawn（PARENT_LEASE_HELD）；租约释放后才重 spawn。
4. STOPPING 恢复 durable（磁盘保持 STOPPING）；STOPPING+pid 未知+lease held →
   等 record 杀组后才 STOPPED_OPERATOR；绝不把 Parent 留后台。
5. reconcile 等 lease 中 stop → 切入 STOPPING（无 PARENT_SPAWN_UNCONFIRMED）。
6. 终止失败 / 取消路径进程组仍 alive → STOPPED_ERROR（SUPERVISOR_INTERNAL_ERROR）
   且 **current_parent 身份保留**；审计 = PARENT_KILL_FAILED（非 PARENT_KILLED）。
7. lease 只 close 不 LOCK_UN（handoff）：supervisor close 自己副本后子进程仍持有
   租约（新 Supervisor 拿不到）；子进程死后锁自动释放。
8. 收养期 parent timeout / wall-time → 杀整组、计数、正确恢复语义/STOPPED_LIMIT。
9. `supervisor stop` 错身份/缺失身份 → rc=1 不动目标进程；双 Supervisor 并发 rc=2 拒绝。

## 环境限制

根文件系统只读导致 `~/.dsh` 不可写，真实 `dsh --profile headless` 无法启动（EROFS）；
已通过 DshRunner 可配置 executable（fake_dsh 零 token 脚本）+ launcher 真实链路验证，
接入真实 headless 只需可写 DSH_HOME。

## 后续里程碑

M6 read-only Git 探测 / activation 前后快照已部分落地（`git_probe.py`），
但完整 M6 验收不在本轮范围；M7 CI、M8 人工评审、V2 token/cost 属后续。