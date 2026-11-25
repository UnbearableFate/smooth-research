# Smooth Research 配置说明

配置分四处：项目配置文件 `smooth-research.yaml`、约束文档 `docs/constraints.md`、几个环境变量、命令行选项。程序读取的只有 `smooth-research.yaml`。约束文档写给人和 agent 看，CLI 不解析它。

## 1. 项目配置 `smooth-research.yaml`

它放在研究项目的根目录，由 `rl init` 生成：开头几行注释，后面是全部默认值。默认值在 `src/smooth_research/project.py` 的 `DEFAULT_CONFIG`：

```yaml
version: 1
models:
  coordinator: [claude:claude-opus-5-5@high, codex:gpt-6-astra@high]
  scientist: [claude:claude-opus-5-5@high, codex:gpt-6-astra@high]
  researcher: [claude:claude-sonnet-5-5@medium, codex:gpt-6-astra@medium]
  reviewer: [codex:gpt-6-astra@medium, claude:claude-opus-5-5@medium]
  advisor: [codex:gpt-6-astra@high, claude:claude-fable-5-2@high]
agents:
  args: {claude: [--permission-mode, auto], codex: [], pi: []}
  beat_seconds: 30
  clear_idle_minutes: 60
  clear_context_tokens: 120000
  user_quiet_minutes: 3
  heartbeat_minutes: 5
  page_minutes: 15
  deadlines: {default: 240}
limits:
  trial_minutes: 30
  fast_lane: {}
  scientists: 2
  envelope_lines: [0.8, 1.0, 1.5]
usage:
  switch_left_percent: 10
compute:
  coordinator: local
  default: local
  repository: none
  tests: local
  resources:
    local: {kind: local}
```

### 加载规则

配置里的任何问题都不会让命令失败。CLI 用能用的值继续运行，并给出警告，说明它实际用了什么。

- 文件里的值和默认值深度合并：字典逐键覆盖，列表整体替换。比如 `models.reviewer` 只写一个条目，默认的两个就都没了。三个映射整体替换，不和默认值合并：`compute.resources`（文件里没写 `local` 时就没有默认的本机资源）、`agents.deadlines` 和 `limits.fast_lane`。
- 警告出现在两个地方：普通输出末尾的 `warning:` 行（`--json` 时不打印），以及用户自己在项目里开的 Claude Code 或 Codex 会话开始时的 hook 提示。
- 下面这些情况会警告：
  - 文件解析不了（YAML 写错，或者顶层不是键值对）：沿用上一次成功读取的设置，直到文件改好。这份设置由 CLI 自动保存在 `research/state/config.last-read.json`（`--read-only` 时不更新）；从没读成功过时用默认值；
  - `version` 不是 `1`：按版本 1 读取；
  - 不认识的键：没有作用。旧版本的键（`agents.watch_minutes`、`agents.stall_minutes`、`agents.wait_check_minutes`、`usage.coordinator_host_left_percent`、`advice`、`context`、`manuscript` 等）现在都属于这一类，可以删掉；
  - 某一节（如 `models`、`agents`）不是键值对：这一节全用默认值；
  - `models.<角色>` 不是列表（写成一个字符串时当作只有一个条目的列表，不警告）：用默认列表；某个条目格式不对：跳过这一条；
  - `agents.beat_seconds`、`clear_idle_minutes`、`clear_context_tokens`、`user_quiet_minutes`、`heartbeat_minutes`、`page_minutes` 不是正数：用默认值；
  - `agents.deadlines.<类型>` 不是正的分钟数：这个类型改用 `default`，`default` 也不能用时用 240；
  - `agents.args.<宿主>` 不是参数列表：用默认参数（写成一个字符串时按 shell 的规则拆开，不警告）；
  - `limits.trial_minutes`、`limits.scientists` 不是正数：用默认值；`limits.fast_lane.<单位>` 不是数：这一项不算。`limits.envelope_lines` 不是正数列表时用默认值，但不警告；
  - `usage.switch_left_percent` 不是 0 到 100 的数：用 10；
  - `compute.repository` 不是 `github-private` 或 `none`：按 `none`；`compute.tests` 不是 `local` 或 `job`：按 `local`；`compute.coordinator` 不是 `local` 或 `login`：按 `login`；
  - 计算资源有问题（见下面的“计算资源”）：不认识的键没有作用；`kind`、`connect`、`jobs` 的值不认识时，这个资源不能用（不回退到 ssh 或 shell）；`code` 的值不认识时按 `git`；远程资源缺 `host` 或 `workspace`、`workspace` 不是绝对路径、名字含有不允许的字符、`connect: local` 却写了 `jobs: shell` 或不是 `code: archive` 时，这个资源也不能用；`compute.default` 不是能用的资源时，用第一个能用的。

### 目录（固定）

目录不能配置，所有项目都一样。路径都相对于项目根目录。

| 位置 | 内容 |
|---|---|
| `src/` | 实验代码。`rl init` 先把项目目录建成 uv 项目（见第 4 节），代码包在 `src/<包名>/`。init 在 `src/` 放一个忽略 `__pycache__/` 和 `*.py[cod]` 的 `.gitignore` |
| `docs/` | 只放给人读的文档：`plan.md`（`rl init --plan` 导入的计划，封存）、`constraints.md`（约束文档，见第 2 节）、`research.html`（研究页，`docs/.gitignore` 让它不进 git）、`explanations/`（用户要求的解释）、`glossary.md`（术语表，研究页里也显示）。`explanations/*.md` 和 `glossary.md` 由协调者用 `rl docs <路径> --message ..` 提交到主线 |
| `research/` | 记录和工作目录：`state/`（数据库 `research.lattice`、写锁 `write.lock`、监视程序的心跳 `watch.json` 和锁 `watch.lock`、`config.last-read.json`、升级时的备份 `backups/`）、`documents/objects/`（封存的文档）、`snapshots/`（`rl export` 的默认输出位置）、`literature/`（文献检索的记录）、`artifacts/<attempt>/`（本机 run 的输出）、`work/T-x/`（任务的工作文档，在任务的 worktree 里写、随分支合并）。`research/.gitignore` 让 `state/` 和 `artifacts/` 不进 git |
| 任务的分支和 worktree | 任务被认领时，CLI 从主线建分支 `sr/T-x` 和 worktree `<仓库目录>-worktrees/T-x`（放在仓库目录旁边；项目是仓库里的一个子目录时，会话在 worktree 里的同一个子目录工作）。之后再认领就复用它们。主线只在 CLI 接受任务时移动：先在任务分支上做一个合并主线的 merge commit，在它上面跑任务的测试，通过后主 checkout 快进到这个提交 |

导入的计划不能换：再导入不同的计划会报 CONFLICT，判断依据是已经封存的计划。新的方向是一个新的 tree 任务（`rl task create --kind tree --reason ..`）。

项目必须是至少有一次提交的 git 仓库，任务分支才能建；项目本身不能放在别的仓库的 linked worktree 里。

### 模型

五个角色各有一个模型列表：`models.coordinator`、`models.scientist`、`models.researcher`、`models.reviewer`、`models.advisor`。每一项的格式是 `<入口>:<模型>[@effort]`：

- 入口 `claude`、`codex`、`pi` 是在 herdr 里启动的交互式 Claude Code、Codex 或 pi 会话；`pi` 的模型写成 `<provider>/<模型>`，比如 `pi:openai/gpt-6-astra@high`。
- 入口 `manual` 仍能解析，但监视程序开会话时跳过它。
- `@effort` 传给 Claude 的 `--effort`、Codex 的 `model_reasoning_effort` 或 pi 的 `--thinking`。

监视程序给一个会话选模型时，按顺序取第一个可用的条目：不是 `manual`，命令（`claude`、`codex`、`pi`）在 PATH 上，所在宿主的订阅额度不低（见“订阅额度”）。所有条目都在额度低的宿主上时，仍用第一个命令可用的条目。一个都没有时不开会话，这一拍的输出说明原因。会话打开时用的条目记在会话记录上，之后改配置只影响新开的会话。rl 自己不调用任何模型。

### 会话怎样启动

一个会话是一个角色做一件事，名字是 `<角色>-<对象>`：`coordinator`、`scientist-T-0002`、`researcher-T-0003`、`reviewer-RV-0004`、`advisor-AD-0001`。协调者的会话由 `rl resume` 打开，其他会话由监视程序在某个接收者有待办事项、又没有打开的会话时打开（见下面的“监视程序”）。打开时：

1. scientist 会话先认领它的对象（开放的 hypothesis、tree 或 phase 任务），researcher 会话认领协调者建的开放 prep 任务；认领时建任务的分支和 worktree。认领失败（别人先拿了，或者前置任务变了）时这一拍不开，下一拍再看。
2. CLI 在当前 herdr workspace（`HERDR_WORKSPACE_ID`）里建一个不抢焦点的 tab。scientist 和 researcher 在任务的 worktree 里工作，其他角色在项目根目录。tab 的环境里设 `SMOOTH_RESEARCH_PROJECT`、`SMOOTH_RESEARCH_RL`、`SMOOTH_RESEARCH_SESSION`，工作目录不是项目根目录时再设 `SMOOTH_RESEARCH_CHECKOUT`。herdr 的 agent 名字和 tab 标签是“项目目录名的前几个字母 + 项目路径的短摘要 + 小写的会话名”，最长 32 个字符，所以两个项目的同名会话不会混在一起。
3. 用 `herdr agent start` 启动 agent，最多等 90 秒。CLI 自己加的参数只有下面这些，然后追加 `agents.args.<宿主>`：

| 宿主 | CLI 加的参数 |
|---|---|
| Claude Code | `--model`、`--session-id`（rl 生成的 UUID）、`-n <会话名>`、`--effort`（条目有 effort 时）、`--settings` 里一个运行 `rl usage statusline` 的状态栏（见“订阅额度”）；reviewer 另加 `--disallowedTools Edit,Write,NotebookEdit` |
| Codex | `-m`、`-c model_reasoning_effort="<effort>"`（条目有 effort 时）、`-c notice.hide_rate_limit_model_nudge=true`（额度快用完时不弹换模型的对话框）；advisor 另加 `--search` |
| pi | `--model`、`--session-id`（rl 生成的 UUID）、`--name <会话名>`、`--thinking`（条目有 effort 时） |

   不加任何绕过沙箱、审批或权限的参数。沙箱、权限方式和 MCP 设置由用户写在 `agents.args` 或宿主自己的配置里。
4. 宿主问是否信任文件夹时，如果问的正是 CLI 开会话的目录（任务的 worktree，或者用户启动协调者时已经信任的项目根目录），CLI 替协调者选“信任”（Claude Code：“Yes, I trust this folder”；Codex：“Trust and continue”），并记一条 `session.trusted` 事件。问的是别的目录，或者是别的对话框时，CLI 不回答，把会话记为需要决定（见下面的“对话框”）。
5. Codex 还显示 “Waiting for startup”（还在启动 MCP 服务）时，CLI 等它消失再发第一条提示。herdr 认不出的 Codex 对话框（“Hooks need review”，以及 `/new` 之后问新对话在哪里运行的问题）也按对话框处理。
6. 会话 id：Claude 和 pi 用 rl 给的 UUID；Codex 的 id 取自 herdr，herdr 还没报时 CLI 输入 `/status`（不调用模型），从 `Session:` 一行读出。引用屏幕时，CLI 去掉显示账号和 ChatGPT workspace 的两行。
7. 第一条提示是一行，以 `[smooth-research]` 开头：告诉会话它是谁、先运行 `rl pack` 并按它打印的内容工作、`rl inbox` 列出等它处理的事，后面附最多 3 条待办事项。会话从 `rl pack` 读到角色卡、它的任务或审查轮、待办事项和材料。

启动失败时，CLI 关掉刚建的 tab，会话记录结束，记一条 `session.failed` 事件。

### 对话框

会话停在对话框时（启动时的上述情况，或者监视程序看到 herdr 报 `blocked`），CLI 把屏幕末尾记在会话记录上，算出一条 `session-needs-decision` 待办事项：别的会话的对话框交给协调者，协调者自己的对话框交给用户。会话停在对话框期间，监视程序不往里输入任何东西；对话框消失后，记录自动清掉。

- `rl agent dialog <名字>` 显示会话屏幕的末尾（最多 1500 个字符），谁都可以看。
- `rl agent answer <名字> <选项> --reason ..` 选择一个选项并按回车。选项可以写编号，也可以写选项文字的开头；按屏幕上最下面匹配的那一行认（对话框在对话内容下面，内容里也可能有 `2. …` 这样的行）。CLI 每按一次方向键都重新读屏幕，等选中行移动后再决定下一步；对话框一消失就不再按键。选中后清掉记录，记一条 `session.answered` 事件，写明选项和理由。
- 别的会话的对话框只有协调者能回答；协调者自己的对话框由用户在它的 pane 里回答（也可以用这个命令）。

### 监视程序

监视程序是一个循环（`rl watch run`），在自己的 herdr tab（标签 `rl watch`）里运行。`rl resume` 在它没在运行时启动它（`rl watch start` 也可以单独启动），再打开协调者的 pane，然后打印项目状态。`rl resume` 需要 herdr，而且要在 herdr 的 pane 里运行（`HERDR_ENV=1`）。`rl watch once` 立刻走一拍（监视程序自己正在走一拍时跳过）。同一个项目只运行一个监视程序：每一拍持有 `research/state/watch.lock`。

| 键 | 默认值 | 含义 |
|---|---|---|
| `agents.args.claude` | `["--permission-mode", "auto"]` | 追加给 Claude Code 会话的参数。三个宿主都可以写成列表，也可以写成一个字符串（按 shell 的规则拆开） |
| `agents.args.codex` | `[]` | 追加给 Codex 会话的参数，比如沙箱和审批方式 `["-s", "workspace-write"]` |
| `agents.args.pi` | `[]` | 追加给 pi 会话的参数 |
| `agents.beat_seconds` | `30` | 两拍之间隔几秒 |
| `agents.clear_idle_minutes` | `60` | 会话空闲超过这么久、上下文又超过 `clear_context_tokens` 时，唤醒它之前先清空对话（prompt 缓存已经过期） |
| `agents.clear_context_tokens` | `120000` | 见上一行。比较的是会话记录里的上下文 token 数；目前没有代码写入这个数，所以清空实际上不会发生 |
| `agents.user_quiet_minutes` | `3` | 用户在协调者的 pane 里说过话之后，这么久之内监视程序不往协调者的 pane 里输入 |
| `agents.heartbeat_minutes` | `5` | 心跳超过这么久（且至少 2 × `beat_seconds` + 60 秒）算作监视程序停了 |
| `agents.page_minutes` | `15` | 研究页至少每隔这么久重画一次；记录变了时每一拍都重画 |
| `agents.deadlines` | `{default: 240}` | 一条待办事项最多等几分钟，超过就升级（见下面）。键是事项的类型，即 `rl inbox` 每条第一行的名字，比如 `hypothesis-task`、`prep-task`、`review-request`、`job-ended`、`decision-request`；没写的类型用 `default` |

每一拍按顺序做这些事：

1. 完成合并测试作业已经结束的接受（`compute.tests: job` 时，任务在 merging 状态等它的合并测试作业；作业的结束已经记下、接受却还没完成时，在这里补上）。
2. 从记录算出全部待办事项（`src/smooth_research/inbox.py` 的规则表）。没有存起来的消息：一条事项在对应的状态变了以后就消失，不需要确认。`rl inbox [--for <名字>]` 显示同一份列表。
3. 关掉工作已经结束的会话：它的任务已接受或取消，它的审查轮已通过或关闭，或者它的对象记录不存在了。关会话就是关掉它的 tab，记录结束，记 `session.closed`。
4. 对每个有事项的接收者（先协调者，再其他会话）：
   - 没有打开的会话：协调者跳过（它的 pane 由 `rl resume` 打开）；其他角色在下面两个上限之内打开一个（见上面的“会话怎样启动”）。scientist 在 hypothesis 任务上：同一阶段同时最多开阶段的 scientist 上限个（阶段建立时取 `--scientists`，没写时取 `limits.scientists`）。researcher：同一个 hypothesis 任务下的任务同时最多开它的 `--parallel` 个（默认 10）。
   - 有会话、但 herdr 里已经没有它的 agent（退出了，或者 tab 被关掉了）：会话记录结束；下一拍还有事项时再开一个新会话。
   - agent 在对话框（`blocked`）：记下需要决定，不输入。
   - agent 在工作，或者状态未知：不输入。
   - agent 空闲：所在宿主额度低、又不是协调者时，关掉会话，下一拍在别的条目上重开。否则把还没告诉过它的事项，以及告诉过它、但已经超过期限的事项，每条一行输入进去（一次最多 4 条，多的写 “+N more: rl inbox”），结尾是 “handle these, then end your turn.”。协调者的 pane 在用户最近 `user_quiet_minutes` 分钟里说过话时不输入（协调者的 pane 有动静、而 rl 最近两拍没给它发过提示，就算用户在说话）。空闲超过 `clear_idle_minutes`、上下文又大的会话先输入 `/clear`，再告诉它重新读 pack。
5. 没有事项的会话也看一次，记下它是否停在对话框。
6. 记录变了（`data_version` 变了）或者距上次超过 `page_minutes` 时重画 `docs/research.html`，然后写心跳 `research/state/watch.json`。

心跳比 `heartbeat_minutes`（至少 2 × `beat_seconds` + 60 秒）更旧时，算作监视程序停了：除了 `rl watch`、`rl hook` 和 `rl usage`，每条 rl 命令都会警告 “the watcher is not running: run `rl resume`”，`rl status` 也会显示。

输入的提示通过 `herdr agent prompt` 送出，不等 agent 反应。送不出去（停在对话框、没有 agent）的事项不算告诉过，下一拍再送。

#### 升级的阶梯

事项的等待时间从它的记录最后一次变化算起；监视程序自己的通知和会话的开、关、清空、信任、回答事件不算变化，所以提醒不会让期限重新开始。等待超过 `agents.deadlines` 里这个类型的期限后，每过一个期限上一级：

1. 第 1 个期限：监视程序再告诉一次持有者（上面第 4 步里“超过期限的事项”）。
2. 第 2 个期限：交给建这个任务的会话（一条 `timeout` 事项）。
3. 第 3 个期限：交给协调者。
4. 第 4 个期限：交给用户。

和接收者相同的一级跳过，任务没有记录建它的会话时也跳过那一级。协调者自己的事项在第 2 个期限直接交给用户。还在等会话名额的事项（`rl inbox` 里标 untimed）、阶段报告和 `timeout` 事项本身不升级。

### 限额

| 键 | 默认值 | 含义 |
|---|---|---|
| `limits.trial_minutes` | `30` | 试运行（`rl run register --trial`，用于 smoke、计时、测试作业）的 `--minutes` 上限。试运行不需要审查，也从不算证据 |
| `limits.fast_lane` | `{}` | 按单位写的估算上限，比如 `{MIG·h: 0.5}`。一个 execute 任务的提交没有被通过的审查覆盖时，满足下面全部条件仍可以派发（快速通道）：没有未关闭的阻塞问题；实验已经封存，manifest 和封存时一样；这个实验更早的任务有通过的审查，之后受保护的代码没变；任务的估算（`--estimate "0.2 MIG·h"`）的单位在这里，数值不超过上限。空映射表示没有快速通道 |
| `limits.scientists` | `2` | 阶段同时在工作的 scientist 默认上限。`rl phase create` 时写进阶段记录，之后改配置不影响已有的阶段；不属于任何阶段的 hypothesis 任务共用这个上限 |
| `limits.envelope_lines` | `[0.8, 1.0, 1.5]` | hypothesis 任务的计算包络（`--envelope "about 4 MIG·h"`）的几条线。已记录的用量越过任何一条线时，持有它的 scientist 得到一条 `usage-line` 事项（调整计划或发决定请求）；越过最后一条线时协调者也得到一条。之后有新的决定或包络改了，事项消失。用量按单位分开计算，不同单位不相加 |

### 订阅额度

Claude Code 和 Codex 一般用订阅，有 5 小时和每周两个窗口。

| 键 | 默认值 | 含义 |
|---|---|---|
| `usage.switch_left_percent` | `10` | 某个窗口只剩这么多（百分比）或更少时，这个宿主算作额度低 |

- 读数从哪里来。Codex 自己把窗口写进会话日志（`$CODEX_HOME/sessions/*/*/*/rollout-*.jsonl` 里的 `token_count` 事件）。CLI 比较最后写入的 5 个日志各自的最后一条读数，取事件时间最新的那条。Claude Code 只把额度交给状态栏命令：CLI 启动的 Claude 会话的状态栏运行 `rl usage statusline`，它把读数存进 `$SMOOTH_RESEARCH_HOME/usage/claude.json`（读数没变时一分钟最多写一次），并显示模型和各窗口还剩多少。pi 不报告窗口。
- 超过 15 分钟的读数算过期，不让宿主算作额度低；没有读数也一样。重置时间已经过了的窗口按没用过计算。
- 宿主额度低时：新会话跳过这个宿主上的条目，用角色列表里的下一个；这个宿主上已经开着的会话（协调者除外）在空闲时被关掉，下一拍在别的条目上重开，新会话从 pack 读回工作，没有交接文档。协调者不换模型。

### 计算资源

`compute` 一节写实验在哪里跑：本机，以及远程主机或集群。每个资源写清楚怎样连上去、用什么启动作业、远程的工作目录，以及代码怎样过去。rl 不连接任何资源，不复制文件，也不启动作业：它只读配置，检查本地能看到的东西，打印步骤，由 agent 用 Bash、ssh、pi-env-mcp 和提交作业的 MCP 或 skill 执行。唯一的例外是 git 这一侧：对 `code: git` 的资源，`rl compute sync` 在本地推送当前分支，必要时先建 remote（见下面的“同步”）；主机上的 clone 仍然是打印出来、由 agent 执行的步骤。预算和队列限制不写在这里，写在约束文档里（第 2 节）。

协调者在超级计算机登录节点上时，设 `compute.coordinator: login`。默认的 `local` 是普通的本机工作流；`login` 不让本机 checkout 或 `jobs: shell` 的资源用于执行，登记 run 时必须写 `--resource`。这是明确的设置，CLI 不按机器名猜测，也不替代站点工具的 allocation 检查。

协调者已经在登录节点、代码和结果目录由计算节点共享时：

```yaml
compute:
  coordinator: login
  default: cluster
  repository: none
  tests: job
  resources:
    cluster:
      kind: remote
      connect: local
      jobs: mcp:cluster-jobs
      workspace: /shared/user/my-project
      code: archive
```

`kind: remote` 表示实验在另一个执行环境里运行；`connect: local` 表示协调者直接访问共享文件系统，不需要 SSH、上传或下载。共享目录必须在登录节点和计算节点上有相同的绝对路径。`tests: job` 让任务的测试也作为作业运行（见下表）。

作业 MCP 经 `connect` 访问，不另开一条连接：

- `connect: pi-env-mcp:<服务名>`：pi-env-mcp 在主机上运行作业服务器（`pi-env-mcp mcp add <服务名> --host <主机> -- <命令>`），用的是 pi-env-mcp 已有的那条连接。宿主只注册 pi-env-mcp。作业工具是那个 pi-env-mcp 服务的 `<前缀>__<工具>`，前缀是作业服务器名里字母、数字、`_` 以外的字符换成 `_`（`miyabi-pbs` → `miyabi_pbs`；在 Claude Code 里是 `mcp__miyabi-g__miyabi_pbs__job_submit`）。不要再用 `ssh … serve` 单独注册它。
- `connect: ssh`：在宿主里注册，经 ssh 在主机上启动。
- `connect: local`：在登录节点的宿主里直接注册。

启动程序、队列和账户放在宿主、pi-env-mcp 或研究项目的配置里，插件只引用服务名。一份 pi-env-mcp 配置可以服务多个研究项目：作业服务器需要项目目录时，把资源的 `workspace` 作为它的项目参数传入。

协调者在工作站、需要远程传输时，用 `coordinator: local`，资源写 `host: cluster-login`、`connect: ssh`（或 `pi-env-mcp:<服务名>`）；作业 MCP 经同一个连接访问，工作目录单独配置。

| 键 | 默认值 | 含义 |
|---|---|---|
| `compute.coordinator` | `local` | `local`：普通本机工作流。`login`：协调者在登录节点，所有工作负载（包括测试、smoke、构建和分析）通过配置的作业 MCP 或 skill；不让 checkout 和 `jobs: shell` 的资源用于执行，登记必须带 `--resource`。不认识的值警告并按 `login` 处理 |
| `compute.default` | `local` | `rl compute steps` 不写资源时用的资源，也是 pack 里显示的默认资源。`rl run register` 不自动采用它：`local` 模式下可以不写 `--resource`（不记录资源），`login` 模式拒绝 |
| `compute.repository` | `none` | `code: git` 的资源缺 git remote 时，`rl compute sync` 怎么办：`github-private` 用 `gh repo create --private` 建一个仓库再推送；`none` 不建，提示问用户代码放到哪里 |
| `compute.tests` | `local` | `local`：`rl task test` 在任务的 worktree 里直接运行实验的语义测试或 prep 任务的完成检查，交付 prep 任务和接受时的合并测试也在本地跑。`job`：这些测试作为短作业运行（`rl run register --task T-x --purpose design-test --minutes N`，run 记下当时的测试命令和提交），结果用 `rl task test T-x --record pass|fail --attempt <ATT>` 记下（记的是那个作业跑的命令，之后改了命令要重跑）；接受时任务停在 merging，等合并测试的作业（`--purpose merge-test`）结束，由监视程序完成接受 |
| `compute.resources` | `{local: {kind: local}}` | 资源名到设置的映射，整体替换默认值。名字以字母或数字开头，只能用字母、数字、`.`、`_`、`-`。`login` 模式下没有能用的资源时，不提供执行步骤，也不回退到本机 |
| `<资源>.kind` | 有 `host` 或 `connect: local` 时为 `remote`，否则 `local` | `local` 使用调用者的 checkout，输出到 `research/artifacts/<attempt>/`；`remote` 在别的执行环境运行一个提交的快照，包括共享目录上的集群作业 |
| `<资源>.host` | 无 | ssh 或 pi-env-mcp 的连接目标；`connect: local` 不需要 |
| `<资源>.connect` | `ssh` | `ssh`、`pi-env-mcp`、`pi-env-mcp:<服务名>` 或 `local`。`local` 要求作业 MCP 或 skill 和 `code: archive`，不会启动本机工作负载 |
| `<资源>.jobs` | `shell` | `shell`、`mcp:<服务名>` 或 `skill:<名字>`。`coordinator: login` 和 `connect: local` 都不允许 `shell` |
| `<资源>.workspace` | 无，`remote` 必填 | 远端的绝对路径，里面有 `snapshots/<commit>/`、`runs/<attempt>/`，code 为 git 时还有 `repo.git`。一个研究项目一个 workspace |
| `<资源>.code` | `connect: local` 时 `archive`，否则 `git` | `git`：推送后由主机 fetch；`archive`：从调用者的提交用 `git archive` 导出，上传到远端，或直接在共享文件系统上生成只读快照。`connect: local` 只支持 `archive` |
| `<资源>.git_remote` | `origin` | code 为 git 时本地推送用的 remote |
| `<资源>.git_url` | 本地 `git_remote` 的 URL | code 为 git 时主机 clone 用的 URL，主机要有读权限 |
| `<资源>.notes` | 无 | 给 agent 看的说明；不能代替 `compute.coordinator` |

- 登录节点只做控制工作。编辑、研究记录、有限的检查和小的源码快照可以在登录侧进行；实验程序、测试、构建、分析都通过执行器到计算 allocation。作业 MCP 不可用时报告缺少工具，不在本地回退。插件不是 shell 沙箱，也不自己验证或创建 allocation。
- 共享目录不走网络。`rl compute steps` 打印本地 `git archive` 和只读快照的步骤，源码取自 attempt 登记的那个提交。结果直接从共享目录导入，manifest 里 artifact 的 `path` 用共享目录的绝对路径（相对路径按研究项目根目录解释）。
- 远程只跑快照。远程的 run 只执行一个完整提交的只读快照 `<workspace>/snapshots/<commit>/`。哪个分支、哪个 worktree 上的提交都可以，分支之后再动也不影响已经排队的作业。同一个提交只同步一次，重复执行步骤是安全的。
- `rl compute [show] [--commit <sha>]` 列出各资源和本地检查：`ssh` 在不在 PATH 上、git remote 在不在，加 `--commit` 时还看这个提交是否已经推送（按最后一次 push 或 fetch，不访问网络）。pi-env-mcp、MCP 服务和 skill 是 agent 的工具，由 agent 自己检查。
- `rl compute steps [<资源>] [--attempt ATT] [--commit <sha>]` 给出一次运行的全部步骤。带 `--attempt` 时用 attempt 记录的资源和它 RunSpec 里的提交；不带时用 `--commit`，再不带就用 checkout 的 HEAD。步骤依次是：推送分支（code 为 git），在主机上 fetch 并取出那个提交（code 为 archive 时是打包、传输、解开），启动方式和要设的环境变量（`SR_ATTEMPT_ID`、`SR_SPEC_DIGEST`），以及取回 `manifest.json` 和 `rl run import-result`。
- `rl run register ... --resource <名字>` 把资源的设置和计划中的位置（远程的快照目录和 `runs/<attempt>/`）记到 attempt 上。名字不在配置里时报 NOT_FOUND。
- 作业结束。执行器的完成通知配置成命令投递，在项目里运行 `rl run ended`（通知的 JSON 从标准输入读）：CLI 按绑定在 attempt 上的作业号找到 attempt，按每个作业自己的状态行记下结束；还在运行或状态未知的作业不记。提交者随后得到一条 `job-ended` 事项，监视程序叫醒它。作业超过预计结束时间还没有结束记录时，提交者得到一条 `job-overdue` 事项，去执行器那里核对。
- 同步（code 为 git）。`rl compute sync [<资源>]` 由协调者在开始时运行（`rl resume` 不运行它），researcher 在远程运行前用它推送自己的任务分支。对每个 `code: git` 的资源用到的 git remote：
  1. remote 不存在、`compute.repository` 是 `github-private`、项目自己就是一个 git 仓库（不是更大仓库里的一个目录）、也没有写 `git_url` 时，用 `gh repo create <项目目录名> --private --source <项目> --remote <git_remote>` 建仓库，记一条 `compute.repository` 事件。gh 不在、没有登录或重名时说明原因和手工的做法。
  2. 用 `git push`（没有 upstream 时加 `-u`）推送当前分支，记一条 `compute.push` 事件。不强推；失败时说明原因。还没有提交、HEAD 是 detached 时只说明，不推送。没有提交的修改不会过去，它也会提醒。
  3. 给每个资源打印一个脚本：在 `<workspace>/repo.git` bare clone 仓库（已经有就 fetch），并检查主机上有刚推送的提交。agent 要立刻用这个资源的连接执行它，这样主机读不了仓库（要在主机上 `gh auth login`，或者配 deploy key）在第一次运行之前就能发现。
  没有 `code: git` 的远程资源时，什么都不做。
- Python 环境。所有资源共用一份提交的 `pyproject.toml` 和 `uv.lock`（CPU、CUDA 等版本写成互斥的 extras，各个 extra 用同一个版本），只在本地改和 lock；主机上从快照建环境，放在快照外、按 lock 区分，用 `rl run register ... --param environment="..."` 记下来。详见 skill 的 `references/workflow.md`（Python environments）。

## 2. 约束文档 `docs/constraints.md`

`rl init` 生成一个模板，有四项要填：

- 研究目标和结论范围；
- 允许的数据，以及哪些计算资源可以用于什么（资源本身写在 `smooth-research.yaml` 的 `compute`）；
- 资源上限（总量，用 GPU·h、MIG·h、node·h 这样的单位）；
- 需要用户先决定的动作。

它写的是只有用户能改的东西。协调者和用户一起按计划填一次，所有角色都遵守；它不允许的事作为决定请求交给用户。CLI 不解析、不执行它：资源预算、可用的队列和各资源的使用范围只写在这里。协调者的 pack 附上它的前 25 行。

## 3. 环境变量

| 变量 | 含义 |
|---|---|
| `SMOOTH_RESEARCH_PROJECT` | 项目目录。查找顺序：先看 `--project`，再看这个变量，最后从当前目录往上找 `smooth-research.yaml`（在任务的 worktree 里时，找主 checkout 里的项目）。监视程序给每个会话的 tab 设它 |
| `SMOOTH_RESEARCH_SESSION` | 调用者是哪个会话（如 `scientist-T-0002`）。监视程序给每个会话的 tab 设它；命令以这个会话的身份运行，按角色检查权限。pane 外用 `--as <名字>` 指定 |
| `SMOOTH_RESEARCH_CHECKOUT` | 会话的工作目录不是项目根目录时（任务的 worktree），监视程序设成它。代码路径、测试和提交都按这个 checkout 读；研究记录仍写进主项目的 `research/`。不设时从当前目录判断：在项目的 linked worktree 里就用这个 worktree |
| `SMOOTH_RESEARCH_HOME` | 插件自己的 CLI 环境的位置（项目外的命令和还没有自己环境的项目用它，见第 6 节）。设了 `XDG_DATA_HOME` 时默认为 `$XDG_DATA_HOME/smooth-research`，否则为 `~/.local/share/smooth-research`。这里有 `env-<build-id>`，Claude 的额度读数存在 `usage/claude.json` |
| `SMOOTH_RESEARCH_RL` | `bin/rl` 启动器把它设成自己的绝对路径，让 hook 提示、监视程序和会话调用同一个 CLI。一般不用手动设 |
| `SMOOTH_RESEARCH_BUILD` | `bin/rl` 启动器设成插件的 build id（源码里运行时没有，按 `dev` 计算），形式是 `<源码摘要>-<构建摘要>`。`rl upgrade` 把它记为项目最后用的 build 并显示。一般不用手动设 |
| `HERDR_ENV`、`HERDR_WORKSPACE_ID` | herdr 设置，CLI 只读。`rl resume` 和监视程序要求 `HERDR_ENV=1`；新的 tab 建在 `HERDR_WORKSPACE_ID` 指的 workspace 里 |
| `CODEX_HOME` | 默认 `~/.codex`。CLI 从其中的 `sessions/` 读 Codex 的订阅额度 |

## 4. 命令行选项

`rl --help` 列出全部命令，`rl <命令> --help` 列出全部参数。这里只写和配置、会话、环境有关的部分。

- 全局选项：`--project`、`--json`（输出完整 JSON）、`--as <会话名>`（以这个会话的身份运行，默认取 `SMOOTH_RESEARCH_SESSION`）、`--read-only`。`--project`、`--json` 和 `--as` 也可以写在子命令后面。出错时在标准错误上打印 `error[代码]: ...`，退出码 1；冲突和过期的修订（CONFLICT、STALE_REVISION）退出码 3。
- 只读：`rl --read-only <命令>` 什么都不改：不升级旧格式的记录，不更新项目的 Python 环境（启动器不跑 uv add，用现有的 `.venv`，没有时用插件自己的环境），不写记录、文件和缓存。会写东西的命令直接拒绝（READ_ONLY）。能用的有 `status`、`show`、`history`、`tree`、`doctor`、`inbox`、`pack`、`refs`、`usage`、`hypothesis list`、`task list` 和 `run show`。
- `rl init [--plan <计划>] [--dir <目录>] [--python 3.13]`：记录由项目自己的 Python 环境生成，所以顺序是：
  1. 在项目目录运行 `uv init . --no-workspace --python=<版本> --name <目录名>`：写 `pyproject.toml`、`.python-version` 和 `src/<包名>/__init__.py`；项目不在任何 git 仓库里时 uv 还会 `git init`。已经有 `pyproject.toml` 时跳过。`--no-workspace` 让放在别的 uv 项目里面的研究项目保持独立。
  2. `uv add` rl 的依赖（取自插件的 `pyproject.toml`）：写进项目的 `pyproject.toml` 和 `uv.lock`，并建好 `.venv`。`.gitignore` 不存在时 rl 写 uv 的那一份，已有时在后面加 `.venv`。
  3. 用项目的 `.venv/bin/python` 运行 init：生成配置、目录和数据库，导入并封存计划。输出和 `project.init` 事件写明用的是哪个环境（Python 版本、latticedb 版本）。

  前两步在 `bin/rl` 启动器里用 shell 完成。`--python` 低于 rl 要求的版本（3.13）时拒绝。uv 失败时 init 停下，不生成记录。之后把新项目提交一次（包括 `pyproject.toml` 和 `uv.lock`）：任务分支从主线建。
- `rl resume`：启动监视程序（没在运行时）和协调者的 pane（没打开时），然后打印状态。`rl status` 只打印状态，不启动任何东西。
- `rl watch [run|once|start]`：见第 1 节“监视程序”。
- `rl agent dialog <名字>`、`rl agent answer <名字> <选项> --reason ..`：见第 1 节“对话框”。
- `rl usage statusline`：Claude Code 的状态栏命令，从标准输入读状态栏的 JSON，任何输入都不会让它失败。
- `rl compute [show|steps|sync] [<资源>] [--commit <sha>] [--attempt ATT]`、`rl run register ... --resource <资源>`、`rl run ended`：见第 1 节“计算资源”。
- `rl upgrade [--note ..] [--move <旧> <新>]`：见第 6 节。
- `<plugin>/bin/rl setup`：由启动器自己处理，不经过 Python 的 CLI。重建插件自己的环境，在项目里还更新项目的环境（见第 6 节）。

## 5. 写死的常量（不能配置）

| 常量 | 值 | 含义 |
|---|---|---|
| `LOCK_TIMEOUT_S` | 60 秒 | 读写等锁的上限，超时报 LOCKED（`store.py`） |
| `MAX_PASSES` | 3 次 | 审查轮里每一段最多几次审查（design 轮分 scientist 段和 researcher 段，conclusion 和 tree 轮各一段）。次数用完还有阻塞的问题时，这一轮升级：researcher 段交给建任务的 scientist，其他交给 advisor 裁决（`reviews.py`） |
| `PARALLEL_MAX` | 10 | hypothesis 任务的 `--parallel` 上限和默认值：它下面同时开几个 researcher 会话（`tasks.py`） |
| `BUDGETS` | 协调者 6000、scientist 12000、researcher 7000、reviewer 6000、advisor 6000 个字符 | 各角色 `rl pack` 的长度上限；超出时可裁剪的块先去掉最旧的行，并写明裁掉了几行（`pack.py`） |
| `NOTE_CHARS` | 300 字符 | 任务说明、引用附注等短文字的上限，更长的写进文档（`tasks.py`、`refs.py`） |
| `MAX_REFS` | 8 个 | 一条笔记或对话最多带几个定位符（`refs.py`） |
| `MAX_REVISIONS` | 12 个 | 一个封存文档最多几个修订（`documents.py`） |
| `STALE_AFTER_H` | 6 小时 | 未结束的 attempt 超过这么久没有新观测，显示时加 “stale?” 提示（`runs.py`） |
| `STALE_MINUTES` | 15 分钟 | 比这更旧的额度读数算过期，不让宿主算作额度低（`usage.py`） |
| 启动等待 | 90 秒 | 打开会话时等 agent 启动、回答信任问题的上限（`launch.py`） |

## 6. 插件升级

插件升级不会让进行中的研究停下来，只有一个例外：2026-11 重新设计之前建的项目。

- 项目的环境。研究项目里的 `rl` 命令都在项目自己的 `.venv` 上运行：代码来自插件（`src/`，不装进项目），依赖（`latticedb`、`pyyaml`）是项目 `uv.lock` 锁定的版本。任务 worktree 里的命令也找到主 checkout 的项目，用它的 `.venv`。插件升级后 rl 的依赖变了时，项目里第一条命令先 `uv add` 新的依赖（几个命令同时来时只做一次），并提示提交 `pyproject.toml` 和 `uv.lock`；会话开始的 hook 在后台做，不等它。删掉 `.venv` 也没关系，下一条命令会重建。
- 插件自己的环境。每个插件 build 一个（`$SMOOTH_RESEARCH_HOME/env-<build>`），只给研究项目以外的命令和还没有自己环境的项目用。会话开始的 hook 在后台开始构建，第一条要用它的命令等它构建完；几个会话同时启动时只构建一次。`<plugin>/bin/rl setup` 强制重建它，在项目里还更新项目的环境。需要 uv。
- 记录格式 5 以前的项目不打开。2026-11 重新设计之前建的项目（记录格式低于 5）打开时报 UNSUPPORTED：用建它的插件版本继续，或者从它的计划新建一个项目（`rl init --plan <计划>`），旧记录不带过来。
- 之后的格式自动升级。新版本的 CLI 第一次打开记录格式较旧（5 或以上）的项目时，在写锁下先把数据库文件复制到 `research/state/backups/schema-<旧格式>-<时间>/`，再在一个事务里运行旧格式到当前格式之间的全部迁移（`upgrade.MIGRATIONS`），记一条 `project.upgrade` 事件。迁移只补充和填写，不删除记录，不改记录下来的事实，不碰封存的文档。`--read-only` 遇到较旧的格式时拒绝（READ_ONLY），不升级。现在的格式是 5，还没有迁移。
- 更新的项目只读。项目由更新的插件写过（记录格式比 CLI 新）时，CLI 照常读取，但拒绝写入（UNSUPPORTED），提示更新插件。
- `rl upgrade` 显示项目的记录格式和插件 build、Python 环境、最近的升级和调整、配置警告、找不到的文档和记录问题，补建新布局要的目录，并说明 agent 可以自己调整什么；运行后记下当前 build。`--note "<改了什么>"` 记下 agent 手工做的调整（`project.adjusted` 事件）。`--move <旧> <新>` 移动文件或目录，并在同一个事务里改写记录里的路径；事务失败时文件移回原处。数据库和封存对象（`research/state/`、`research/documents/`）不能移动。
- agent 自己调整旧资产，不用问用户：可以改配置文件、只是格式变了的约束文档措辞、旧布局里的文件位置、任务工作目录里未封存的草稿、worktree 和分支，也可以用 `rl` 命令补上新版本需要的记录。只有会改变研究目标、结论范围、数据或预算的调整才问用户。记录下来的事实（观测、结果、证据、决定、审查）和封存的文档不改：新版本要求另一种形式时，新增一条引用旧记录的记录或修订。改完用 `rl upgrade --note` 记下来并提交。
- 开发规则。改记录格式时提高 `SCHEMA_VERSION`，在 `upgrade.MIGRATIONS` 里加迁移，并在 `tests/fixtures/projects/schema-<n>` 冻结一份旧格式的项目；已有的冻结项目（现在是 `schema-5`）保留，测试用每一份恢复项目、运行命令。不要删除读取格式 5 及以后项目的能力。
