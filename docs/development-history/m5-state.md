> **Historical artifact** — archived from `.agent/` at Runtime Namespace Separation.
> Not a live task file. See `docs/supervisor-protocol.md §0` for ownership.

# .agent/STATE.md — 当前执行状态

## 阶段

M5 hardening（第 1+2+3+4 轮评审）全部完成，全量测试绿，已提交推送。

## 完成情况

- 第 1–3 轮相关内容见前几版 STATE 记录（R1 7 项 / R2 8 项 / R3 6 项测试）。
- 第四轮（本会话，交付前的两处 P0 收尾）：
  - lease FD handoff：`ParentLease.release()` 改为只 close 自己的 FD 副本
    （永不 LOCK_UN）；DSH 存活时 supervisor close 不破坏租约；kill-failure
    场景不再被 supervisor 主动解锁（测试：handoff 后子进程活着租约仍被占）。
  - kill failure 保留身份 + fail-closed：`_finalize(keep_parent=True)`；
    收养 stop/超时、激活 timeout group_survived、stale 清理失败、取消路径均
    保留 current_parent 身份并转 STOPPED_ERROR，绝不 restart、绝不发 PARENT_KILLED。
  - 审计拆分：终止成功 PARENT_KILLED / 失败 PARENT_KILL_FAILED。
  - 统一 stop 收尾 reconciliation：operator-stop 取消激活也走
    `_complete_interrupted_stop()`（去 ad-hoc `last_pid` 判定）；修复了
    record 已死导致的无限循环（record 更新需"与当前不同"才 continue）。
  - stale-group 清理返回 bool：失败 → STOPPED_ERROR（不再忽略返回值继续开发）。
- 既有 93 项全绿，合计 **96 passed**（`python -m pytest -q`，~40s）。

## 关键实现（第四轮）

- `lock.py::ParentLease.release()`：只 `os.close`，不 `LOCK_UN`——flock 绑定 OFD，
  对继承 FD 做 LOCK_UN 会连 DSH 的租约一起解；唯一解锁路径是最后一个持有副本的
  FD 关闭（DSH 死亡时内核自动释放）。
- `engine.py`：`Outcome.keep_parent`；`_finalize(keep_parent=...)`；所有 kill-failure
  分支（收养/超时/取消/stale/stop 收尾）→ PARENT_KILL_FAILED + STOPPED_ERROR +
  保留 current_parent；`_clean_recorded_group` 返回 bool；stop 取消统一走
  `_complete_interrupted_stop` reconciliation。

## 阻塞项

无。

## 分支 / 提交

- master 历史：3aabeef … bc5e860 (R1), d0825a1+ba5586f (R2), f96d946 (R3),
  11993ec (R4，本会话，已提交并推送 origin/master)。