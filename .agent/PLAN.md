# .agent/PLAN.md — M5 hardening（第 1+2 轮评审意见）

## 目标

按两轮评审意见完成 M5 hardening：

- 第一轮：收养孤儿期 `stop` 杀整个进程组；`STARTING_PARENT` 崩溃窗口用
  `launcher+process.json` 消除重复 Parent；收养期 timeout/wall-time 继续生效；
  `supervisor stop` 增加 Supervisor 身份校验；文件名统一；`cmdline` 校验接入
  恢复；`PARENT_STARTING`/`PARENT_STARTED` 拆分。
- 第二轮（本轮）：① Parent lease（`parent.lock` flock 由 launcher→exec 后的
  DSH 继承持有；拿不到租约绝不 spawn 第二个 Parent）；② 终止必须确认整个 PGID
  消失（不只 leader PID）；③ 收养 timeout 走 `RECOVER_AFTER_PARENT_TIMEOUT` +
  退避（不用终态 `StopReason.MAX_TIMEOUTS` 表达内部 kill 原因）；④ orphan 自退
  且状态未知 → 保守 `RECOVER_AFTER_PARENT_CRASH`；⑤ 实现 `STOPPING`
  （operator-stop 收尾落盘，崩溃后重启完成收尾）；⑥ 文档同步。

## 任务清单

| ID | 任务 | 状态 |
|---|---|---|
| H1 | launcher 自写 process.json + DshRunner exec 链改造 | DONE |
| H2 | models: ParentInfo.token / RuntimeState.supervisor_process_start_id | DONE |
| H3 | engine: 三态恢复 + record reconciliation + adoption 限额/超时 | DONE |
| H4 | process_identity: is_dsh_process / is_proc_alive(僵尸判定) / storage/plan 命名统一 | DONE |
| H5 | events: PARENT_STARTING/KILLED/RECONCILED/SPAWN_UNCONFIRMED/RECORD_STALE | DONE |
| H6 | cli: stop 身份校验（pid+start_id），init/status/events 不变 | DONE |
| H7 | prompts/AGENTS.md/.agent/PLAN.md 统一，协议文档 13 章重写 | DONE |
| H8 | tests/test_hardening.py 7 项 + 既有 71 项 全绿 | DONE |
| H9 | **Parent lease**：lock.ParentLease + layout.parent_lock_path + launcher 记录
      parent_lock_fd + DshRunner pass_fds/SUPERVISOR_PARENT_LOCK_FD + engine
      `_ensure_lease`（拿不到租约不 spawn）+ cli parent-once 持锁 | DONE |
| H10 | **整组终止**：process_identity.process_group_alive + terminate_process_group /
      DshRunner._terminate_group 改为 PGID 判据（SIGTERM→grace→SIGKILL→确认 PGID 消失） | DONE |
| H11 | 收养 outcome：internal KillReason（OPERATOR/PARENT_TIMEOUT/WALL/STALE_GROUP）
      + timeout→RECOVER_AFTER_PARENT_TIMEOUT+退避 + orphan 自退→保守 CRASH 恢复 | DONE |
| H12 | **STOPPING**：收养/激活 stop 路径落盘 STOPPING；重启见到 STOPPING →
      完成未完成的 stop（process.json 找回 pid）→ STOPPED_OPERATOR，绝不 spawn | DONE |
| H13 | 文档同步：protocol §2/§4.1/§12/§13、AI_automated_coding.md 历史声明、
      delivery-report.md historical、pyproject 声明 tomli（3.8–3.10）/requires-python | DONE |
| H14 | tests/test_hardening2.py 8 项（lease×2、PGID 组杀×1、收养 timeout 语义、
      orphan 自退语义、STOPPING×3）全绿 | DONE |

## 验收（可命令验证）

- `python -m pytest -q` → **86 passed**
- lease 被旧 activation 占用 → 重启 Supervisor 绝不 spawn（PARENT_LEASE_HELD），
  租约释放后才 spawn（PARENT_SPAWN_UNCONFIRMED）
- 终止后整个 PGID 消失（含忽略 SIGTERM 的子进程），不只 leader
- 收养期 timeout 下一轮 prompt = `RECOVER_AFTER_PARENT_TIMEOUT`，并发生退避
- orphan 自退且状态 RUNNING → 下一轮 prompt = `RECOVER_AFTER_PARENT_CRASH`
- operator-stop 收尾期间 runtime 落盘 `STOPPING`；STOPPING 崩溃重启 →
  完成收尾（杀组 → STOPPED_OPERATOR），绝不 spawn

## 出域

不做 M6/M7/M8（CI 轮询、GitHub review、token/cost）。M6 只落地了只读 Git 探测与
activation 前后快照，完整 M6 验收不在本轮范围。