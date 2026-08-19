# .agent/STATE.md — 当前执行状态

## 阶段

M5 hardening（第 1+2+3 轮评审）全部完成，全量测试绿，待提交推送。

## 完成情况

- 第一轮 hardening 7 项 + 第二轮 8 项回归测试已完成（详见前两版 STATE 记录）。
- 第三轮（本会话，crash consistency 收尾）新增 tests/test_stopping_consistency.py 6 项：
  - STOPPING 恢复后磁盘必须**继续保持 STOPPING**（不落盘 BOOTING）。
  - STOPPING + pid 未知 + lease held + record 迟到 → 等 record（token 匹配）杀组
    后才 STOPPED_OPERATOR，绝不留 Parent 在后台。
  - reconcile 等 lease 时收到 stop → 切入 STOPPING，不落 PARENT_SPAWN_UNCONFIRMED。
  - 终止失败（monkeypatch 返回 False / 取消路径进程组仍 alive）→ STOPPED_ERROR
    （SUPERVISOR_INTERNAL_ERROR），绝不写 STOPPED_OPERATOR。
  - `_clean_recorded_group(pid, None)` 不按裸 pid 杀组（身份不可验证）。
- 既有 87 项全绿，合计 **93 passed**（`python -m pytest -q`，~36s）。

## 关键实现（第三轮）

- `_restore_or_init_runtime`：恢复 STOPPING 时 `existing.status` 保持 STOPPING
  （仅更新 supervisor_pid/start_id、同步限额）——stop intent durable。
- `_complete_interrupted_stop` 重写：pid 已验证→杀组；pid 未知→轮询 process.json
  （token 匹配+身份可验证）→杀组；无可信身份→lease 空闲=完成 stop / 被占=继续等
  （绝不结束 stop）；杀组确认失败→STOPPED_ERROR。
- `_reconcile_starting_parent`：等 lease 期间收到 stop → 切入 STOPPING（不 PSU、
  不清 current_parent）；run_forever 在 adopt 后再次检查 STOPPING 完成收尾。
- `terminate_process_group` / `DshRunner._terminate_group` 返回 bool；
  `ParentResult.group_survived`；激活超时路径 / cancel 路径均确认进程组消失，
  失败 → `Outcome(stop_error)` → STOPPED_ERROR。
- `_clean_recorded_group` 要求 start_id 非空且 identity_matches。

## 阻塞项

无。

## 分支 / 提交

- master 历史：3aabeef … bc5e860 (R1), d0825a1 + ba5586f (R2)
- 本轮待提交：engine/dsh_runner/models + tests/test_stopping_consistency.py +
  协议 §12/§13 + delivery-report-r2 historical 标注 + PLAN/STATE/FINAL_REPORT。