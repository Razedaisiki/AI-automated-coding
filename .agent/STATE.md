# .agent/STATE.md — 当前执行状态

## 阶段

FINALIZE（实现完成，交付审查完成，生成交付报告）

## 完成情况

- M0–M5 全部完成并提交（5 个 milestone commit）。
- 测试：71 passed（M1:25, M2:7, M3-M5 engine:21, CLI:12, git probe:3, adversarial:3）。
  覆盖文档五十一 的 01–09、15–24 场景（10-14 的 CI 细节属 M7 范围）。
- 端到端验证：
  - `kill -9 Supervisor → 重启 → 收养孤儿 → 孤儿退出后继续至完成`（test_cli 端到端）。
  - 外部 SIGKILL 正在运行的 Parent 进程组 → 检测为 PARENT_CRASH + crash_restarts≥1 + 进程组清干净。
  - `supervisor stop` 发 SIGTERM → STOPPED_OPERATOR 且 Parent 进程组被清。
  - 第二个 `supervisor run` 被锁拒绝 rc=2。
  - 真实子进程超时 → SIGTERM→SIGKILL 进程组（engine+DshRunner+fake dsh）。
  - 进入 WAIT_HUMAN 前的活跃时长计入墙钟预算。

## 验证记录

- `python3 -m pytest` → 71 passed（11.45s）
- `python3 -m supervisor --help` → 正常
- `python3 -m supervisor run <repo-without-toml>` → rc=1, "error: config file not found: ..."（无 traceback）
- 静态检查：无 shell=True、无 os.system/eval；py_compile 全过
- Python 3.8.10 兼容（无 3.10+ 语法；tomllib→tomli 回退）

## 阻塞项

无

## 分支 / 提交

- master：3aabeef（M0+M1）、ecafa95（M2）、44cbf55（M3–M5）、7527a90（kill-9 e2e + 文档）、b360dac（审查补充测试）

## 环境备注

- `/` 只读：`~/.dsh` 不可写，真实 `dsh --profile headless` 在当前环境无法启动
  （EROFS）。DSH 可执行文件路径与 profile 均可配置，测试全部用 fake dsh 脚本；
  接入真实 headless 时需把 DSH_HOME 指向可写位置并为 headless profile 配置插件
  （文档五十四）。