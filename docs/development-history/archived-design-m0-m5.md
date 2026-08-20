> **Historical artifact — archived from `AI_automated_coding.md` at Runtime Namespace Separation.**
> Not a live task file. See `docs/supervisor-protocol.md §0` for ownership.
> This was the M0–M5 initial design draft, kept for provenance only.

对。现在可以把目标收敛得非常清楚：

> **历史设计文档（archived design）**：本文档是 M0–M5 阶段的设计稿，保留作
> 追溯。当前规范性协议以 `docs/supervisor-protocol.md` 为准；文中出现的文件名
> 细节（如小写 `.agent/plan.md`）以现行代码/协议（`.agent/PLAN.md`）为准，
> 不再逐处改写。

> **Supervisor 是一个确定性的进程管理器。它不负责开发，只负责保证 Parent Agent 能持续、安全、可恢复地工作。**

而且你当前 DSH 的 `headless` 模式非常适合这个架构：它会创建一个新的持久化 Agent，把任务作为普通用户消息提交，等待这一轮完全结束，然后 flush Session、输出最终 assistant 文本并退出；它没有交互式后续输入接口。
正确启动方式就是在目标仓库目录里运行 `dsh --profile headless "<task>"`，调用目录会成为默认 workspace。

下面我会按**可以真正一步一步实施**的顺序设计，而不是一次性造一个很大的系统。

---

# 一、先冻结 Supervisor 的职责边界

Supervisor **负责**：

```text
启动 Parent
监控 Parent PID
检测 Parent 正常/异常退出
超时终止
异常重启
最大 Parent activation 次数
最大 crash restart 次数
整个任务最大 wall time
状态持久化
Supervisor 自身崩溃恢复
读取 .agent/state.json
观察 Git 的机械状态
等待 GitHub CI
等待 Human Gate
决定是否启动下一轮 Parent
决定整个自动化什么时候停止
记录完整事件日志
```

Supervisor **不负责**：

```text
需求分析
任务拆分
决定修改哪些代码
运行项目 test / lint / build
解释测试失败
修改代码
决定 bug 怎么修
code review
doublecheck
commit 内容设计
判断实现是否正确
```

这些全部属于：

```text
Parent Agent
    ↓
subagents
```

可以把核心原则写成一句：

```text
Supervisor 决定 WHEN。
Parent Agent 决定 HOW。
```

---

# 二、V1 技术选型

我建议 Supervisor 第一版用：

```text
Python 3.11+
asyncio
dataclasses
json
tomllib
pathlib
subprocess / asyncio.subprocess
signal
fcntl
```

**第一版尽量零第三方依赖。**

原因不是 Python 更“高级”，而是 Supervisor 本身需要的能力非常朴素：

```text
启动进程
等进程
杀进程
读 JSON
写 JSON
读配置
计时
轮询
处理 signal
```

Python 标准库已经足够。

先不要引入：

```text
Celery
Redis
Temporal
数据库
Docker orchestration
消息队列
FastAPI
```

那些以后多任务调度时再考虑。

第一版是：

> **一个 repo + 一个 task + 一个 Supervisor process + 多轮 DSH Parent activation。**

---

# 三、推荐目录

最终项目里可以这样：

```text
repo/
│
├── TASK.md
├── AGENTS.md
│
├── .agent/
│   ├── state.json
│   └── plan.md
│
├── .supervisor/
│   ├── runtime.json
│   ├── events.jsonl
│   ├── lock
│   ├── runs/
│   │   ├── activation-000001/
│   │   │   ├── prompt.txt
│   │   │   ├── stdout.log
│   │   │   ├── stderr.log
│   │   │   └── result.json
│   │   └── activation-000002/
│   │
│   └── inbox/
│       └── ...
│
├── supervisor.toml
│
└── supervisor/
    ├── __init__.py
    ├── __main__.py
    ├── cli.py
    ├── config.py
    ├── models.py
    ├── storage.py
    ├── events.py
    ├── lock.py
    ├── dsh_runner.py
    ├── git_probe.py
    ├── github_ci.py
    ├── prompts.py
    ├── engine.py
    └── process_identity.py
```

这里最重要的是：

```text
.agent/*
```

属于 **Parent Agent 的世界**。

而：

```text
.supervisor/*
```

属于 **Supervisor 的世界**。

不要让两个角色同时写同一个状态文件。

---

# 四、两个完全不同的状态文件

这是整个设计最重要的部分之一。

## `.agent/state.json`

由 **Parent Agent 写**。

Supervisor只读。

它描述的是：

> “开发任务做到哪了？”

第一版 schema 可以非常简单：

```json
{
  "schema_version": 1,
  "task_id": "TASK-001",

  "status": "RUNNING",

  "checkpoint_seq": 7,

  "checkpoint": {
    "phase": "implementation",
    "summary": "Implementing refresh token rotation"
  },

  "git": {
    "branch": "feat/refresh-token",
    "head": "abc1234",
    "pushed_head": null
  },

  "ci": {
    "sha": null,
    "status": "NONE"
  },

  "next_action": "Continue implementation",

  "blocker": null,

  "updated_at": "2026-08-18T06:00:00Z"
}
```

Supervisor真正关心的字段其实很少。

`status` 第一版只允许：

```text
RUNNING
WAIT_CI
WAIT_HUMAN
COMPLETED
BLOCKED
```

不要搞：

```text
TESTING
FIXING
REVIEWING
WRITING_CODE
CLIPPY_FAILED
```

这些都是 Parent 内部的语义。

Supervisor不应该理解。

---

# 五、`.supervisor/runtime.json`

这个只允许 **Supervisor 写**。

它描述的是：

> “自动化系统运行到哪了？”

例如：

```json
{
  "schema_version": 1,

  "status": "RUNNING_PARENT",

  "task_started_at": "2026-08-18T05:00:00Z",

  "current_parent": {
    "activation_id": 6,
    "pid": 412514,
    "process_start_id": "linux-proc-start-123456",
    "started_at": "2026-08-18T06:02:00Z",
    "reason": "RECOVER_AFTER_CRASH"
  },

  "counters": {
    "parent_activations": 6,
    "crash_restarts": 1,
    "clean_restarts": 1,
    "timeouts": 0,
    "ci_wakeups": 2
  },

  "limits": {
    "max_parent_activations": 20,
    "max_crash_restarts": 5,
    "max_clean_restarts": 10,
    "max_ci_wakeups": 10,
    "max_wall_seconds": 14400,
    "parent_timeout_seconds": 2700
  },

  "last_agent_checkpoint_seq": 7,

  "stop_reason": null
}
```

这两个状态文件以后一定不要混。

```text
.agent/state.json
        ↓
开发状态

.supervisor/runtime.json
        ↓
运行状态
```

---

# 六、Supervisor 自己的状态机应该非常小

不要把开发 workflow 编进 Supervisor。

Supervisor 状态只需要这些：

```text
BOOTING

STARTING_PARENT

RUNNING_PARENT

RESTART_BACKOFF

WAITING_CI

WAITING_HUMAN

STOPPED_SUCCESS

STOPPED_BLOCKED

STOPPED_LIMIT

STOPPED_ERROR

STOPPED_OPERATOR
```

注意这里**没有**：

```text
IMPLEMENT
TEST
REVIEW
FIX
```

非常重要。

---

# 七、核心状态机

整体逻辑：

```text
                     ┌──────────┐
                     │  BOOTING │
                     └────┬─────┘
                          ↓
                  inspect durable state
                          │
             ┌────────────┼─────────────┐
             │            │             │
          RUNNING       WAIT_CI      WAIT_HUMAN
             │            │             │
             ↓            ↓             ↓
       START_PARENT    WAIT CI      WAIT HUMAN
             │
             ↓
       RUNNING_PARENT
             │
     ┌───────┼───────────────┐
     │       │               │
   exit0   exit1          timeout
     │       │               │
     ↓       ↓               ↓
 inspect   restart         terminate
 state     policy          restart
     │
     ├── RUNNING ─────────→ restart Parent
     │
     ├── WAIT_CI ─────────→ WAITING_CI
     │
     ├── WAIT_HUMAN ──────→ WAITING_HUMAN
     │
     ├── COMPLETED ───────→ STOP_SUCCESS
     │
     └── BLOCKED ─────────→ STOP_BLOCKED
```

---

# 八、最重要的一条：不要把 DSH exit code 当任务状态

DSH headless 的退出码语义是：

```text
0 = 这一 Agent turn 正常结束
1 = runner/error
```

它不是：

```text
0 = 软件开发成功
1 = 软件开发失败
```

官方 headless runner 是等待 Agent 完全停稳后输出结果；正常 `turn/end` 完成会退出 0，否则退出 1。

所以：

```text
Parent exit code = 0
.agent/state = RUNNING
```

含义应该是：

> Parent 正常结束了这一轮，但任务还没有到稳定终止状态。

Supervisor应该再次启动 Parent。

可以给事件：

```text
PARENT_CLEAN_EXIT_WITH_RUNNING_STATE
```

---

# 九、状态优先级

我建议明确写进代码。

最高优先：

```text
1. Supervisor hard limits
2. Operator stop
3. fresh .agent/state.json
4. Parent process result
5. previous runtime state
```

举例。

如果 Parent：

```text
写：
status = WAIT_CI

然后退出码 = 1
```

只要这个 `state.json`：

```text
JSON 合法
checkpoint_seq 增加了
updated_at 在本轮 activation 之后
CI SHA 合理
```

那 Supervisor仍然可以进入：

```text
WAITING_CI
```

同时记录：

```text
parent_exit_code=1
state_transition=WAIT_CI
anomaly=true
```

因为可能是：

```text
Parent 已经 push
↓
已经 checkpoint WAIT_CI
↓
DSH 最后一刻发生非关键退出错误
```

没必要因此重新让第二个 Parent乱入。

---

# 十、`checkpoint_seq` 很重要

Parent每次更新：

```text
.agent/state.json
```

都：

```text
checkpoint_seq += 1
```

例如：

```text
1
2
3
4
5
```

Supervisor记录：

```text
last_agent_checkpoint_seq
```

这样就能判断：

```text
这是不是本轮新产生的状态？
```

否则可能读到昨天残留的：

```text
WAIT_CI
```

误以为当前 Parent 刚写的。

---

# 十一、Parent 状态必须原子写

否则 Supervisor可能刚好读到：

```text
{
   "status":
```

这种半写文件。

Parent规则应该要求：

```text
先写 .agent/state.json.tmp
↓
flush
↓
rename
↓
.agent/state.json
```

Unix rename 是原子的。

如果以后觉得让 Agent 每次自己写太不可靠，可以再提供一个：

```text
supervisor checkpoint ...
```

小工具负责 schema 校验 + atomic replace。

但这是 V1.5。

第一版可以先靠提示词规定。

---

# 十二、Supervisor 自己写 JSON 也必须原子

例如 Python：

```python
def atomic_write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")

    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp, path)
```

`runtime.json` 永远这么写。

---

# 十三、事件日志不要覆盖

`.supervisor/events.jsonl`

只追加：

```json
{"ts":"...","event":"SUPERVISOR_STARTED"}
{"ts":"...","event":"PARENT_STARTED","activation":1,"pid":1234}
{"ts":"...","event":"PARENT_EXITED","activation":1,"exit_code":0}
{"ts":"...","event":"AGENT_STATE","status":"WAIT_CI","checkpoint_seq":4}
{"ts":"...","event":"WAIT_CI","sha":"abc123"}
{"ts":"...","event":"CI_FAILED","sha":"abc123"}
{"ts":"...","event":"PARENT_STARTED","activation":2,"reason":"CI_FAILED"}
```

以后你会非常感谢这个文件。

例如：

> 为什么这个任务唤醒了 8 次 Parent？

直接：

```bash
grep PARENT_STARTED .supervisor/events.jsonl
```

就知道。

---

# 十四、每轮 Parent 都必须单独建运行目录

例如：

```text
.supervisor/runs/activation-000007/
```

里面保存：

```text
prompt.txt
stdout.log
stderr.log
result.json
git-before.json
git-after.json
```

`result.json`：

```json
{
  "activation_id": 7,
  "reason": "CI_FAILED",

  "started_at": "...",
  "ended_at": "...",

  "duration_seconds": 382.4,

  "exit_code": 0,
  "timed_out": false,

  "agent_state_before": {
    "status": "WAIT_CI",
    "checkpoint_seq": 6
  },

  "agent_state_after": {
    "status": "RUNNING",
    "checkpoint_seq": 8
  }
}
```

这就是完整 audit trail。

---

# 十五、DshRunner 是第一个真正要写的核心组件

接口：

```python
@dataclass
class ParentResult:
    activation_id: int
    exit_code: int | None
    timed_out: bool
    started_at: str
    ended_at: str
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path


class DshRunner:
    async def run(
        self,
        *,
        repo: Path,
        prompt: str,
        activation_id: int,
        timeout_seconds: int,
    ) -> ParentResult:
        ...
```

底层必须是：

```text
cwd = repo
```

命令：

```bash
dsh --profile headless "<prompt>"
```

这是你当前版本官方明确支持的 one-shot 入口。

---

# 十六、不要使用 `shell=True`

一定：

```python
await asyncio.create_subprocess_exec(
    "dsh",
    "--profile",
    "headless",
    prompt,
    cwd=repo,
)
```

不要：

```python
create_subprocess_shell(...)
```

因为 TASK / prompt 很容易包含：

```text
'
"
$
;
```

shell quoting 会制造麻烦，甚至安全问题。

---

# 十七、DSH Parent 要独立 process group

非常重要。

创建：

```python
proc = await asyncio.create_subprocess_exec(
    "dsh",
    "--profile",
    "headless",
    prompt,
    cwd=repo,
    stdout=stdout_file,
    stderr=stderr_file,
    start_new_session=True,
)
```

`start_new_session=True` 会给它单独 process group。

因为 Parent 下面可能还有：

```text
DSH
├─ subagents
├─ bash commands
├─ tests
└─ other subprocesses
```

timeout 时不要只杀 DSH 主 PID。

应该：

```text
SIGTERM process group
↓
等待 10 秒
↓
仍不退出
↓
SIGKILL process group
```

例如：

```python
os.killpg(proc.pid, signal.SIGTERM)
```

---

# 十八、Parent timeout 逻辑

例如：

```toml
[limits]

parent_timeout_seconds = 2700
```

45 分钟。

实现：

```text
启动 Parent
↓
最多等待 45 min
↓
timeout
↓
SIGTERM group
↓
10 sec grace
↓
SIGKILL group
↓
timeouts += 1
↓
写 TIMEOUT event
↓
检查 limits
↓
重新启动 Parent
```

下一轮 Prompt：

```text
SUPERVISOR EVENT: RECOVER_AFTER_PARENT_TIMEOUT
```

---

# 十九、Parent Prompt 不应该包含开发逻辑

Supervisor只发送**控制协议**。

Bootstrap：

```text
SUPERVISOR EVENT: INITIAL_START

You are the Parent Agent responsible for the software-development task
in this repository.

Read:
- TASK.md
- AGENTS.md
- .agent/plan.md if it exists
- .agent/state.json if it exists

Inspect the actual repository state.

You own all software-engineering decisions and execution.

The Supervisor only manages your process lifecycle and does not decide
how implementation should be performed.

Continue autonomously until you reach a durable boundary.

Before ending this activation, atomically update .agent/state.json.

Valid durable statuses are:

RUNNING
WAIT_CI
WAIT_HUMAN
COMPLETED
BLOCKED

If more development work remains and you choose to end this activation,
write RUNNING.

If remote CI must complete before useful work can continue, write WAIT_CI
and record the exact commit SHA.

If human action is required, write WAIT_HUMAN.

Only write COMPLETED when the development task is actually complete.

Only write BLOCKED when further autonomous progress is genuinely impossible.
```

注意这里没有告诉它：

```text
怎么 test
怎么 review
怎么修代码
```

这些都应该来自：

```text
AGENTS.md
```

---

# 二十、Crash Recovery Prompt

Parent异常退出：

```text
SUPERVISOR EVENT: RECOVER_AFTER_PARENT_CRASH

The previous Parent Agent terminated unexpectedly.

Do not restart the development task from scratch.

Read:
- TASK.md
- AGENTS.md
- .agent/plan.md
- .agent/state.json

Inspect the actual repository state, including:
- current branch
- HEAD
- git status
- working-tree changes
- existing commits

Persisted agent state may lag behind the repository because the previous
process terminated unexpectedly.

Reconcile the documented state with the actual repository.

Continue the existing task autonomously.

Before ending, atomically checkpoint .agent/state.json.
```

---

# 二十一、Supervisor 不需要理解 `plan.md`

`.agent/plan.md` 可以非常复杂：

```text
T1...
T2...
T3...
```

Supervisor只知道：

```text
exists?
mtime?
size?
```

甚至这些都不是必须。

它只是告诉新的 Parent：

```text
read it
```

不要解析：

```text
哪个 task READY
哪个 task DONE
```

这是 Parent 的责任。

---

# 二十二、Git Probe 只做机械观察

Supervisor可以读取：

```text
当前 branch
HEAD SHA
working tree 是否 dirty
remote 是否存在
remote URL
```

例如：

```bash
git rev-parse --show-toplevel
git symbolic-ref --short HEAD
git rev-parse HEAD
git status --porcelain=v2 --branch
git remote get-url origin
```

禁止 Supervisor：

```text
git add
git commit
git checkout
git reset
git merge
git rebase
git push
```

这些都是 Parent 行为。

`git_probe.py` 输出：

```python
@dataclass
class GitSnapshot:
    branch: str | None
    head: str | None
    dirty: bool
    has_remote: bool
    remote_url: str | None
```

Supervisor不看 diff 内容。

---

# 二十三、为什么要在 Parent 前后各取一次 Git snapshot

例如：

```text
activation 4 before:

HEAD = abc123
dirty = false

activation 4 crashes

after:

HEAD = abc123
dirty = true
```

Supervisor不用知道改了什么。

只需要在恢复事件里记录：

```text
Previous activation left uncommitted working-tree changes.
```

新 Parent 自己检查。

---

# 二十四、第一次启动 Supervisor 自己时怎么恢复

假设 Supervisor 本身崩了。

启动时：

```text
BOOT
↓
获得独占 lock
↓
读取 runtime.json
↓
读取 .agent/state.json
↓
检查 recorded PID
↓
检查 Git snapshot
↓
决定：
    Parent 还活着？
    Parent 已死？
    WAIT_CI？
    WAIT_HUMAN？
    COMPLETED？
```

---

# 二十五、一定要做 Supervisor lock

否则：

```text
terminal 1:
supervisor run

terminal 2:
supervisor run
```

可能会：

```text
启动两个 Parent
↓
两个 Agent 同时修改 repo
```

灾难。

Linux V1 可以用：

```python
fcntl.flock()
```

锁：

```text
.supervisor/lock
```

第二个 Supervisor发现锁已持有：

```text
Supervisor already running for this repository.
```

直接退出。

---

# 二十六、Supervisor 崩溃后 Parent 可能仍然活着

这是比较高级但很重要的问题。

因为 Parent 是单独进程。

例如：

```text
Supervisor PID 1000
Parent PID 1010

Supervisor 💥

Parent 1010
仍在继续开发
```

新 Supervisor启动时不能马上再启动一个 Parent。

所以 runtime 保存：

```json
{
  "pid": 1010,
  "process_start_id": "..."
}
```

然后检查：

```text
PID 是否存在？
command line 是否还是 DSH？
process start identity 是否一致？
```

Linux 可以从：

```text
/proc/<pid>/
```

判断。

不能只：

```python
os.kill(pid, 0)
```

因为 PID 可能已经被系统复用。

---

# 二十七、第一版可以先简化 orphan adoption

MVP 可以先这样：

```text
Supervisor重启
↓
发现 recorded PID 仍活着
↓
不启动第二个 Parent
↓
轮询这个 PID
↓
等它退出
↓
读取 .agent/state.json
```

虽然它已经不是当前 Python 进程的 child，不能 `waitpid()`，但可以：

```text
轮询 /proc/<pid>
```

等消失。

后面再完善 process identity。

---

# 二十八、Restart Policy

至少分三种。

### 1. Process Crash

```text
exit code != 0
state 仍 RUNNING / 无新 checkpoint
```

计数：

```text
crash_restarts += 1
```

### 2. Clean Exit But Not Finished

```text
exit code == 0
state == RUNNING
```

不是 crash。

计：

```text
clean_restarts += 1
```

### 3. Timeout

```text
timed_out == true
```

计：

```text
timeouts += 1
```

---

# 二十九、Backoff

不要 crash 后瞬间无限重启。

例如：

```text
2s
5s
15s
30s
60s
60s...
```

实现：

```python
BACKOFF = [2, 5, 15, 30, 60]
```

选择：

```python
delay = BACKOFF[min(restart_count, len(BACKOFF) - 1)]
```

---

# 三十、硬停止预算

第一版已经可以真正实现：

```toml
[limits]

max_parent_activations = 20

max_crash_restarts = 5

max_clean_restarts = 10

max_ci_wakeups = 10

max_timeouts = 3

max_wall_seconds = 14400

parent_timeout_seconds = 2700
```

Supervisor每次循环最先：

```python
enforce_limits()
```

一旦触发：

```text
STOPPED_LIMIT
```

并且：

```json
{
  "stop_reason": "MAX_PARENT_ACTIVATIONS"
}
```

---

# 三十一、Wall time 是整个任务的

不是单次 Parent。

比如：

```text
task_started_at = 10:00

max_wall_time = 4h
```

即使：

```text
Parent 运行
CI 等待
Parent 重启
Human wait
```

是否都算？

我建议默认：

```text
Parent runtime + CI wait = 算
WAIT_HUMAN = 不算
```

因为人可能一天后才 review。

所以最好未来拆：

```text
active_wall_time
human_wait_time
```

V1 可以简单一点：

```text
WAIT_HUMAN 时暂停 budget clock
```

---

# 三十二、CI 是 Supervisor 最适合接管的外部等待

Parent工作到：

```text
commit
push
```

之后写：

```json
{
  "status": "WAIT_CI",

  "ci": {
    "sha": "abc1234",
    "status": "PENDING"
  }
}
```

然后结束。

Supervisor：

```text
Parent exit
↓
state = WAIT_CI
↓
不启动 Parent
↓
poll GitHub
```

---

# 三十三、CI 必须绑定 SHA

绝对不要：

```text
看 branch 最新 CI
```

一定：

```text
expected_sha = abc123
```

否则：

```text
Parent push commit A
↓
CI A running
↓
又有人 push commit B
↓
Supervisor误把 CI A success
当成 B success
```

---

# 三十四、CI Adapter

接口：

```python
class CiProvider(Protocol):

    async def get_status(
        self,
        repo: Path,
        sha: str,
    ) -> CiResult:
        ...
```

统一返回：

```text
NOT_FOUND
PENDING
SUCCESS
FAILURE
CANCELLED
ERROR
```

Supervisor完全不理解：

```text
测试为什么失败
```

---

# 三十五、CI failure 时怎么处理

Supervisor：

```text
CI FAILURE
↓
保存 CI metadata / logs
↓
启动新 Parent
```

不要自己分析。

可以把信息存：

```text
.supervisor/inbox/ci-abc123/
    summary.json
    failed-check.log
```

然后 prompt：

```text
SUPERVISOR EVENT: CI_FAILED

The external CI run for commit abc123 failed.

Supervisor-collected CI material is available under:

.supervisor/inbox/ci-abc123/

Resume the existing development task.

You are responsible for determining the cause and deciding how to fix it.
```

这样避免把几十 KB CI log 全塞 prompt。

---

# 三十六、CI SUCCESS

也是：

```text
WAIT_CI
↓
CI SUCCESS
↓
ci_wakeups += 1
↓
Parent activation +1
```

Prompt：

```text
SUPERVISOR EVENT: CI_SUCCEEDED

External CI for commit abc123 succeeded.

Resume the existing task and continue from the persisted state.
```

Parent决定：

```text
是不是创建 PR
是不是等待 Human
是不是还有事
```

Supervisor不知道。

---

# 三十七、CI discovery grace period

Push 以后 CI 不一定立刻出现在 GitHub API。

所以：

```toml
[ci]

poll_seconds = 30

discovery_grace_seconds = 180

max_wait_seconds = 7200
```

前 3 分钟：

```text
NOT_FOUND
```

不能马上当失败。

等。

---

# 三十八、WAIT_HUMAN

第一版不需要 GitHub Review API。

只实现：

```text
state = WAIT_HUMAN
↓
Supervisor停止 Parent
↓
进入 WAITING_HUMAN
```

然后 CLI：

```bash
python -m supervisor resume \
  --event HUMAN_APPROVED
```

或者：

```bash
python -m supervisor resume \
  --event HUMAN_CHANGES_REQUESTED
```

V2 再自动接 GitHub PR review。

---

# 三十九、Supervisor CLI

建议第一版就是：

```text
supervisor init

supervisor run

supervisor status

supervisor stop

supervisor resume

supervisor events
```

例如：

```bash
python -m supervisor init .
```

生成：

```text
.supervisor/
supervisor.toml
```

运行：

```bash
python -m supervisor run .
```

状态：

```bash
python -m supervisor status .
```

---

# 四十、`supervisor.toml`

可以这样：

```toml
version = 1

[dsh]
executable = "dsh"
profile = "headless"

[limits]
max_parent_activations = 20
max_crash_restarts = 5
max_clean_restarts = 10
max_timeouts = 3
max_ci_wakeups = 10

parent_timeout_seconds = 2700
terminate_grace_seconds = 10

max_active_wall_seconds = 14400

[restart]
backoff_seconds = [2, 5, 15, 30, 60]

[ci]
enabled = false
provider = "github"
poll_seconds = 30
discovery_grace_seconds = 180
max_wait_seconds = 7200

[human]
pause_active_wall_clock = true
```

CI 先：

```toml
enabled = false
```

等本地 lifecycle 稳了再开。

---

# 四十一、Engine 主循环

最终核心不会很多：

```python
async def run_forever(self):

    self.acquire_lock()

    self.restore_runtime()

    while True:

        self.enforce_limits()

        agent_state = self.read_agent_state()

        if agent_state is None:
            await self.ensure_parent("INITIAL_START")
            continue

        match agent_state.status:

            case "COMPLETED":
                self.stop_success()
                return

            case "BLOCKED":
                self.stop_blocked()
                return

            case "WAIT_HUMAN":
                await self.wait_human()
                continue

            case "WAIT_CI":
                await self.wait_ci(agent_state)
                continue

            case "RUNNING":
                await self.ensure_parent("CONTINUE")
```

真正复杂的是：

```text
ensure_parent()
```

---

# 四十二、`ensure_parent()`

逻辑：

```text
当前 Parent 还活着？
        │
     yes│no
        │
        │    检查 limit
        │        ↓
        │    生成 activation ID
        │        ↓
        │    capture git-before
        │        ↓
        │    build prompt
        │        ↓
        │    spawn DSH
        │        ↓
        │    persist runtime
        │
   monitor      monitor
```

退出：

```text
capture git-after
↓
read fresh agent state
↓
write result.json
↓
dispatch next
```

---

# 四十三、Supervisor 不能依赖 Parent stdout 做控制

这是一个重要设计。

不要：

```python
if "WAIT_CI" in stdout:
```

绝对不要。

stdout只是：

```text
人类日志
debug
审计
```

唯一机器协议：

```text
.agent/state.json
```

---

# 四十四、Parent 最后一条 assistant 文本仍然保存

DSH headless 官方会把该运行区间最后一条非空 assistant 文本输出到 stdout。

所以保存：

```text
activation-X/stdout.log
```

很有价值。

但是不解析它。

---

# 四十五、Supervisor 自己被 SIGTERM 时

处理：

```text
SIGINT
SIGTERM
```

建议：

```text
收到 stop
↓
runtime = STOPPING
↓
SIGTERM Parent process group
↓
grace
↓
SIGKILL
↓
runtime = STOPPED_OPERATOR
↓
释放 lock
```

不要悄悄把 Parent 留后台。

除非以后明确实现：

```text
detach mode
```

V1 不需要。

---

# 四十六、安全限制

Supervisor只允许启动几类外部程序：

```text
dsh
git（read only）
gh（read only CI）
```

不允许：

```text
bash -c arbitrary-string
```

即使以后有配置，也不要直接：

```python
subprocess.run(config["command"], shell=True)
```

---

# 四十七、CI logs 要视为 untrusted data

GitHub CI 输出可能包含：

```text
恶意文本
prompt injection
巨量日志
ANSI escapes
binary noise
secret-like data
```

Supervisor不解释。

只：

```text
保存
限制大小
交给 Parent
```

例如：

```text
max_ci_log_bytes = 2 MB
```

大于：

```text
截断 + 保存 metadata
```

---

# 四十八、Token / Cost 先不要做假的 hard limit

目前我们已经确认 DSH 主发行包里存在：

```text
@deepseek-ai/dsh-token-meter
```



但 headless 当前公开接口文档只描述任务、stdout/stderr 和退出行为，没有暴露 token/cost machine-readable contract。

所以 V1：

```text
max tokens = 暂不实现
max cost   = 暂不实现
```

不要：

```text
根据字符串长度估 token
× 模型价格
```

然后把它当安全预算。

那是假安全。

但接口提前留：

```python
class UsageProvider:

    async def usage_for_activation(...):
        ...
```

现在：

```python
NullUsageProvider
```

以后研究 token-meter 后替换。

---

# 四十九、真正第一阶段不要碰 GitHub

我建议实施分 8 个 Milestone。

---

## M0 — Freeze Protocol

先写文档，不写 engine。

完成：

```text
docs/supervisor-protocol.md

supervisor.toml

.agent/state.schema.example.json
```

定义：

```text
Agent statuses
Supervisor statuses
stop reasons
checkpoint_seq
ownership rules
restart rules
```

验收：

```text
你能回答：
谁写哪个文件？
exit 0 + RUNNING 怎么办？
exit 1 + WAIT_CI 怎么办？
timeout 怎么办？
什么时候停止？
```

---

# M1 — Storage + Lock

实现：

```text
config.py
models.py
storage.py
events.py
lock.py
```

功能：

```text
读取 TOML
读取 agent state
atomic runtime write
JSONL event append
exclusive supervisor lock
```

不要启动 DSH。

测试：

```text
两个 supervisor 同时启动
→ 第二个失败

runtime 写到一半进程死
→ 原 runtime 不损坏

invalid state.json
→ 明确错误，不崩成 traceback
```

---

# M2 — DshRunner

只实现：

```text
启动 headless
日志
timeout
kill process group
exit result
```

暂时不做自动循环。

CLI：

```bash
python -m supervisor parent-once .
```

内部：

```text
dsh --profile headless "..."
```

验收：

```text
能启动 hello_world Parent

stdout.log 有最终回复

exit_code 正确

timeout 能 SIGTERM

必要时能 SIGKILL

运行目录被保存
```

---

# M3 — Local Supervisor Loop

现在实现：

```text
RUNNING
COMPLETED
BLOCKED
```

还不做 CI/Human。

规则：

```text
no state
→ initial Parent

RUNNING
→ Parent

COMPLETED
→ stop success

BLOCKED
→ stop blocked
```

再加：

```text
Parent exit 0 + RUNNING
→ start next Parent

Parent exit 1 + RUNNING
→ restart
```

这一步跑通，你已经拥有真正的 Supervisor MVP。

---

# M4 — Crash / Timeout / Limits

加：

```text
max activations
max crash restart
max clean restart
max timeout
max wall time
backoff
```

验收模拟：

```text
Agent每次 exit1
→ 第 5 次后停止

Agent一直不退出
→ timeout kill

Agent一直 RUNNING + exit0
→ activation limit 停止
```

---

# M5 — Supervisor Restart Recovery

实现：

```text
runtime restore
PID discovery
orphan Parent detection
lock recovery
```

测试：

```text
Supervisor运行 Parent
↓
kill -9 Supervisor
↓
Parent继续
↓
重新启动 Supervisor
↓
不能启动第二个 Parent
↓
等原 Parent结束
↓
继续
```

这一关非常重要。

通过后才算真正：

> “进程管理器”。

---

# M6 — Git Snapshot

实现只读：

```text
branch
HEAD
dirty
remote
```

在：

```text
activation before
activation after
Supervisor startup
```

采样。

Parent crash recovery prompt 中加入：

```text
Supervisor observed repository changes since activation start.
Inspect actual repository state.
```

不解析代码。

---

# M7 — WAIT_CI

再实现：

```text
WAIT_CI
CI poll
exact SHA
CI success
CI failure
CI timeout
CI wakeup Parent
```

先让 CI Adapter 可 fake。

不要一开始就依赖真实 GitHub。

先做：

```python
FakeCiProvider
```

测试 FSM。

之后才接：

```text
GitHub
```

---

# M8 — WAIT_HUMAN

实现：

```text
WAIT_HUMAN
```

Supervisor停止 Agent activation。

CLI：

```text
resume --event ...
```

后续再接 GitHub review automation。

---

# 五十、Supervisor 自己必须有 Fake DSH Runner

这一点非常关键。

你不能每次测 Supervisor 都真的烧 Agent token。

定义接口：

```python
class ParentRunner(Protocol):

    async def run(...) -> ParentResult:
        ...
```

生产：

```text
DshRunner
```

测试：

```text
FakeDshRunner
```

Fake 可以：

```text
第 1 次：
写 state=RUNNING
exit 1

第 2 次：
写 state=WAIT_CI
exit 0
```

这样整个 Supervisor FSM：

```text
毫秒级测试
零 token
```

---

# 五十一、必须覆盖的测试场景

正式接真实 DSH 之前，至少覆盖：

```text
01 fresh task → COMPLETED

02 fresh task → BLOCKED

03 clean exit + RUNNING → new activation

04 crash + RUNNING → restart

05 repeated crash → STOP_LIMIT

06 timeout → kill → restart

07 repeated timeout → STOP_LIMIT

08 max activations → STOP_LIMIT

09 max wall time → STOP_LIMIT

10 WAIT_CI → no Parent starts

11 WAIT_CI pending → continue waiting

12 CI success → wake Parent

13 CI failure → wake Parent

14 wrong CI SHA → do not accept result

15 WAIT_HUMAN → no Parent starts

16 invalid agent state

17 missing agent state after crash

18 stale checkpoint_seq

19 Supervisor restart with dead Parent

20 Supervisor restart with live Parent

21 two Supervisors → second rejected

22 operator SIGTERM

23 runtime atomic write

24 event log survives restart
```

---

# 五十二、Stop Reason 最好固定枚举

例如：

```text
TASK_COMPLETED

TASK_BLOCKED

MAX_PARENT_ACTIVATIONS

MAX_CRASH_RESTARTS

MAX_CLEAN_RESTARTS

MAX_TIMEOUTS

MAX_ACTIVE_WALL_TIME

CI_WAIT_TIMEOUT

INVALID_AGENT_STATE

SUPERVISOR_INTERNAL_ERROR

OPERATOR_STOP
```

不要只保存：

```text
"something went wrong"
```

---

# 五十三、完整一次真实运行最终会是这样

```text
$ supervisor run .

Supervisor
│
├─ activation #1
│      ↓
│   DSH Parent
│      ↓
│   plan
│   subagents
│   coding
│   testing
│      ↓
│   .agent/state = WAIT_CI
│      ↓
│   exit 0
│
├─ WAIT CI
│
├─ CI failed
│
├─ activation #2
│      ↓
│   DSH Parent
│      ↓
│   inspect CI
│   fix
│   test
│   push
│      ↓
│   WAIT_CI
│      ↓
│   exit
│
├─ CI success
│
├─ activation #3
│      ↓
│   DSH Parent
│      ↓
│   PR
│      ↓
│   WAIT_HUMAN
│
├─ WAIT HUMAN
│
├─ review changes requested
│
├─ activation #4
│      ...
│
└─ COMPLETED
```

Supervisor全程都不知道：

```text
代码是什么
bug是什么
测试是什么
PR改了什么
```

它只知道：

```text
process
checkpoint
limits
external wait
termination
```

这正是你要的架构。

---

# 五十四、还有一个当前环境要注意的点

你现在的：

```text
context
doublecheck
permission-rules
```

是装在：

```text
web profile
```

里面的。

而 Supervisor将使用：

```text
headless profile
```

这是**另一个独立 profile**。DSH 的 profile 本身是独立目录、独立 manifest / plugin dependencies，`web` 和 `headless` 都有自己的模板。 

所以将来如果 Parent headless 也需要：

```text
doublecheck
permission-rules
context
```

需要**单独给 headless profile 配置**。

但我建议现在先不要做。

先完成：

```text
M0
M1
M2
M3
```

确认：

```text
Supervisor → headless Parent → checkpoint → Parent exit → Supervisor继续
```

跑通。

然后再一个一个给 headless 增加插件。

---

# 五十五、我建议你的实际开发顺序

如果从现在开始写，我会严格按照：

```text
第 1 个 commit
protocol + config + models

第 2 个 commit
atomic storage + events + lock

第 3 个 commit
DshRunner + parent-once

第 4 个 commit
Supervisor local FSM

第 5 个 commit
timeout + restart + limits

第 6 个 commit
Supervisor crash recovery

第 7 个 commit
Git snapshot

第 8 个 commit
Fake CI provider

第 9 个 commit
GitHub CI provider

第 10 个 commit
Human gate

第 11 个 commit
hardening + docs

之后
token/cost V2
```

**先不要同时做 GitHub、Token、Human Review。**

先把“Parent 崩了 Supervisor 能不能救回来”做到非常可靠，这才是这个项目真正的核心。

你第一阶段的成功标准应该只有一句：

> **我可以随时杀掉一个 Parent，甚至杀掉 Supervisor 本身，然后重新启动 Supervisor，它仍然能够在不破坏仓库、不启动重复 Agent 的情况下，从 `.agent/state.json + .agent/plan.md + Git state` 恢复开发任务。**

做到这一点以后，再接 CI，其余都只是扩展。
