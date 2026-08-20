# Supervisor Protocol (V1) — 冻结的协议

> 本文档由 `AI_automated_coding.md` 的设计收敛而来，是 Supervisor 与 Parent
> Agent 之间的**唯一机器协议**。Parent 是开发 Agent；Supervisor 是确定性进程
> 管理器。
>
> **Supervisor 决定 WHEN。Parent Agent 决定 HOW。**

版本：1（对应 `supervisor.toml` 的 `version = 1` 与两个状态文件的
`schema_version = 1`）。

---

## 0. 命名空间原则（Source vs Runtime）

> **Supervisor source tree must never contain live runtime control files
> using the same reserved paths consumed from a target workspace.**

Supervisor 自身源码仓库**不得**携带会被目标运行时识别为"当前任务/当前状态"
的活跃控制文件。混淆会导致 self-hosting 冲突：用另一个 coding agent 维护
Supervisor 源码时，agent 会把产品运行时文件当成自己的任务指令。

因此：

- Supervisor 源码中可提交的是**产品资源与配置模板**（`supervisor/resources/`、
  `supervisor.toml.example`、`docs/` 等）。
- 目标仓库的运行时状态（`.agent/`、`.supervisor/`、task 文件）永远由
  `supervisor init` / 首次 activation 在目标仓库内创建，**不在 Supervisor
  源码仓库中版本控制**。
- `AGENTS.md` 属于**目标仓库（用户）**，Supervisor 不拥有它；Parent 策略通过
  `supervisor/resources/parent-policy.md` 经 prompt 注入。

---

## 1. 职责边界

### Supervisor 负责（WHEN）

启动/监控/终止 Parent 进程、超时击杀、异常重启、`STOPPED_*` 判定、硬限额、
任务总墙钟、状态持久化、自身崩溃恢复、读取 `.agent/state.json`、观察 Git 的
机械状态、等待 CI（M7 起）/Human（M8 起）、决定是否启动下一轮 Parent、记录
完整事件日志。

### Supervisor 明确不负责（HOW）

需求分析、任务拆分、决定改哪些代码、跑项目 test/lint/build、解释测试失败、
修改代码、修 bug、code review、doublecheck、commit 内容设计、判断实现是否正确。

以上全部属于 Parent Agent → subagents。

### 禁止 Supervisor 执行的 git 命令

`add` `commit` `checkout` `reset` `merge` `rebase` `push` —— 都是 Parent 行为。
Supervisor 只用只读探测：`rev-parse`、`symbolic-ref`、`status --porcelain`、
`remote get-url`。

---

## 2. 文件所有权（最重要的规则）

| 文件 | Owner | 谁写 | 是否 Git tracked | 描述 |
|---|---|---|---:|---|
| 项目源码 | Target repo | 人/Parent | ✅ | 目标仓库的所有代码 |
| `AGENTS.md` | Target repo / user | 人 | ✅ 可选 | 目标仓库的 agent 补充说明（非 Supervisor 策略） |
| `supervisor.toml` | Target repo / user | 人 / `supervisor init` | ❌ (模板 `supervisor.toml.example` 被跟踪) | 配置（`[task].file` 指定 task 源，默认 `.supervisor/task.md`） |
| `supervisor/resources/parent-policy.md` | Supervisor package | Supervisor 维护者 | ✅ | Parent 角色/委托/验证/完成策略（经 prompt 注入） |
| `supervisor/resources/agent-state.schema.json` | Supervisor package | Supervisor 维护者 | ✅ | `agent/state.json` 示例 schema |
| `.supervisor/task.md` | Supervisor / User | 人（或 `supervisor init --task`） | ❌ | 任务描述（可经 `supervisor.toml [task].file` 配置） |
| `.agent/PLAN.md` | Parent | Parent | ❌ | 开发计划 |
| `.agent/STATE.md` | Parent | Parent | ❌ | Parent 自己的文档工作流 |
| `.agent/state.json` | Parent | Parent | ❌ | 开发任务做到哪了（Supervisor 只读） |
| `.supervisor/runtime.json` | Supervisor | Supervisor | ❌ | 自动化系统运行到哪了 |
| `.supervisor/events.jsonl` | Supervisor | Supervisor | ❌ | 只追加的事件日志 |
| `.supervisor/lock` | Supervisor | Supervisor | ❌ | 独占锁（Supervisor 唯一性） |
| `.supervisor/parent.lock` | Supervisor | Supervisor | ❌ | **Parent 唯一性租约**（flock；由 launcher→exec 后的 DSH 继承持有） |
| `.supervisor/runs/activation-NNNNNN/` | Supervisor | Supervisor | ❌ | 每轮 Parent 的运行审计目录 |
| `.supervisor/inbox/` | Supervisor | Supervisor | ❌ | 交给 Parent 的资料（如 CI failed 材料） |

两个角色**永不写同一个文件**。`runtime.json` 永远只能由 Supervisor 原子写。

> **不变式**：没有明确存在的 active task source（`supervisor.toml [task].file`
> 指向的文件），就绝不启动任何 Parent。`supervisor init --task <file>` 持久化
> task 路径到 `supervisor.toml`；`supervisor run` / `parent-once` 在 spawn 前
> 均验证 task 文件存在，否则 `error: task file not found`（rc=1）。

---

## 3. Agent 状态（`.agent/state.json`）

### 3.1 枚举

`status` 只允许五个值：

```
RUNNING     更多开发工作要做；本轮 Parent 主动结束激活
WAIT_CI     需要远端 CI 完成才能继续；必须记录准确 commit SHA
WAIT_HUMAN  需要人工介入
COMPLETED   开发任务真正完成
BLOCKED     自主推进已无可能
```

禁止把 Parent 内部语义（`TESTING`/`FIXING`/`REVIEWING`/`WRITING_CODE`…）
写进 `status`。Supervisor 不理解这些。

### 3.2 Schema（V1）

```json
{
  "schema_version": 1,
  "task_id": "TASK-001",
  "status": "RUNNING",
  "checkpoint_seq": 7,
  "checkpoint": { "phase": "implementation", "summary": "..." },
  "git": { "branch": "...", "head": "abc1234", "pushed_head": null },
  "ci": { "sha": null, "status": "NONE" },
  "next_action": "Continue implementation",
  "blocker": null,
  "updated_at": "2026-08-18T06:00:00Z"
}
```

Supervisor 强制校验：`schema_version == 1`、`status` 在枚举内、
`checkpoint_seq` 为非负整数、`updated_at` 存在。其余字段 Parent 自由使用。

### 3.3 `checkpoint_seq` 规则

Parent **每次**更新 `state.json` 都必须 `checkpoint_seq += 1`。Supervisor 记录
`last_agent_checkpoint_seq`，以此判断读到的状态是不是**本轮新产生**的，避免把
上一轮残留的 `WAIT_CI` 误当成当前激活的产出。

### 3.4 原子写（Parent 也必须遵守）

```
先写 .agent/state.json.tmp
flush + fsync
os.replace → .agent/state.json
```

Unix rename 原子。Supervisor 读到半写文件 = invalid state → `INVALID_AGENT_STATE`。

---

## 4. Supervisor 状态（`.supervisor/runtime.json`）

### 4.1 枚举

```
BOOTING           启动/恢复
STARTING_PARENT   正在启动 Parent
RUNNING_PARENT    Parent 运行中
RESTART_BACKOFF   崩溃后退避等待
WAITING_CI        等待 CI（M7+）
WAITING_HUMAN     等待人工（M8+）
STOPPING          收到停止请求，正在收尾（终止宽限期内落盘；过程中崩溃、
                  重启后完成收尾，见 §12）
STOPPED_SUCCESS   成功完成（TASK_COMPLETED）
STOPPED_BLOCKED   Parent 声明 BLOCKED
STOPPED_LIMIT     触发硬限额
STOPPED_ERROR     内部错误 / 无效状态
STOPPED_OPERATOR  操作员停止
```

没有 `IMPLEMENT/TEST/REVIEW/FIX` —— 那些是 Parent 的语义。

### 4.2 Schema（V1）

```json
{
  "schema_version": 1,
  "status": "RUNNING_PARENT",
  "task_started_at": "2026-08-18T05:00:00Z",
  "current_parent": {
    "activation_id": 6,
    "pid": 412514,
    "process_start_id": "123456",
    "started_at": "2026-08-18T06:02:00Z",
    "reason": "RECOVER_AFTER_CRASH",
    "activation_token": "a3b830670d2546cdb7851f4f281ef3aa"
  },
  "counters": {
    "parent_activations": 6,
    "crash_restarts": 1,
    "clean_restarts": 1,
    "timeouts": 0,
    "ci_wakeups": 0
  },
  "limits": { "...": "镜像 supervisor.toml，便于审计" },
  "last_agent_checkpoint_seq": 7,
  "supervisor_pid": 1000,
  "supervisor_process_start_id": "123457",
  "active_budget": { "accrued_seconds": 0.0, "last_mark": "..." },
  "stop_reason": null
}
```

### 4.3 原子写

`runtime.json` 永远走 `tmp + fsync + os.replace`（见 `storage.atomic_write_json`）。
Supervisor 自己在写一半时崩溃，磁盘上的原文件不损坏。

---

## 5. DSH exit code 不是任务状态

```
0 = 这一 Agent turn 正常结束
1 = runner/error
```

`Parent exit 0 + state RUNNING` 的含义是：**Parent 正常结束了这一轮，但任务还
没有到稳定终止状态** → Supervisor 应再次启动 Parent（事件
`PARENT_CLEAN_EXIT_WITH_RUNNING_STATE`）。

状态判定永远以 fresh 的 `.agent/state.json` 为准，不解析 stdout/stderr 内容。
stdout 只是人类日志/审计。

---

## 6. 状态优先级（写进代码）

1. Supervisor hard limits
2. Operator stop
3. fresh `.agent/state.json`
4. Parent process result
5. 上一轮 runtime 状态

例：Parent 写 `WAIT_CI` 后 exit code = 1。只要 `state.json` JSON 合法、
`checkpoint_seq` 增加、`updated_at` 在本轮 activation 之后、CI SHA 合理 →
Supervisor 仍进入 `WAITING_CI`，同时记录 `parent_exit_code=1`、
`state_transition=WAIT_CI`、`anomaly=true`。不因此让第二个 Parent 乱入。

---

## 7. 重启策略

| 情形 | 判定 | 计数 | 下一轮 prompt |
|---|---|---|---|
| Process Crash | exit != 0 且 state=RUNNING / 无新 checkpoint | `crash_restarts += 1` + 退避 | `RECOVER_AFTER_PARENT_CRASH` |
| Clean Exit Not Finished | exit == 0 且 state == RUNNING | `clean_restarts += 1` | `CONTINUE` |
| Timeout | `timed_out == true` | `timeouts += 1` | `RECOVER_AFTER_PARENT_TIMEOUT` |
| WAIT_CI / WAIT_HUMAN / COMPLETED / BLOCKED | 按状态分派 | 相应计数 | 相应事件 |

退避序列（`[restart] backoff_seconds`）：`2, 5, 15, 30, 60`（超出取末值），
应用于 crash 与 timeout 后。

---

## 8. 硬限额（每次循环最先检查 `enforce_limits()`）

| 限额 | 停止原因 |
|---|---|
| 超过 `max_parent_activations` | `MAX_PARENT_ACTIVATIONS` |
| 超过 `max_crash_restarts` | `MAX_CRASH_RESTARTS` |
| 超过 `max_clean_restarts` | `MAX_CLEAN_RESTARTS` |
| 超过 `max_timeouts` | `MAX_TIMEOUTS` |
| 超过 `max_active_wall_seconds` | `MAX_ACTIVE_WALL_TIME` |
| CI 等待超 `max_wait_seconds`（M7+） | `CI_WAIT_TIMEOUT` |

## 9. Stop Reason 固定枚举

```
TASK_COMPLETED | TASK_BLOCKED | MAX_PARENT_ACTIVATIONS | MAX_CRASH_RESTARTS
MAX_CLEAN_RESTARTS | MAX_TIMEOUTS | MAX_ACTIVE_WALL_TIME | CI_WAIT_TIMEOUT
INVALID_AGENT_STATE | SUPERVISOR_INTERNAL_ERROR | OPERATOR_STOP
```

禁止只存 `"something went wrong"`。

---

## 10. 事件日志（`.supervisor/events.jsonl`）

只追加，绝不覆盖。每行一个 JSON 对象：

```json
{"ts":"...","event":"SUPERVISOR_STARTED"}
{"ts":"...","event":"PARENT_STARTING","activation":1,"reason":"INITIAL_START","activation_token":"a3b8..."}
{"ts":"...","event":"PARENT_STARTED","activation":1,"pid":1234,"process_start_id":"123456","reason":"INITIAL_START"}
{"ts":"...","event":"PARENT_EXITED","activation":1,"exit_code":0,"timed_out":false}
{"ts":"...","event":"AGENT_STATE","status":"WAIT_CI","checkpoint_seq":4}
{"ts":"...","event":"PARENT_CLEAN_EXIT_WITH_RUNNING_STATE","activation":1}
{"ts":"...","event":"PARENT_CRASH","activation":1,"exit_code":1}
{"ts":"...","event":"PARENT_TIMEOUT","activation":1}
{"ts":"...","event":"PARENT_KILLED","activation":1,"pid":1234,"reason":"PARENT_TIMEOUT"}
{"ts":"...","event":"PARENT_KILL_FAILED","activation":2,"pid":1234,"reason":"PARENT_TIMEOUT"}  # 终止失败（进程组仍存活）
{"ts":"...","event":"RESTART_BACKOFF","seconds":5,"reason":"RECOVER_AFTER_PARENT_CRASH"}
{"ts":"...","event":"LIMIT_REACHED","reason":"MAX_CRASH_RESTARTS"}
{"ts":"...","event":"SUPERVISOR_STOPPED","status":"STOPPED_LIMIT","stop_reason":"MAX_CRASH_RESTARTS"}
```

`grep PARENT_STARTED .supervisor/events.jsonl` 即可回答"为何唤醒 N 次 Parent"。

---

## 11. 运行目录（`.supervisor/runs/activation-NNNNNN/`）

每轮 Parent 一个目录，保存 `prompt.txt`、`stdout.log`、`stderr.log`、
`process.json`（launcher 自写 pid/start_id/token）、`result.json`、
`git-before.json`、`git-after.json`。`result.json`：

```json
{
  "activation_id": 7, "reason": "CI_FAILED",
  "started_at": "...", "ended_at": "...", "duration_seconds": 382.4,
  "exit_code": 0, "timed_out": false,
  "agent_state_before": {"status": "WAIT_CI", "checkpoint_seq": 6},
  "agent_state_after": {"status": "RUNNING", "checkpoint_seq": 8},
  "git_before": {"branch": "...", "head": "...", "dirty": false},
  "git_after": {"branch": "...", "head": "...", "dirty": true}
}
```

完整 audit trail。

---

## 12. 进程生命周期

- Parent 必须独立 process group（`start_new_session=True`），且进程组组长即
  Parent 本体。
- 超时：`SIGTERM` 进程组 → 等待 `terminate_grace_seconds`（默认 10s）→ 仍不退
  出则 `SIGKILL` 进程组 → **确认整个 PGID 消失**（`os.killpg(pgid, 0)` 抛
  `ProcessLookupError`，或 /proc 扫描只剩僵尸）。绝不只检查 leader PID：
  leader 死了但忽略 SIGTERM 的子进程/子 agent 也必须在同一轮被清。
- **Parent 唯一性租约（Parent lease）**：Supervisor 在每次 spawn 前
  `flock` `.supervisor/parent.lock`，并把已锁 FD 通过 `pass_fds` +
  `SUPERVISOR_PARENT_LOCK_FD` 传给 launcher；`os.execvp` 不关闭该 FD，
  因此 exec 后的 DSH 进程继续持有租约。
- 租约是 **FD handoff，不是释放**：激活收尾时 Supervisor 只 `close` 自己那份
  FD 副本，**绝不 `LOCK_UN`**（flock 锁绑定在 open-file-description 上，对共享
  OFD 的继承 FD 执行 `LOCK_UN` 会连 DSH 的租约一起解掉）。子进程存活 → 锁继续
  被 DSH 持有；直到最后一个持有该 OFD 的 FD 关闭，内核才自动释放锁。
  作用分工：`process.json` = **身份发现**（pid/starttime/token）；
  `parent.lock` 租约 = **唯一性保证**。重启的 Supervisor 拿不到租约 =
  存在活着的旧 activation = **绝不 spawn 第二个 Parent**。
- Supervisor 只在 `dsh` 与只读 `git`/CI 工具之间受限启动程序；**绝不**
  `shell=True` 执行任意字符串。
- Supervisor 收到 SIGINT/SIGTERM：runtime 置 `STOPPING` 并落盘 → SIGTERM
  Parent 进程组 → grace → SIGKILL → 确认 PGID 消失 → `STOPPED_OPERATOR` →
  释放锁。不把 Parent 悄悄留后台。
- `STOPPING` 是 **durable stop intent**：恢复启动时磁盘**继续保持 `STOPPING`**
  （绝不先落盘成 `BOOTING`），直到整个 Parent 组确认消失、真正写成
  `STOPPED_OPERATOR`——无论重启多少次（`STOPPING → crash → STOPPING → …`）
  都不会丢失 stop intent。
- 完成 STOPPING 收尾时：PID 已知且身份可验证 → 杀组；PID 未知 → 经
  `process.json`（token 匹配）找回；无可信身份 → 看 `parent.lock` 租约：
  空闲 = 没有活着的 launcher/DSH → 完成 stop；被占 = 旧 launcher 可能仍存活
  → **绝不结束 stop**，继续等 record / 等租约释放。
- **终止失败必须显式失败**：`terminate_process_group` / `_terminate_group`
  若 SIGKILL+确认窗口后整个 PGID 仍存在 → 返回失败；Supervisor **绝不**在
  Parent 仍 alive 时写 `STOPPED_OPERATOR`，改为 `STOPPED_ERROR`
  （`SUPERVISOR_INTERNAL_ERROR`），并**保留 `current_parent` 身份**
  （activation_id/pid/start_id/token），绝不丢给后续排查/重启 reconciliation。
- 审计区分成败：终止成功记 `PARENT_KILLED`；终止失败记 `PARENT_KILL_FAILED`，
  不用同一个事件既表示 "killed" 又表示 "没能 kill 掉"。
- 按 PGID 杀组**必须身份可验证**：`start_id` 缺失或与 `/proc` starttime 不符
  时，绝不按裸 pid 杀（PID 复用风险）；若此时 `process_group_alive(recorded_pgid)`
  仍为 True，则**必须 fail-closed**（`PARENT_KILL_FAILED` /
  `UNVERIFIABLE_PROCESS_GROUP` → `STOPPED_ERROR + keep_parent`），
  绝不能视为清理成功而继续 spawn（宁可留给 operator）。
- `supervisor.toml [task].file` 与 CLI `--task` 均为 repo-relative；含 `..` 的
  路径逃逸在配置解析与 `Layout.task_file()` 两层被拒绝；`AGENTS.md` 属于目标
  仓库（可选补充说明），Supervisor 的 Parent 策略仅通过
  `supervisor/resources/parent-policy.md` 注入，不要求目标仓库覆盖 `AGENTS.md`。
- 统一的 stop 收尾 reconciliation：无论 STOPPING 来自“崩溃后恢复”还是“当前进程
  刚收到 SIGTERM 取消激活”，都走同一套 —— PID 已验证 → 杀组；PID 未知 →
  `process.json`（token 匹配）；record 未知 → `parent.lock` 租约；确认没有
  activation 后才 `STOPPED_OPERATOR`。

---

## 13. 崩溃恢复契约（M5 hardening）

Supervisor 重启时三态恢复（`NO_PARENT` / `STARTING_PARENT` / `RUNNING_PARENT`）：

```
BOOT → 拿独占锁 → 读 runtime.json → 读 .agent/state.json
     → 若 runtime 定格 STOPPING（上次 operator-stop 收尾中崩溃）
         磁盘**继续保持 STOPPING**（不复位 BOOTING）→ 完成收尾：
         杀仍在的 Parent 组（pid 未知时经 process.json/租约等待找回；进程组
         必须确认消失，否则 STOPPED_ERROR）→ STOPPED_OPERATOR
     → 若状态为 STARTING_PARENT（pid 未知）：
         读 .supervisor/runs/activation-N/process.json
         token 一致 + pid 存在 + starttime 一致 + cmdline 仍是 DSH/launcher
           → 收养孤儿（PARENT_RECONCILED）
         记录缺失 → 宽限等待：
           仍无记录但 parent.lock 租约空闲（旧 launcher/DSH 必死）
             → 重新 spawn（PARENT_SPAWN_UNCONFIRMED）
           租约仍被占用（旧 launcher 活着、还没写记录）
             → 绝不 spawn，继续等记录/等租约释放（PARENT_LEASE_HELD）
     → 若状态为 RUNNING_PARENT：
         PID 存在 + starttime 一致 + cmdline 仍是 DSH/launcher
           → orphan 收养：不启动第二个 Parent，轮询直到整组消失
           收养期间：stop 到来→STOPPING 落盘→杀整组(PGID)；
           parent timeout / wall-time 继续生效
         PID 不存在 / 身份不一致 → 上一轮进程已死：若进程组还有残留成员先清组，
           按状态继续（见下"orphan 消失后的策略"）
```

防线：

- PID 复用：`pid + starttime + cmdline(contains dsh/launcher)` 三重校验。
- `supervisor stop`：`supervisor_pid + supervisor_process_start_id` 双重校验。
- **Parent lease**：`.supervisor/parent.lock` flock 由 launcher→exec 后的 DSH
  继承持有；`process.json` 负责身份发现，`parent.lock` 负责唯一性 —— 拿不到租约
  绝不 spawn 第二个 Parent，从根上封死 `spawn → child writes process.json` 窗口。
- 整组终止：SIGTERM→grace→SIGKILL 后确认**整个 PGID 消失**（含忽略 SIGTERM 的
  子进程），而不是只看 leader PID；确认失败（PGID 仍 alive）→ **失败收场**
  `STOPPED_ERROR`，绝不写 `STOPPED_OPERATOR`。
- 按 PGID 杀组必须身份可验证（`start_id` 缺失/不符则不杀）；`STOPPING` 在磁盘上
  durable 保持，直到真正 `STOPPED_OPERATOR`（二次崩溃也不丢 stop intent）。
- 事件拆分：`PARENT_STARTING`（意图持久化，含 token）→ `PARENT_STARTED`（确认，
  含 pid/process_start_id），便于审计与恢复回放。

**orphan 消失后的策略**（退出方式未知，按 durable agent 状态分派）：

| 状态 | 下一轮 |
|---|---|
| `COMPLETED` / `BLOCKED` / `WAIT_HUMAN` / `WAIT_CI` | 按状态分派（stop / wait） |
| `RUNNING` 或状态缺失 | **保守** `RECOVER_AFTER_PARENT_CRASH` + 退避（不是 `CONTINUE`） |
| 收养期 parent timeout | `timeouts += 1` + 退避 + `RECOVER_AFTER_PARENT_TIMEOUT` |
| 收养期 wall-time | 杀组，主循环以 `MAX_ACTIVE_WALL_TIME` 收场 |

---

## 14. Git Observation Contract（M6）

### 14.1 目标

Supervisor 能够机械回答：

```
is_git_repo / branch / head / detached_head / dirty / has_remote / remote_url
activation before / after 的机械差异
```

但**绝不**分析 diff 内容、判断功能是否完成、评价代码质量。

### 14.2 允许 / 禁止的 Git 命令

**允许（只读探测）：**

```
git rev-parse --is-inside-work-tree
git rev-parse HEAD
git rev-parse --verify <sha>
git symbolic-ref --short HEAD
git status --porcelain
git remote
git remote get-url <name>
```

**禁止（任何仓库 mutation）：**

```
git add / commit / reset / checkout（改变 working tree） / merge / rebase
git push / clean / restore（mutation 形式）
```

任何 `add/commit/checkout/reset/merge/rebase/push` 均属于 Parent 行为，Supervisor 绝不执行。

### 14.3 GitSnapshot V1 冻结 Schema

```json
{
  "is_git_repo": true,
  "branch": "feature/foo",
  "head": "abc123... (40-char SHA)",
  "detached_head": false,
  "dirty": true,
  "has_remote": true,
  "remote_url": "https://..."
}
```

非 Git 仓库：

```json
{
  "is_git_repo": false,
  "branch": null,
  "head": null,
  "detached_head": false,
  "dirty": false,
  "has_remote": false,
  "remote_url": null
}
```

字段语义：

| 字段 | 语义 |
|---|---|
| `is_git_repo` | `git rev-parse --is-inside-work-tree == true`；false 时其余字段为 null/false，Supervisor 不 crash |
| `branch` | 正常 branch 名；detached HEAD 时 `null` |
| `detached_head` | 是否 detached HEAD（`branch is null && is_git_repo && head != null`）；独立 bool，不让消费者猜 |
| `head` | 完整 40-char commit SHA；无法获得则 `null` |
| `dirty` | working tree/index 是否有 Git-visible changes（`status --porcelain` 非空）；不记录原因/文件/diff |
| `has_remote` | 是否存在至少一个 configured remote（**不是** `has_origin`）；`remote_url` 优先 `origin`，无 origin 则取确定性的第一个 remote（按 `git remote` 排序首个），否则 `null` |

`GitSnapshot` 位于 `supervisor/models.py`，`capture()` 位于 `supervisor/git_probe.py`，`compare_git_snapshots()` 提供纯机械比较（`head_changed/dirty_changed/branch_changed/remote_changed/detached_changed`），绝不推断业务语义。

### 14.4 Snapshot 生命周期与持久化

| 时机 | 路径 | 形式 |
|---|---|---|
| Supervisor startup | `.supervisor/git-startup.json` | `atomic_write_json`（`supervisor/storage.py:Layout.git_startup_path`）|
| 每次 activation before | `.supervisor/runs/activation-NNNNNN/git-before.json` | **crash-safe：先 capture → 原子落盘 → 再 spawn Parent** |
| 每次 activation after | `.supervisor/runs/activation-NNNNNN/git-after.json` | after capture 后原子落盘 |

`git-before.json` 的正确顺序（correctness invariant）：

```
allocate run_dir
→ capture git_before
→ atomic write git-before.json + fsync
→ persist STARTING_PARENT + PARENT_STARTING
→ spawn Parent
```

绝不能 `capture before → spawn → 完成后才写 before`，否则 `capture → kill -9 Supervisor` 会丢失 before evidence。

`git-startup.json` 用于 `Supervisor restart` 诊断与 recovery 上下文（见 §11 运行目录）。

### 14.5 Git Comparison

`compare_git_snapshots(before, after)` 只能得出：

```
HEAD changed / unchanged
dirty changed / unchanged
branch changed / unchanged
remote changed / unchanged
```

不得得出 `feature completed / bug introduced / dangerous diff`。

### 14.6 Git 事件

M6 增加机械事件（`supervisor/events.py`）：

```
GIT_SNAPSHOT         phase=startup|before|after + 全量字段
GIT_HEAD_CHANGED     activation + before/after SHA
GIT_DIRTY_CHANGED    activation + before/after dirty
GIT_BRANCH_CHANGED   activation + before/after branch
```

仅记录事实。

### 14.7 Recovery Context

Parent crash / Supervisor recovery 后，若存在 durable Git evidence（`git-before.json` / `git-after.json` / 当前 `capture`），recovery prompt 可中性告知 Parent：

```
Previous activation changed mechanical Git state.
Before: HEAD=... dirty=...
Current: HEAD=... dirty=...
Inspect the repository (git status/diff/log) before continuing.
```

`RECOVER_AFTER_PARENT_CRASH / RECOVER_AFTER_PARENT_TIMEOUT` 实际实现需读取最近一次 `activation-*/git-before.json` 与 `git-after.json`（或当前 `capture`）的 `head/dirty/branch` 并在 prompt 中以机械事实呈现，避免泛化提示。Parent 自行执行 `git status/diff/log` 判断；Supervisor 绝不嵌入 diff 内容。

---

## 15. CI Waiting Contract（M7）

- CI 必须绑定 **exact commit SHA**，绝不看 branch 最新 CI。
- CI 状态统一映射：`NOT_FOUND | PENDING | SUCCESS | FAILURE | CANCELLED | ERROR | TIMEOUT`（`supervisor/models.py:CiStatus`）。
- `CiObservation` / `CiMaterial` 定义见 `supervisor/ci/base.py`；字段仅用于 identity/state/diagnostics。
- 超时与轮询：`[ci] poll_seconds > 0`、`discovery_grace_seconds >= 0`、`max_wait_seconds > 0`；`NOT_FOUND` 在 `discovery_grace_seconds` 内继续等待，超出则按 `TIMEOUT` 处理。
- 等待循环：`WAITING_CI` → `get_status(exact SHA)` → `NOT_FOUND(grace 内继续) / PENDING / SUCCESS(→ CI_SUCCEEDED) / FAILURE(→ collect → CI_FAILED) / CANCELLED(→ CI_FAILED) / ERROR(→ 重试至 max_wait，绝不伪装为 CI_FAILED) / TIMEOUT`。
- `SUCCESS != COMPLETED`：Supervisor 绝不自行完成任务。
- Durability：`runtime.json: ci_wait {sha, provider, started_at, last_status, last_observed_at}`，`kill -9` 重启后继续等待同一 SHA，不启动普通 Parent。
- Inbox：`.supervisor/inbox/ci-<sha>/{observation.json, summary.txt, failed-jobs.json, logs/}`，幂等、crash-safe、有界（单文件 2 MiB 截断）。
- 日志为 untrusted data：只保存/size cap/safe decode/truncate + metadata，不解释、不执行；Parent prompt 仅写材料路径。
- `ci_wait` 仅在 `WAITING_CI` 时存在；离开该状态后清空。

## 16. Human Gate（M8）

- 持久化：`.supervisor/inbox/human/event-XXXX.json` 追加式（`supervisor/human_events.py:HumanEventStore`），**不是**单文件 `resume.json`（`resume.json` 保留为 legacy 兼容 shims，M8 起新事件一律走 inbox/human）。
- `HumanEvent V1`：`{event_id, event_type: HUMAN_APPROVED|HUMAN_CHANGES_REQUESTED, created_at, message?, attachment_path?, gate_id, status: PENDING→DELIVERING→DELIVERED}`，`event_id` 单调递增（如 `human-000003`），`gate_id` 强制绑定到触发该事件的 `human_gate.gate_id`，进入 `event file / runtime / events.jsonl / activation prompt`。
- CLI：`supervisor resume [repo] --event HUMAN_APPROVED [--message TEXT] [--file feedback.md] [--gate-id GATE]`；`--file` 复制进 `inbox/human/attachments/<event_id>/`。默认自动绑定当前 `WAITING_HUMAN` 的 `human_gate.gate_id`；非 `WAITING_HUMAN` 时拒绝（不产生可被未来 gate 误消费的未绑定事件）；`--gate-id` 显式绑定时校验与当前 gate 一致。
- FSM：`WAIT_HUMAN` 期间不启动 Parent，直到 durable **gate-bound** event 出现；`PENDING → DELIVERING（事件被选中后落盘 runtime.human_event_id） → DELIVERED（该事件对应的 Parent activation 完成退出后）`，`DELIVERING` 重启后可重试（同一 `event_id` 可能触发第二次 activation，需 Parent 侧幂等处理），绝不静默丢失；未绑定或错 gate 的事件对当前 gate 不可见（`next_pending(gate_id)` 强制精确匹配）；`active wall-time` 在 `WAITING_HUMAN` 且 `pause_active_wall_clock=true` 时暂停。
- 事件幂等：同一 `event_id` 可重试交付，但 `HumanEventStore` 按追加顺序 FIFO 去重；Parent 必须对同一 `event_id` 的重复交付结果幂等（检查已应用的变更/已存在的 review 状态）。

## 17. Parent Delivery Contract（M9）

- 标准链路（`supervisor/resources/parent-policy.md` 冻结）：`Read task → AGENTS.md → Inspect repo → acceptance criteria(PLAN) → decomposition → subagents → Implementation → inspect diff → targeted tests → format → typecheck → lint → build → broader tests → doublecheck → commit → push → WAIT_CI`。工具缺失则跳过对应校验。
- 子代理契约：仅边界清晰、验收明确、ownership 不冲突的任务才委派；每个委派含 `目标/范围内/范围外/验收/验证`。
- Parent 不得盲信 subagent：必须检查实际 diff 后才 commit。
- Fresh reviewer：实现后由独立 reviewer 检查 `correctness/edge cases/regression/security/compatibility`，Parent 决定 `accept/fix/re-review`。
- `WAIT_CI` 准入：`local validation passed + commit exists + push completed + exact SHA known + git.head == ci.sha && git.pushed_head == ci.sha（若提供）`，否则 `AgentStateError → STOPPED_ERROR`（`supervisor/models.py:AgentState.from_dict` 强制校验）。

## 18. PR / Review Loop（M10）

- 归属：`create/find/update PR、title/body、read review、repair、merge（仅当 policy 授权）` 均属 Parent；Supervisor 仅持久化 `review` 机械引用。
- 幂等：同一 delivery lane 必须 `lookup existing PR → reuse/update`，禁止 `blind create` 导致 crash-recovery 后重复 PR。
- `review` 字段（`agent/state.json` 可选）：`{provider, pr_number, pr_url, head_sha}`，仅机械引用，Supervisor 校验 `pr_number:int, pr_url:str, head_sha:hex`，不解释内容。
- 循环：`WAIT_CI→CI_SUCCESS→Parent PR→WAIT_HUMAN→HUMAN_CHANGES_REQUESTED→repair→WAIT_CI(new SHA)→CI_SUCCESS→update PR→WAIT_HUMAN→HUMAN_APPROVED→Parent verify delivery→COMPLETED`；支持多轮 `changes-requested`。
- `COMPLETED` 仅 Parent 确认 `acceptance criteria + CI + human gate` 后写入；`CI success` 与 `human approval` 均不自动等同 `COMPLETED`。Supervisor 绝不替 Parent `merge`。

---

## 19. 里程碑落点（M0–M5）

- 前 3 分钟 CI `NOT_FOUND` 不算失败（discovery grace）—— M7。
- Token/cost 不做假硬限额（不按字符串长度乘价格）—— V2。
- 本阶段成功标准（唯一）：

> **我可以随时杀掉一个 Parent，甚至杀掉 Supervisor 本身，然后重新启动
> Supervisor，它仍然能够在不破坏仓库、不启动重复 Agent 的情况下，从
> `.agent/state.json + .agent/PLAN.md + Git state` 恢复开发任务。**
