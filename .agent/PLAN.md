# .agent/PLAN.md — M5 hardening（第 1+2+3 轮评审意见）

## 目标

按三轮评审意见完成 M5 hardening：

- 第一轮：收养孤儿期 `stop` 杀整个进程组；`STARTING_PARENT` 崩溃窗口用
  `launcher+process.json` 消除重复 Parent；收养期 timeout/wall-time 继续生效；
  `supervisor stop` 身份校验；文件名统一；`cmdline` 校验接入恢复；
  `PARENT_STARTING`/`PARENT_STARTED` 拆分。
- 第二轮：Parent lease（唯一性）+ process.json（身份发现）；整组终止（PGID
  确认）；收养 timeout/自退恢复语义；`STOPPING`；文档/依赖同步。
- 第三轮（本轮，crash consistency 收尾）：① `STOPPING` 在磁盘上 durable 保持
  （恢复时绝不落盘 BOOTING，二次崩溃不丢 stop intent）；② STOPPING + pid 未知 +
  lease held → stop 收尾像普通 reconciliation 一样等 record/lease，绝不留
  Parent 在后台；③ reconcile 等 lease 时收到 stop → 切入 STOPPING 而非
  PARENT_SPAWN_UNCONFIRMED；④ 终止失败（PGID 仍 alive）→ 显式失败
  STOPPED_ERROR，绝不写 STOPPED_OPERATOR；⑤ 按 PGID 杀组必须身份可验证
  （start_id 缺失/不符不杀）。

## 任务清单

| ID | 任务 | 状态 |
|---|---|---|
| H1–H8 | 第一轮：launcher/process.json、token/sid 字段、三态恢复+收养、cmdline/僵尸判定、事件拆分、stop 身份校验、命名/协议统一、7 项测试 | DONE |
| H9–H14 | 第二轮：Parent lease、PGID 整组终止、收养恢复语义（KillReason/RECOVER_*）、STOPPING、文档同步、8 项测试 | DONE |
| H15 | **durable STOPPING**：`_restore_or_init_runtime` 恢复 STOPPING 时磁盘保持
      STOPPING（不复位 BOOTING），直到真正 `STOPPED_OPERATOR` | DONE |
| H16 | **lease-aware stop 收尾**：`_complete_interrupted_stop` 按
      pid 已验证→杀组 / process.json 找回→杀组 / 无可信身份→看 lease（被占则等，
      绝不满状态收场）；reconcile 等 lease 遇 stop → 切入 STOPPING 不落 PSU | DONE |
| H17 | **终止失败必须失败收场**：`terminate_process_group`/`_terminate_group`
      返回 bool；PGID 仍 alive → `STOPPED_ERROR`(SUPERVISOR_INTERNAL_ERROR)；
      cancel 路径/超时路径都确认进程组消失 | DONE |
| H18 | **PGID 击杀身份可验证**：`_clean_recorded_group` 要求 start_id 非空且
      identity_matches；`_complete_interrupted_stop` 不按裸 pid 杀 | DONE |
| H19 | tests/test_stopping_consistency.py 6 项（durable STOPPING、wait-record-then-kill、
      stop cut-in 无 PSU、surviving-group→STOPPED_ERROR×2、identity-guarded kill） | DONE |
| H20 | 协议 §12/§13、delivery-report-r2 标注 historical、PLAN/STATE/FINAL_REPORT 更新 | DONE |

## 验收（可命令验证）

- `python -m pytest -q` → **93 passed**
- STOPPING 恢复：磁盘 runtime 保持 STOPPING；STOPPING+pid 未知+lease held →
  等 record（token 匹配）出现后杀组 → STOPPED_OPERATOR；等不到时绝不结束 stop
- reconcile 等 lease 中收到 stop → 切入 STOPPING，无 PARENT_SPAWN_UNCONFIRMED
- 终止失败（含取消路径进程组仍 alive）→ STOPPED_ERROR / SUPERVIOR_INTERNAL_ERROR
- 缺失/不符 start_id 的 PGID 不会被按裸 pid 误杀

## 出域

不做 M6/M7/M8（CI 轮询、GitHub review、token/cost）。M6 只落地了只读 Git 探测与
activation 前后快照，完整 M6 验收不在本轮范围。