# Delivery report — Supervisor M0–M5 (@ /home/co1718/dsh-test)

Tool verdict: **green**（doublecheck_report 自动聚合；本会话验证维度在工具侧未执行，
以本文件中的实测证据为准；会话内已做对抗式自查并修复。）

## Spec（摘要）

按 `AI_automated_coding.md` 实施 Supervisor：协议冻结 → 原子存储/事件/锁 → DshRunner
（进程组超时击杀）→ 本地 FSM 循环 → 限额/退避/超时 → 崩溃恢复与孤儿收养。全部用
Fake runner 测试，零 LLM；CLI 按文档工作；Python 3.8 兼容；不做 CI/Human/token（后续里程碑）。

## 红/绿测试时间线（本会话实测命令）

| 阶段 | 命令 | 结果 |
|---|---|---|
| M1 红 | `pytest tests/test_m1.py` | 25 failed（NotImplementedError，行为缺失） |
| M1 绿 | 同命令 | 25 passed |
| M2 红 | `pytest tests/test_m2.py` | 7 failed |
| M2 绿 | `pytest tests/test_m2.py tests/test_m1.py` | 32 passed |
| M3–M5 红 | `pytest tests/test_engine.py tests/test_cli.py tests/test_git_probe.py` | 35 failed |
| M3–M5 绿（渐进） | `pytest tests/test_engine.py`（修复 asyncio.Event 循环绑定/Limits 类型/激活号回退/resume 字段冲突等） | 21 passed |
| CLI 绿 | `pytest tests/test_cli.py` | 11 passed |
| 全量绿 | `pytest` | 68 passed（10.48s） |
| 对抗补充 | 新增 test_adversarial.py（外部杀 Parent、双 supervisor 锁拒绝、预算回归） | 3 passed |
| 最终全量 | `pytest` | **71 passed（11.45s）** |

## 对抗式自查记录

1. 发现：asyncio.Event 在 Python 3.8 于事件循环外构造会绑定已关闭的 loop → 惰性创建 + 停止标记（修复）。
2. 发现：运行时恢复后激活号可能回退（counters 落后于 current_parent.activation_id）→ 恢复时取 max（修复）。
3. 发现：WAIT_HUMAN 前活跃时长是否计入墙钟预算 —— 插桩核实已计入（0.411s 实测），非缺陷；保留回归测试。
4. 新增回归覆盖：外部 SIGKILL 存活 Parent → PARENT_CRASH+crash_restarts≥1+进程组清空；
   第二个 supervisor run 被锁拒绝 rc=2；预算计费。

## 验收对照（自动化证据）

- 杀 Parent（外部 SIGKILL）→ 恢复并计数：test_adversarial.py::TestKillParentExternally ✓
- 杀 Supervisor（kill -9）→ 重启收养孤儿、不重复启动、继续至完成：test_cli.py::TestCrashRecoveryCli ✓
- 超时 SIGTERM→SIGKILL 进程组：test_m2.py::TestDshRunnerTimeout ✓ + test_engine 06b ✓
- 双 Supervisor 锁拒绝：test_engine 21 + test_adversarial CLI rc=2 ✓
- 协议不解析 stdout、只读 Git 探测、无 shell=True：静态检查 + 代码审计 ✓
- 运行目录审计（prompt/stdout/stderr/result/git-before/git-after）：test_engine 01 + test_cli parent-once ✓

## 交付工件

- 代码：`supervisor/`（10 模块）+ `supervisor.toml` + `docs/supervisor-protocol.md` + `pyproject.toml`
- 测试：`tests/`（5 类共 71 项 + fixtures fake_dsh / fake runner）
- 文档：`.agent/PLAN.md`、`.agent/STATE.md`、`.agent/FINAL_REPORT.md`
- 提交：master 5 个 commit（3aabeef, ecafa95, 44cbf55, 7527a90, b360dac）

## 结论

M0–M5 交付完成，71 项测试全绿，验收场景全部自动化覆盖。剩余风险：
真实 headless profile 在当前只读根文件系统下无法启动（环境限制，非代码缺陷）；
CI/Human 属 M7/M8 后续里程碑。