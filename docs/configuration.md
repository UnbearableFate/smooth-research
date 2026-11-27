# Smooth Research 配置说明

配置分四处：项目配置文件 `smooth-research.yaml`、约束文档 `docs/constraints.md`、几个环境变量、命令行选项。程序读取的只有 `smooth-research.yaml`。约束文档写给人和 agent 看，CLI 不解析它。

插件只做引导和记录，不卡住 agent。这里的配置没有一项是名额、并发上限、超时或预算的强制执行：会话开几个、任务同时跑几个，由协调者放行多少任务决定；时间和用量只用来显示和提醒。

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
  tree: [claude:claude-fable-5-2@xhigh, codex:gpt-6.2-astra@xhigh]
agents:
  args: {claude: [--permission-mode, auto], codex: [], pi: []}
  beat_seconds: 30
  user_quiet_minutes: 3
  heartbeat_minutes: 5
  page_minutes: 15
  deadlines: {default: 240}
limits:
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

配置里的任何问题都不会让命令失败。CLI 用能用的值继续运行，并给出警告，说明它实际用了什么。每条命令都重新读配置，观察者每一拍也重读，所以改了就在下一条命令生效。

- 文件里的值和默认值深度合并：字典逐键覆盖，列表整体替换。比如 `models.reviewer` 只写一个条目，默认的两个就都没了。两个映射整体替换，不和默认值合并：`compute.resources`（文件里没写 `local` 时就没有默认的本机资源）和 `agents.deadlines`。
- 警告出现在两个地方：普通输出末尾的 `warning:` 行（`--json` 时不打印），以及用户自己在项目里开的 Claude Code 或 Codex 会话开始时的 hook 提示。
- 下面这些情况会警告：
  - 文件解析不了（YAML 写错，或者顶层不是键值对）：沿用上一次成功读取的设置，直到文件改好。这份设置由 CLI 自动保存在 `research/state/config.last-read.json`（`--read-only` 时不更新）；从没读成功过时用默认值；
  - `version` 不是 `1`：按版本 1 读取；
  - 不认识的键：没有作用。旧版本的键现在都属于这一类，可以删掉：2026-11-27 改版去掉的 `limits.scientists`、`limits.trial_minutes`、`limits.fast_lane`、`agents.clear_idle_minutes`、`agents.clear_context_tokens`，以及更早的 `agents.watch_minutes`、`agents.stall_minutes`、`agents.wait_check_minutes`、`usage.coordinator_host_left_percent`、`advice`、`context`、`manuscript` 等；
  - 某一节（如 `models`、`agents`）不是键值对：这一节全用默认值；
  - `models.<角色>` 或 `models.tree` 不是列表（写成一个字符串时当作只有一个条目的列表，不警告）：用默认列表；某个条目格式不对：跳过这一条；
  - `agents.beat_seconds`、`user_quiet_minutes`、`heartbeat_minutes`、`page_minutes` 不是正数：用默认值；
  - `agents.deadlines.<类型>` 不是正的分钟数：这个类型改用 `default`，`default` 也不能用时用 240；
  - `agents.args.<宿主>` 不是参数列表：用默认参数（写成一个字符串时按 shell 的规则拆开，不警告）；
  - `limits.envelope_lines` 不是正数列表：用默认值；
  - `usage.switch_left_percent` 不是 0 到 100 的数：用 10；
  - `compute.repository` 不是 `github-private` 或 `none`：按 `none`；`compute.tests` 不是 `local` 或 `job`：按 `local`；`compute.coordinator` 不是 `local` 或 `login`：按 `login`；
  - 计算资源有问题（见下面的“计算资源”）：不认识的键没有作用；`kind`、`connect`、`jobs` 的值不认识时，这个资源不能用（不回退到 ssh 或 shell）；`code` 的值不认识时按 `git`；远程资源缺 `host` 或 `workspace`、`workspace` 不是绝对路径、名字含有不允许的字符、`connect: local` 却写了 `jobs: shell` 或不是 `code: archive` 时，这个资源也不能用；`compute.default` 不是能用的资源时，用第一个能用的。

### 目录（固定）

目录不能配置，所有项目都一样。路径都相对于项目根目录。

| 位置 | 内容 |
|---|---|
| `src/` | 实验代码。`rl init` 先把项目目录建成 uv 项目（见第 4 节），代码包在 `src/<包名>/`。init 在 `src/` 放一个忽略 `__pycache__/` 和 `*.py[cod]` 的 `.gitignore` |
| `docs/` | 只放给人读的文档：`plan.md`（`rl init --plan` 导入的计划，封存）、`constraints.md`（约束文档，见第 2 节）、`research.html`（研究页，`docs/.gitignore` 让它不进 git）、`explanations/`（用户要求的解释）、`glossary.md`（术语表，研究页里也显示）。`explanations/*.md` 和 `glossary.md` 由协调者用 `rl docs <路径> --message ..` 提交到主线 |
| `research/` | 记录和工作目录：`state/`（数据库 `research.lattice`、写锁 `write.lock`、观察者的心跳 `observer.json`（schema 6 以前的 watcher 写 `watch.json`）和锁 `watch.lock`、`config.last-read.json`、升级时的备份 `backups/`）、`documents/objects/`（封存的文档）、`snapshots/`（`rl export` 的默认输出位置）、`literature/`（文献检索的记录）、`artifacts/<attempt>/`（本机 run 的输出）、`work/T-x/`（任务的工作文档，在任务的 worktree 里写、随分支合并）、`tree-draft/`（协调者的子代理写的建树草稿 `tree.yaml`，不是记录；`rl tree record` 记下后删掉）。`research/.gitignore` 让 `state/` 和 `artifacts/` 不进 git |
| 任务的分支和 worktree | 任务得到持有者时，CLI 从主线建分支 `sr/T-x` 和 worktree `<仓库目录>-worktrees/T-x`（放在仓库目录旁边；项目是仓库里的一个子目录时，会话在 worktree 里的同一个子目录工作）：协调者放行时，科学家新建执行或准备任务时，或者认领、派发一个还没有 worktree 的任务时。依赖的任务（`--depends-on`）还没验收时先不建，因为分支要从已经合入前置的主线开出；前置验收时 `rl task accept` 通知创建者认领。主线只在 CLI 接受任务时移动：先在任务分支上做一个合并主线的 merge commit，在它上面跑任务的测试，通过后主 checkout 快进到这个提交。测试没通过仍要移动主线时用 `rl task accept T-x --force --reason ..`（记一个 `force` 标记）；合并冲突不能强制 |

导入的计划不能换：再导入不同的计划会报 CONFLICT，判断依据是已经封存的计划。新的文档或新的方向，由协调者开子代理按 `rl tree prompt --from <文档>` 起草，用户批准后 `rl tree record` 记下（见下面的“假设树”）。

项目必须是至少有一次提交的 git 仓库，任务分支才能建；项目本身不能放在别的仓库的 linked worktree 里。

### 模型

五个角色各有一个模型列表：`models.coordinator`、`models.scientist`、`models.researcher`、`models.reviewer`、`models.advisor`。每一项的格式是 `<入口>:<模型>[@effort]`：

- 入口 `claude`、`codex`、`pi` 是在 herdr 里启动的交互式 Claude Code、Codex 或 pi 会话；`pi` 的模型写成 `<provider>/<模型>`，比如 `pi:openai/gpt-6-astra@high`。
- 入口 `manual` 仍能解析，但打开会话时跳过它。
- `@effort` 传给 Claude 的 `--effort`、Codex 的 `model_reasoning_effort` 或 pi 的 `--thinking`。

打开会话时选一次模型：按顺序取第一个可用的条目，即不是 `manual`、命令（`claude`、`codex`、`pi`）在 PATH 上的条目；所在宿主订阅额度低的条目排到最后（见“订阅额度”，所有角色都一样）。接着上次的宿主对话重新打开时，上次的条目还在列表里、还能用，就用它。一个都没有时不开会话，命令的输出说明原因。会话打开时用的条目记在会话记录上；开着的会话不换模型，改了配置只影响之后打开的会话。rl 自己不调用任何模型。

`models.tree` 不是角色：它是协调者建树子代理的模型，每个宿主家族一条，默认是该家族最好的模型、effort 为 xhigh。协调者的待办里建树那一项（`build-tree`）写明用协调者所在宿主的那一条（没有就用第一条）；出了更好的模型就改这里。

### 假设树

假设只写研究判断；为检验一个假设做的准备、工程、校准和复核，都是它的假设任务下面的任务。建树不是任务，也不占角色：

1. 还没有假设时，协调者的待办有一项 `build-tree`：开一个子代理（`models.tree` 的模型），只交代“运行 `rl tree prompt`，照它做，把它要的摘要交回来”。`rl tree prompt [--from <文档>...]` 打印给子代理的说明（默认读 `docs/plan.md`），协调者自己不读。宿主不能开子代理时，协调者自己照说明写。
2. 子代理写 `research/tree-draft/tree.yaml`，用 `rl tree check` 检查格式（只报告，不记录），交回摘要。已经有草稿时，待办改为 `tree-draft`：请用户看草稿，免得重启后又开一个子代理。
3. 协调者运行 `rl view`，研究页把草稿画出来（标明是草稿），用户在页面上看全文。要改就再开一个子代理，交代用户的原话；草稿改多少次都行。
4. 用户批准后，`rl tree record --approval "<用户原话>"` 一次记下：假设、存疑标记（`rl hypothesis keep|drop` 由用户决定）、一条路线决定、草稿里共用的准备工作（建成排队的公共准备任务）和用户的批准，然后删掉草稿。不带 `--approval` 照样记下，标记 `unapproved`。
5. 批准建树就是批准检验这些假设，放行它们的假设任务不再另要批准。以后由裁决引出的新假设，协调者直接排期、放行；只有预计会超出 `docs/constraints.md` 里一开始的预算时，才先问用户（`rl request --kind approval --task T-x ..`，答复用 `rl request answer RQ-x --by user --approve|--change --text "<原话>"`）。

### 会话

一个会话是一个角色做一件事，名字是 `<角色>-<对象>`：`coordinator`、`scientist-T-0002`、`researcher-T-0003`、`reviewer-RV-0004`、`advisor-AD-0001`。会话有三种打开途径：

- 交出工作的命令：对方没有开着的会话时，命令先打开它（见下面的“交接和通知”）；
- `rl session open <名字>`：手动打开（已经开着时，把还没告诉它的事项告诉它）；`--print` 打印在终端里手动启动它的命令，不需要 herdr；
- `rl resume`：一次性操作，见第 4 节。

打开会话不认领任何任务：持有者在交接时就写进了任务。打开时：

1. CLI 在当前 herdr workspace（`HERDR_WORKSPACE_ID`）里建一个不抢焦点的 tab。scientist 和 researcher 在任务的 worktree 里工作，其他角色在项目根目录。tab 的环境里设 `SMOOTH_RESEARCH_PROJECT`、`SMOOTH_RESEARCH_RL`、`SMOOTH_RESEARCH_SESSION`，工作目录不是项目根目录时再设 `SMOOTH_RESEARCH_CHECKOUT`。herdr 的 agent 名字和 tab 标签是“项目目录名的前几个字母 + 项目路径的短摘要 + 小写的会话名”，最长 32 个字符，所以两个项目的同名会话不会混在一起。
2. 用 `herdr agent start` 启动 agent，最多等 90 秒（打开是同步的，交接命令会等它）。这个会话以前开过、记着宿主的会话 id 时，接着那段对话（Claude `--resume`，Codex `codex resume`，pi `--session`）。CLI 自己加的参数只有下面这些，然后追加 `agents.args.<宿主>`：

| 宿主 | CLI 加的参数 |
|---|---|
| Claude Code | `--model`、`--session-id`（rl 生成的 UUID）、`-n <会话名>`、`--effort`（条目有 effort 时）、`--settings` 里一个运行 `rl usage statusline` 的状态栏（见“订阅额度”）；reviewer 另加 `--disallowedTools Edit,Write,NotebookEdit` |
| Codex | `-m`、`-c model_reasoning_effort="<effort>"`（条目有 effort 时）、`-c notice.hide_rate_limit_model_nudge=true`（额度快用完时不弹换模型的对话框）；advisor 另加 `--search` |
| pi | `--model`、`--session-id`（rl 生成的 UUID）、`--name <会话名>`、`--thinking`（条目有 effort 时） |

   不加任何绕过沙箱、审批或权限的参数。沙箱、权限方式和 MCP 设置由用户写在 `agents.args` 或宿主自己的配置里。交接命令在 agent 自己的进程里调用 herdr，沙箱要允许它；不允许时命令照样记录，并报告没送到的通知。
3. 宿主问是否信任文件夹时，如果问的正是 CLI 开会话的目录（任务的 worktree，或者用户启动协调者时已经信任的项目根目录），CLI 替协调者选“信任”（Claude Code：“Yes, I trust this folder”；Codex：“Trust and continue”），并记一条 `session.trusted` 事件。问的是别的目录，或者是别的对话框时，CLI 不回答，把会话记为需要决定（见下面的“对话框”）。
4. Codex 还显示 “Waiting for startup”（还在启动 MCP 服务）时，CLI 等它消失再发第一条提示。herdr 认不出的 Codex 对话框（“Hooks need review”，以及 `/new` 之后问新对话在哪里运行的问题）也按对话框处理。
5. 会话 id：Claude 和 pi 用 rl 给的 UUID；Codex 的 id 取自 herdr，herdr 还没报时 CLI 输入 `/status`（不调用模型），从 `Session:` 一行读出。引用屏幕时，CLI 去掉显示账号和 ChatGPT workspace 的两行。
6. 第一条提示是一行，以 `[smooth-research]` 开头：告诉会话它是谁、先运行 `rl pack` 并按它打印的内容工作、`rl inbox` 列出等它处理的事，后面附最多 3 条待办事项。

启动失败时，CLI 关掉刚建的 tab，会话记录结束，记一条 `session.failed` 事件；交接命令照样成功，并报告没能打开。

会话只由让它的工作结束的那条命令关掉，而且只关空闲的：`rl task accept` 或 `cancel` 关掉这个任务的研究者和它各轮的审核者；假设任务、阶段任务结束时，还关掉对应的科学家和它名下剩下的会话。在忙或停在对话框的会话不关。其余情况下会话留着也没关系，空闲时不花 token；`rl status` 列出工作已结束但还开着的会话，用 `rl session close <名字>` 关掉（`--force` 不管它在做什么）。`rl session list` 列出开着的会话。

角色卡建议没事就结束回合；在等别人的结果时，对方的命令会告诉它。等自己的短作业时可以自己等。插件不禁止等待。

### 交接和通知

一件事交给另一个会话时，由做这件事的命令当场通知对方：

| 命令 | 通知谁 |
|---|---|
| `rl task release T-x` | 假设、阶段任务：`scientist-T-x`；公共准备任务：`researcher-T-x`；`--to scientist`：`scientist-T-x` |
| `rl task dispatch`、`rl task answer` | `researcher-T-x` |
| `rl task block` | 任务的创建者（公共准备任务已经交给科学家时，那位科学家） |
| `rl task deliver` | 任务的创建者。公共准备任务完成检查通过时，交付时就验收、合并，再照 `accept` 一行通知 |
| `rl task return` | 持有者 |
| `rl task accept` | 依赖这个任务的任务的创建者或持有者 |
| `rl task cancel` | 持有者（它正忙、会话关不掉时） |
| `rl task claim`（接手） | 原来的持有者 |
| `rl review request` / `rl review answer` | 审核者（复审和改目标叫醒同一个会话）/ 作者 |
| `rl discuss`、`rl reply`、`rl question` | 对方 |
| `rl request` / `rl request answer` | 协调者 / 发请求的科学家。协调者给用户的请求不通知会话，进用户的待办 |
| `rl advise ask` / `rl advise answer` | 顾问 / 科学家 |
| `rl run ended` | 提交者 |
| `rl run usage` | 用量越过一条包络线时，假设任务的持有者；越过最后一条时也通知协调者 |
| `rl decide --final` | 依赖这个假设的其他假设的任务的持有者 |

通知的内容是对方现在的待办里还没告诉过它的事项（`src/smooth_research/inbox.py` 的规则表，从记录现算）。按对方会话的状态：

1. 开着、空闲：往它的 pane 里每条打一行（一次最多 4 条，多的写 “+N more: rl inbox”），记进会话记录的 `told`。
2. 开着、在忙：不打字。它结束回合时，Stop hook 把还没告诉它的事项交给它（`decision: block` 加一行原因），记进 `told`，同一件事不会再拦第二次。只对 rl 打开的会话生效。
3. 停在对话框：不打字，记下需要决定，交给协调者（协调者自己的对话框交给用户，列在 `rl status` 里）。
4. 没有开着的会话：打开它（见上面的“会话”）。
5. 协调者：它的 pane 也是用户的。用户在最近 `agents.user_quiet_minutes` 分钟里说过话时不打字：UserPromptSubmit hook 在用户下次说话时附上新事项，Stop hook 在协调者结束回合时交给它，用户安静下来以后观察者补送。
6. 以上都没送到（herdr 不在、超时）：命令照样成功，输出 “could not notify X: <原因>; remedy: rl session open X or rl resume”。

通知只是个指针，事实都在记录里，漏一次不丢东西：观察者在跑时下一拍补送；不在跑时，对方下次看 `rl inbox` 就能看到；谁有事却没开会话，`rl status` 里列着。

### 对话框

会话停在对话框时（启动时的上述情况，命令通知它时，或者观察者看到 herdr 报 `blocked`），CLI 把屏幕末尾记在会话记录上（`needs_decision`），算出一条 `session-needs-decision` 待办事项：别的会话的对话框交给协调者，协调者自己的对话框交给用户。会话停在对话框期间，不往里输入任何东西；对话框消失后，记录自动清掉。

- `rl agent dialog <名字>` 显示会话屏幕的末尾（最多 1500 个字符），谁都可以看。
- `rl agent answer <名字> <选项> --reason ..` 选择一个选项并按回车。选项可以写编号，也可以写选项文字的开头；按屏幕上最下面匹配的那一行认（对话框在对话内容下面，内容里也可能有 `2. …` 这样的行）。CLI 每按一次方向键都重新读屏幕，等选中行移动后再决定下一步；对话框一消失就不再按键。选中后清掉记录，记一条 `session.answered` 事件，写明选项和理由。
- 别的会话的对话框由协调者回答；协调者自己的对话框由用户在它的 pane 里回答（也可以用这个命令）。

### 观察者（可选）

观察者是一个只读的循环（`rl watch run`），在自己的 herdr tab（标签 `rl watch`）里运行。`rl resume` 在它没在运行时启动它（`rl watch start` 也可以单独启动），`rl watch once` 立刻走一拍。它不改研究记录（任务、审核、决定、运行），不开会话，不认领，不关会话，不收尾合并，不换模型，不清上下文；它写的只有会话记录里的 `told`、`needs_decision`、心跳文件和研究页。它停了研究照常进行，只是少了下面这些。

| 键 | 默认值 | 含义 |
|---|---|---|
| `agents.args.claude` | `["--permission-mode", "auto"]` | 追加给 Claude Code 会话的参数（不只观察者，所有打开会话的命令都用）。三个宿主都可以写成列表，也可以写成一个字符串（按 shell 的规则拆开） |
| `agents.args.codex` | `[]` | 追加给 Codex 会话的参数，比如沙箱和审批方式 `["-s", "workspace-write"]` |
| `agents.args.pi` | `[]` | 追加给 pi 会话的参数 |
| `agents.beat_seconds` | `30` | 观察者两拍之间隔几秒 |
| `agents.user_quiet_minutes` | `3` | 用户在协调者的 pane 里说过话之后，这么久之内命令和观察者都不往协调者的 pane 里输入 |
| `agents.heartbeat_minutes` | `5` | 心跳超过这么久（且至少 2 × `beat_seconds` + 60 秒）算作观察者没在跑。只在 `rl status` 里显示，其他命令不警告 |
| `agents.page_minutes` | `15` | 研究页至少每隔这么久重画一次；记录变了时每一拍都重画 |
| `agents.deadlines` | `{default: 240}` | 一条待办事项等了多少分钟后，观察者再提醒一次（每件事只提醒一次）。键是事项的类型，即 `rl inbox` 每条第一行的名字，比如 `hypothesis-task`、`prep-task`、`review-request`、`job-ended`、`decision-request`；没写的类型用 `default` |

每一拍做这些事：

1. 记录变了（`data_version` 变了）或者距上次超过 `page_minutes` 时，重画 `docs/research.html`。没有观察者时用 `rl view` 生成；页面写明生成于何时。
2. 对每个开着、空闲的会话，把还没告诉过它的事项打给它：命令没送到的，给协调者暂缓的，以及到点才出现的（例如作业过了预计结束时间还没有结果，`job-overdue`）；等得超过 `agents.deadlines` 的再提醒一次。往协调者 pane 里打字的规则同上面的“交接和通知”。接收方没开会话的，不替它打开，只在 `rl status` 里列出。
3. 记下停在对话框的会话（`needs_decision`）。
4. 写心跳 `research/state/observer.json`。

同一时刻只走一拍：每一拍持有 `research/state/watch.lock`，抢不到锁就跳过这一拍，不退出。每一拍都重读配置。

### 包络线

| 键 | 默认值 | 含义 |
|---|---|---|
| `limits.envelope_lines` | `[0.8, 1.0, 1.5]` | hypothesis 任务的计算包络（`--envelope "about 4 MIG·h"`）的几条线。已记录的用量越过任何一条线时，`rl run usage` 通知持有它的 scientist（一条 `usage-line` 事项：调整计划或发决定请求）；越过最后一条线时也通知协调者。之后有新的决定或包络改了，事项消失。用量按单位分开计算，不同单位不相加。只提醒，不停任何工作 |

### 订阅额度

Claude Code 和 Codex 一般用订阅，有 5 小时和每周两个窗口。

| 键 | 默认值 | 含义 |
|---|---|---|
| `usage.switch_left_percent` | `10` | 某个窗口只剩这么多（百分比）或更少时，这个宿主算作额度低 |

- 读数从哪里来。Codex 自己把窗口写进会话日志（`$CODEX_HOME/sessions/*/*/*/rollout-*.jsonl` 里的 `token_count` 事件）。CLI 比较最后写入的 5 个日志各自的最后一条读数，取事件时间最新的那条。Claude Code 只把额度交给状态栏命令：CLI 启动的 Claude 会话的状态栏运行 `rl usage statusline`，它把读数存进 `$SMOOTH_RESEARCH_HOME/usage/claude.json`（读数没变时一分钟最多写一次），并显示模型和各窗口还剩多少。pi 不报告窗口。
- 超过 15 分钟的读数算过期，不让宿主算作额度低；没有读数也一样。重置时间已经过了的窗口按没用过计算。
- 只在打开会话时用：额度低的宿主上的条目排到角色列表的最后，所有角色都一样。全都在额度低的宿主上时，照样用第一个能用的。已经开着的会话不关、不换模型。

### 计算资源

`compute` 一节写实验在哪里跑：本机，以及远程主机或集群。每个资源写清楚怎样连上去、用什么启动作业、远程的工作目录，以及代码怎样过去。rl 不连接任何资源，不复制文件，也不启动作业：它只读配置，检查本地能看到的东西，打印步骤，由 agent 用 Bash、ssh、pi-env-mcp 和提交作业的 MCP 或 skill 执行。唯一的例外是 git 这一侧：对 `code: git` 的资源，`rl compute sync` 在本地推送当前分支，必要时先建 remote（见下面的“同步”）；主机上的 clone 仍然是打印出来、由 agent 执行的步骤。预算和队列限制不写在这里，写在约束文档里（第 2 节）。

协调者在超级计算机登录节点上时，设 `compute.coordinator: login`。默认的 `local` 是普通的本机工作流；`login` 不让本机 checkout 或 `jobs: shell` 的资源用于执行，登记 run 时应写 `--resource`，不写照样记下，留 `no-executor` 标记。这是明确的设置，CLI 不按机器名猜测，也不替代站点工具的 allocation 检查。

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
| `compute.coordinator` | `local` | `local`：普通本机工作流。`login`：协调者在登录节点，所有工作负载（包括测试、smoke、构建和分析）通过配置的作业 MCP 或 skill；不让 checkout 和 `jobs: shell` 的资源用于执行，登记应带 `--resource`（不带照样记下，留 `no-executor` 标记）。不认识的值警告并按 `login` 处理 |
| `compute.default` | `local` | `rl compute steps` 不写资源时用的资源，也是 pack 里显示的默认资源。`rl run register` 不自动采用它：`local` 模式下可以不写 `--resource`（不记录资源），`login` 模式照样记下并留 `no-executor` 标记 |
| `compute.repository` | `none` | `code: git` 的资源缺 git remote 时，`rl compute sync` 怎么办：`github-private` 用 `gh repo create --private` 建一个仓库再推送；`none` 不建，提示问用户代码放到哪里 |
| `compute.tests` | `local` | `local`：`rl task test` 在任务的 worktree 里直接运行实验的语义测试或 prep 任务的完成检查，交付 prep 任务和接受时的合并测试也在本地跑。`job`：这些测试作为短作业运行（`rl run register --task T-x --request-id <新 id> --purpose design-test --minutes N`，走执行器的项目再加 `--resource <资源>`；run 记下当时的测试命令、提交和实验版本），结果用 `rl task test T-x --record pass|fail --attempt <ATT>` 记下，每个作业记一次（记的是那个作业跑的命令，之后改了命令或实验版本要重跑）；接受时任务停在 merging，等合并测试的作业（`--purpose merge-test`）结束：记下作业结束的那条命令（`rl run ended` 或 `rl run record`）就完成接受，中断了的接受重跑 `rl task accept T-x` 或 `rl resume` 接着做。`compute.coordinator: login` 时总按 `job`，不管这里写的是什么：登录节点上不跑测试。 |
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
- 作业结束。执行器的完成通知配置成命令投递，在项目里运行 `rl run ended`（通知的 JSON 从标准输入读）：CLI 按绑定在 attempt 上的作业号找到 attempt，按每个作业自己的状态行记下结束；还在运行或状态未知的作业不记。提交者随后得到一条 `job-ended` 事项，`rl run ended` 当场通知它的会话（见“交接和通知”）。作业超过预计结束时间还没有结束记录时，提交者得到一条 `job-overdue` 事项（观察者在跑时补送给它），去执行器那里核对。
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

它写的是只有用户能改的东西。协调者和用户一起按计划填一次，所有角色都遵守；它不允许的事作为决定请求交给用户。CLI 不解析、不执行它：资源预算、可用的队列和各资源的使用范围只写在这里。协调者的 pack 附上它的前 25 行。由裁决引出的新假设，预计用量（假设的 `cost`）加上已用量（`rl status` 的 compute used 一节，按阶段列出；研究页上也有各假设任务的花费）会超出这里的资源上限时，协调者先问用户再放行；超没超出由协调者判断。

## 3. 环境变量

| 变量 | 含义 |
|---|---|
| `SMOOTH_RESEARCH_PROJECT` | 项目目录。查找顺序：先看 `--project`，再看这个变量，最后从当前目录往上找 `smooth-research.yaml`（在任务的 worktree 里时，找主 checkout 里的项目）。rl 打开会话时给它的 tab 设它 |
| `SMOOTH_RESEARCH_SESSION` | 调用者是哪个会话（如 `scientist-T-0002`）。rl 打开会话时给它的 tab 设它；命令以这个会话的身份记录。流程上该由别的角色或别的会话做的事照样做，在记录上留 `role` 或 `not-holder` 标记；没有会话身份时，会写记录的命令拒绝（记不下是谁做的）。pane 外用 `--as <名字>` 指定 |
| `SMOOTH_RESEARCH_CHECKOUT` | 会话的工作目录不是项目根目录时（任务的 worktree），rl 打开会话时设成它。代码路径、测试和提交都按这个 checkout 读；研究记录仍写进主项目的 `research/`。当前目录在本项目的某个 worktree 里时，以当前目录为准，这个变量只作后备（`--brief` 等相对路径也这样解析） |
| `SMOOTH_RESEARCH_HOME` | 插件自己的 CLI 环境的位置（项目外的命令和还没有自己环境的项目用它，见第 6 节）。设了 `XDG_DATA_HOME` 时默认为 `$XDG_DATA_HOME/smooth-research`，否则为 `~/.local/share/smooth-research`。这里有 `env-<build-id>`，Claude 的额度读数存在 `usage/claude.json` |
| `SMOOTH_RESEARCH_RL` | `bin/rl` 启动器把它设成自己的绝对路径，让 hook 提示、观察者和会话调用同一个 CLI。一般不用手动设 |
| `SMOOTH_RESEARCH_BUILD` | `bin/rl` 启动器设成插件的 build id（源码里运行时没有，按 `dev` 计算），形式是 `<源码摘要>-<构建摘要>`。`rl upgrade` 把它记为项目最后用的 build 并显示。一般不用手动设 |
| `HERDR_ENV`、`HERDR_WORKSPACE_ID` | herdr 设置，CLI 只读。打开会话、往 pane 里送通知和观察者要求 `HERDR_ENV=1`；没有时命令照样记录，报告没送到的通知，`rl session open <名字> --print` 打印手动启动的命令。新的 tab 建在 `HERDR_WORKSPACE_ID` 指的 workspace 里 |
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
- `rl resume`：一次性操作，随时可以再跑：先接着做中断了的验收合并，再打开协调者的 pane（没打开时），然后打开所有有待办却没开着会话的会话，启动观察者（没在运行时），最后打印状态。没有 herdr 时只做第一步，并提示 `rl session open <名字> --print`。
- `rl status`：只打印状态，不启动任何东西：观察者在不在跑（只作信息）、正在进行的工作、等用户的事、“没按流程做的事”（标记）、有事却没开会话的、工作已结束但还开着的会话、停在对话框的会话。
- `rl session open|close|list [<名字>] [--print] [--force]`：见第 1 节“会话”。
- `rl tree [prompt|check|record]`：不带子命令时打印假设树；其余见第 1 节“假设树”。
- `rl watch [run|once|start]`：见第 1 节“观察者”。
- `rl task accept T-x [--force --reason ..]`：合并提交上的测试没通过仍要移动主线时加 `--force --reason`，这是唯一要强制的一步。
- `rl agent dialog <名字>`、`rl agent answer <名字> <选项> --reason ..`：见第 1 节“对话框”。
- `rl usage statusline`：Claude Code 的状态栏命令，从标准输入读状态栏的 JSON，任何输入都不会让它失败。
- `rl compute [show|steps|sync] [<资源>] [--commit <sha>] [--attempt ATT]`、`rl run register ... --resource <资源>`、`rl run ended`：见第 1 节“计算资源”。
- `rl upgrade [--note ..] [--move <旧> <新>]`：见第 6 节。
- `<plugin>/bin/rl setup`：由启动器自己处理，不经过 Python 的 CLI。重建插件自己的环境，在项目里还更新项目的环境（见第 6 节）。

## 5. 写死的常量（不能配置）

| 常量 | 值 | 含义 |
|---|---|---|
| `LOCK_TIMEOUT_S` | 60 秒 | 读写等锁的上限，超时报 LOCKED（`store.py`） |
| `REVIEWS_PER_ROUND` | 2 次 | 一轮审核最多几次审核：审核 → 修改 → 复审 → 修改。第一次就通过、没有未关闭的发现时这一轮结束；复审之后不管还剩什么都结束，剩下的发现成为标记，作者照常往下走。还想审就开新的一轮（`reviews.py`） |
| `BUDGETS` | 协调者 6000、scientist 12000、researcher 7000、reviewer 6000、advisor 6000 个字符 | 各角色 `rl pack` 的长度上限；超出时可裁剪的块先去掉最旧的行，并写明裁掉了几行（`pack.py`） |
| `NOTE_CHARS` | 300 字符 | 任务说明、引用附注等短文字的建议长度。更长的照样记下，留一个 `long-note` 标记，通知和待办里只显示前 300 个字符；长的推理写进文档（`tasks.py`） |
| `STALE_AFTER_H` | 6 小时 | 未结束的 attempt 超过这么久没有新观测，显示时加 “stale?” 提示（`runs.py`） |
| `STALE_MINUTES` | 15 分钟 | 比这更旧的额度读数算过期，不让宿主算作额度低（`usage.py`） |
| 启动等待 | 90 秒 | 打开会话时等 agent 启动、回答信任问题的上限（`launch.py`） |
| `SHOWN` | 4 条 | 一次往 pane 里打、或由 hook 交给会话的事项条数，多的写 “+N more: rl inbox”（`sessions.py`、`hooks.py`） |

## 6. 插件升级

插件升级不会让进行中的研究停下来，只有一个例外：2026-11 重新设计之前建的项目。

- 项目的环境。研究项目里的 `rl` 命令都在项目自己的 `.venv` 上运行：代码来自插件（`src/`，不装进项目），依赖（`latticedb`、`pyyaml`）是项目 `uv.lock` 锁定的版本。任务 worktree 里的命令也找到主 checkout 的项目，用它的 `.venv`。插件升级后 rl 的依赖变了时，项目里第一条命令先 `uv add` 新的依赖（几个命令同时来时只做一次），并提示提交 `pyproject.toml` 和 `uv.lock`；会话开始的 hook 在后台做，不等它。删掉 `.venv` 也没关系，下一条命令会重建。
- 插件自己的环境。每个插件 build 一个（`$SMOOTH_RESEARCH_HOME/env-<build>`），只给研究项目以外的命令和还没有自己环境的项目用。会话开始的 hook 在后台开始构建，第一条要用它的命令等它构建完；几个会话同时启动时只构建一次。`<plugin>/bin/rl setup` 强制重建它，在项目里还更新项目的环境。需要 uv。
- 记录格式 5 以前的项目不打开。2026-11 重新设计之前建的项目（记录格式低于 5）打开时报 UNSUPPORTED：用建它的插件版本继续，或者从它的计划新建一个项目（`rl init --plan <计划>`），旧记录不带过来。
- 之后的格式自动升级。新版本的 CLI 第一次打开记录格式较旧（5 或以上）的项目时，在写锁下先把数据库文件复制到 `research/state/backups/schema-<旧格式>-<时间>/`，再在一个事务里运行旧格式到当前格式之间的全部迁移（`upgrade.MIGRATIONS`），记一条 `project.upgrade` 事件。迁移只补充和填写，不删除记录，不改记录下来的事实，不碰封存的文档。`--read-only` 遇到较旧的格式时拒绝（READ_ONLY），不升级。现在的格式是 6：5 → 6 的迁移去掉并行上限和阶段的科学家上限，把升级了的或已审过两次的审核轮次和等顾问裁决的单子改为结束，补上批准的选项（approve 或 change）和空的标记字段；拆树任务、树审核和复合假设留着，照读（设计 2026-11-27 第 9 章）。
- 更新的项目只读。项目由更新的插件写过（记录格式比 CLI 新）时，CLI 照常读取，但拒绝写入（UNSUPPORTED），提示更新插件。
- `rl upgrade` 显示项目的记录格式和插件 build、Python 环境、最近的升级和调整、配置警告、找不到的文档和记录问题，补建新布局要的目录，并说明 agent 可以自己调整什么；运行后记下当前 build。`--note "<改了什么>"` 记下 agent 手工做的调整（`project.adjusted` 事件）。`--move <旧> <新>` 移动文件或目录，并在同一个事务里改写记录里的路径；事务失败时文件移回原处。数据库和封存对象（`research/state/`、`research/documents/`）不能移动。
- agent 自己调整旧资产，不用问用户：可以改配置文件、只是格式变了的约束文档措辞、旧布局里的文件位置、任务工作目录里未封存的草稿、worktree 和分支，也可以用 `rl` 命令补上新版本需要的记录。只有会改变研究目标、结论范围、数据或预算的调整才问用户。记录下来的事实（观测、结果、证据、决定、审查）和封存的文档不改：新版本要求另一种形式时，新增一条引用旧记录的记录或修订。改完用 `rl upgrade --note` 记下来并提交。
- 开发规则。改记录格式时提高 `SCHEMA_VERSION`，在 `upgrade.MIGRATIONS` 里加迁移，并在 `tests/fixtures/projects/schema-<n>` 冻结一份旧格式的项目；已有的冻结项目都保留，测试用每一份恢复项目、运行命令。不要删除读取格式 5 及以后项目的能力，也不要用一次性脚本代替迁移。
