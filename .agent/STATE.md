# .agent/STATE.md — 当前执行状态

## 阶段

M5 hardening 完成，准备交付验证

## 完成情况

- hardening 7 项全部完成并自测通过。
- 新增 tests/test_hardening.py 7 项：收养+stop 杀孤儿、STARTING 记录 reconcile/重 spawn、事件时序、收养期超时限额、stop 身份校验（错身份/缺失身份 均拒绝）。
- 既有 71 项全绿，合计 **78 passed**。
- 全量 `python -m pytest -q` → 78 passed（~12s）。
- 静态检查：无 `shell=True`、`py_compile` 全过。
- 文件名：`AGENT.md → AGENTS.md`，`storage.agent_plan_path` 已改 `.agent/PLAN.md`，prompts 统一读取清单。

## 验证记录

- launcher: `sys.path` 修正 `parent.parent`（此前 `ModuleNotFoundError: supervisor` 导致 M2 失败）。
- `is_proc_alive` 增加僵尸（Z）判定；`terminate_process_group` 轮询式 grace。
- runtime 在 `_restore_or_init_runtime` 同步最新 `config.limits`（修复收养期超时限额不生效）。
- DshRunner 经 launcher；缺失可执行文件时通过 127 + stderr 标记抛 `RunnerError`（保持既有测试语义）。
- 协议文档 13 章重写，补充 process.json / 身份双重校验 / 三态恢复 / 事件拆分。

## 阻塞项

无（后续 M6/M7 属计划外）。

## 分支 / 提交

- master: 3aabeef (M0+M1), ecafa95 (M2), 44cbf55 (M3–M5), 7527a90, b360dac, 5f116b4
- 待提交 hardening: launcher + engine + models + cli + 协议 + hardening 测试
