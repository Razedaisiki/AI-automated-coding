# .agent/STATE.md — 当前执行状态

## 阶段

M5 hardening（第 1+2 轮评审）全部完成，全量测试绿，待提交推送。

## 完成情况

- 第一轮 hardening 7 项已完成（收养+stop 杀孤儿、STARTING 记录 reconcile/重 spawn、
  事件时序、收养期超时、stop 身份校验）。
- 第二轮（本会话）新增 8 项回归测试（tests/test_hardening2.py）：
  - Parent lease：锁被占 → 绝不 spawn，释放后安全重 spawn（红→绿）。
  - Parent lease：launcher→exec 后 DSH 继承已锁 FD；子进程死后锁自动释放；
    process.json 记录 parent_lock_fd。
  - 整组终止：忽略 SIGTERM 的子进程必须被 SIGKILL，整个 PGID 消失。
  - 收养期 timeout → RECOVER_AFTER_PARENT_TIMEOUT prompt + 退避，非终态 StopReason。
  - orphan 自退且状态未知 → 保守 RECOVER_AFTER_PARENT_CRASH。
  - STOPPING 落盘（operator-stop 收尾窗口可观测）；STOPPING 崩溃重启 → 完成收尾，
    绝不 spawn（含 STARTING pid 未知时经 process.json 找回真身杀组）。
- 既有 78 项全绿，合计 **86 passed**（`python -m pytest -q`，~33s）。
- `_pgid_alive` 测试助手改为"先按进程组判定、非组长回退 leader 判定"。

## 关键实现

- `lock.py::ParentLease`：flock 独占 `.supervisor/parent.lock`；引擎 spawn 前获取，
  DshRunner 经 `pass_fds`/`SUPERVISOR_PARENT_LOCK_FD` 交给 launcher→exec 后的 DSH；
  子进程死亡（内核关 FD）后自动释放。
- `process_identity.process_group_alive`：killpg 探测 + /proc 扫描排除僵尸；
  终止函数（terminate_process_group / DshRunner._terminate_group）以此为判据，
  SIGTERM→grace→SIGKILL→确认整个 PGID 消失。
- engine：`_ensure_lease`（拿不到租约不 spawn）；收养返回 Outcome（timeout →
  RECOVER_AFTER_PARENT_TIMEOUT+退避；自退 → RECOVER_AFTER_PARENT_CRASH）；
  internal `KillReason` 用于 PARENT_KILLED 审计，不动终态 StopReason；
  stop 路径落盘 `STOPPING`；`_complete_interrupted_stop` 完成中断的收尾。
- cli parent-once 也持有租约，禁止双 Parent。

## 阻塞项

无。

## 分支 / 提交

- master 历史：3aabeef (M0+M1), ecafa95 (M2), 44cbf55 (M3–M5), 7527a90, b360dac,
  5f116b4, bc5e860 (M5 hardening 第一轮)
- 本轮待提交：lock/process_identity/dsh_runner/launcher/engine/cli/events/models/
  storage/pyproject + tests/test_hardening2.py + 协议/设计/交付文档。