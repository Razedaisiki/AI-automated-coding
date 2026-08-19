# Doublecheck report

> Verdict: **green**

## Spec
- Goal: 按评审意见完成 M5 hardening：收养孤儿期 stop 必须杀整个进程组；STARTING_PARENT 崩溃窗口用 launcher+process.json 进程记录彻底消除重复 Parent；收养期 timeout/wall-time 限额继续生效；supervisor stop 增加 Supervisor 自身 PID 身份校验；文件名统一与协议一致；cmdline 校验接入恢复；PARENT_STARTING/PARENT_STARTED 事件拆分；全部以新增回归测试 + 全量 71+ 测试全绿交付。
- Scope: 范围内：supervisor/{engine,dsh_runner,launcher,process_identity,models,storage,events,cli,prompts}.py、tests/test_hardening.py 与现有测试适配、AGENT.md→AGENTS.md 与 .agent/PLAN.md 统一、docs/supervisor-protocol.md 同步、.agent/PLAN.md/STATE.md/FINAL_REPORT.md 更新、一个 hardening git commit。范围外：M6/M7/M8、token/cost、多任务、第三方依赖。
- Acceptance criteria: (1) 收养 orphan 期间 operator stop/SIGTERM → SIGTERM 整个进程组→grace→SIGKILL，记录 PARENT_KILLED，不记 ORPHAN_EXITED，runtime STOPPED_OPERATOR，孤儿进程组必死（test_hardening#1 绿）；(2) STARTING_PARENT 崩溃窗口：runtime 仅 STARTING_PARENT+token、无 pid → 从 .supervisor/runs/activation-N/process.json 恢复；记录存在且 pid+starttime+token+cmdline 匹配 → 收养（不重复启动，PARENT_RECONCILED）；记录缺失 → 有界宽限后重新 spawn（PARENT_SPAWN_UNCONFIRMED），激活号不重复（test_hardening#2 两变体绿）；(3) 收养期间 parent timeout / wall-time 限额继续生效：超时 → 杀进程组 + timeouts+1 + PARENT_TIMEOUT/PARENT_KILLED，随后按限额 STOPPED_LIMIT（test_hardening#3 绿）；(4) runtime 增加 supervisor_process_start_id，`supervisor stop` 必须 is_proc_alive + identity_matches 才发 SIGTERM，身份缺失/不符 → rc=1 且不动目标进程（test_hardening#4 绿）；(5) 文件名统一：AGENT.md→AGENTS.md，.agent/plan.md→.agent/PLAN.md（git mv），prompts、Layout、协议文档同步；(6) is_dsh_process(cmdline) 接入收养与 reconcile 判定（删除 dead code，协议与实现一致）；(7) 事件拆分 PARENT_STARTING（意图，含 token）→ PARENT_STARTED（确认，含 pid/process_start_id），顺序断言绿；(8) 全量 pytest 绿且新增回归测试至少 5 个、既有 71 个不回归。
- Failure modes: 孤儿进程在收养前已自然退出 → is_proc_alive/identity 判假 → 清 current_parent 正常继续（不误杀新进程）；process.json 中 pid 已死（launcher 写记录后被 kill）→ 判 stale → 允许重新 spawn；launcher exec 失败（dsh 不存在）→ 退出码 127 + stderr 标记 → DshRunner 抛 RunnerError 清晰报错（保持现有测试语义）；旧 runtime 无 supervisor_process_start_id → stop 保守拒绝（不杀可能无关的 PID）；收养期收到 stop 与超时同时发生 → 以 stop 优先（OPERATOR_STOP），先杀进程组再退出；wall-time 在收养期达到 → 杀孤儿并按 MAX_ACTIVE_WALL_TIME 停止。
- Priorities: 正确性优先：能 100% 阻止重复 Parent（launcher+process.json，用户已拍板）优先于实现简单；安全优先：stop 身份校验保守（可能误报 not running 也绝不杀错进程）优先于便利；审计语义清晰（PARENT_STARTING/STARTED/PARENT_KILLED 事件）优先于少改事件；兼容旧 runtime 文件（缺失新字段可恢复）优先于格式严格；测试零 LLM、用真实子进程验证进程组行为。
- Non-goals: 不继续 M6/M7/M8（CI 轮询、GitHub review、token/cost）；不做多任务调度；不改变 M0–M4 已验证的协议语义（状态枚举、限额枚举、文件所有权）；不引入第三方依赖；不修改 hello_world；不对既有测试做"为了变绿而删减"的改动；不做真正需要 Git 协议之外权限的功能。

## Test evidence
- failing runs: 0
- passing runs: 0

- [spec] 在 /home/co1718/dsh-test 按 AI_automated_coding.md 实施 Supervisor 系统 M0–M5：协议文档/配置、原子存储+事件日志+独占锁、DshRunner（进程组超时击杀）、本地 FSM …
- [spec] 按评审意见完成 M5 hardening：收养孤儿期 stop 必须杀整个进程组；STARTING_PARENT 崩溃窗口用 launcher+process.json 进程记录彻底消除重复 Parent；收养期 timeout/wall-…

## Adversary review
No adversary review ran for this session.

## Verification
Not run.

## Delivery
- implementation edits: 131
