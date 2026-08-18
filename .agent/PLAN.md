# .agent/PLAN.md — Autonomous Development Supervisor (M0–M5)

## 任务目标

按 `AI_automated_coding.md` 实施 Supervisor（确定性进程管理器），完成 M0–M5：

> Supervisor 决定 WHEN；Parent Agent 决定 HOW。

## 验收标准

- 保存状态、事件、锁全部原子/只追加；并发第二个 Supervisor 被锁拒绝。
- DshRunner 能启动 `dsh --profile headless`（可配置 executable，测试用 fake dsh），
  超时先 SIGTERM 进程组、宽限期后 SIGKILL。
- 本地循环：无状态→INITIAL_START；RUNNING→继续；COMPLETED→成功停止；
  BLOCKED→阻塞停止；exit0+RUNNING→下一轮；exit!=0+RUNNING→崩溃重启。
- 限额：max activations / crash / clean / timeouts / active wall time → STOPPED_LIMIT。
- 崩溃恢复：kill -9 Supervisor 后重启，收养存活孤儿，绝不启动第二个 Parent。

## 任务清单

| ID | 描述 | 状态 |
|---|---|---|
| M0 | 冻结协议：docs/supervisor-protocol.md、supervisor.toml、state schema 示例 | DONE |
| M1 | config / models / storage / events / lock | DONE |
| M2 | DshRunner（进程组/超时/SIGTERM→SIGKILL）+ parent-once | DONE |
| M3 | 本地 Supervisor 主循环（RUNNING/COMPLETED/BLOCKED + 重启策略） | DONE |
| M4 | 限额/退避/超时/墙钟预算 → STOPPED_LIMIT | DONE |
| M5 | 崩溃恢复：runtime 恢复、进程身份、orphan 收养、operator SIGTERM | DONE |
| 测试 | 68 tests green（FakeParentRunner + fake dsh，零 LLM） | DONE |
| 文档 | 更新 STATE.md / FINAL_REPORT.md | IN_PROGRESS |

## 后续（本轮不做，文档明确属于后续里程碑）

M7 CI 轮询（GitHub provider，先 fake）、M8 人工评审接入 GitHub、V2 token/cost 计量、
多任务调度。`[ci] enabled = false` 保持默认。