# .agent/PLAN.md — M5 hardening（按评审意见）

## 目标

按评审“P0/P1 收养与崩溃窗口”完成 hardening：收养孤儿期 `stop` 必须杀整个进程组；
`STARTING_PARENT` 崩溃窗口用 `launcher+process.json` 消除重复 Parent；
收养期 timeout/wall-time 继续生效；`supervisor stop` 增加 Supervisor 身份校验；
文件名统一；`cmdline` 校验接入恢复；`PARENT_STARTING`/`PARENT_STARTED` 拆分；
5 个回归测试 + 71 个原有测试全绿。

## 任务清单

| ID | 任务 | 状态 |
|---|---|---|
| H1 | launcher 自写 process.json + DshRunner exec 链改造 | DONE |
| H2 | models: ParentInfo.token / RuntimeState.supervisor_process_start_id | DONE |
| H3 | engine: 三态恢复（NO/STARTING/RUNNING）+ record reconciliation + adoption 限额/超时 | DONE |
| H4 | process_identity: is_dsh_process / is_proc_alive(僵尸判定) / storage/plan 命名统一 | DONE |
| H5 | events: PARENT_STARTING/KILLED/RECONCILED/SPAWN_UNCONFIRMED/RECORD_STALE | DONE |
| H6 | cli: stop 身份校验（pid+start_id），init/status/events 不变 | DONE |
| H7 | prompts/AGENTS.md/.agent/PLAN.md 统一，协议文档 13 章重写 | DONE |
| H8 | tests/test_hardening.py 7 项 + 既有 71 项 全绿 | DONE |

## 验收（可命令验证）

- `python -m pytest -q` → 78 passed
- `supervisor run` 收养期 `stop` → 进程组必死、PARENT_KILLED、STOPPED_OPERATOR
- `STARTING_PARENT` + 有 record → 收养（无重复启动）；无 record → PARENT_SPAWN_UNCONFIRMED 后重 spawn
- 收养期 parent timeout / wall-time → 杀进程组、计数、STOPPED_LIMIT
- `supervisor stop` 对错误/缺失身份的 PID → 拒绝（rc=1，不动目标进程）

## 出域

不做 M6/M7/M8（CI 轮询、GitHub review、token/cost），不引入第三方依赖。