# Smooth Research 配置说明

配置分四处：项目配置文件 `smooth-research.yaml`、约束文档、几个环境变量、命令行选项。程序读取的只有 `smooth-research.yaml`。约束文档写给人和 agent 看，CLI 不执行它。

## 1. 项目配置 `smooth-research.yaml`

它放在研究项目的根目录，由 `rl init` 生成。默认值在 `src/smooth_research/project.py` 的 `DEFAULT_CONFIG`。

### 加载规则

配置里的任何问题都不会让命令失败。CLI 用能用的值继续运行，并给出警告，说明它实际用了什么。

- 文件里的值和默认值深度合并：字典逐键覆盖，列表整体替换。比如 `models.reviewer` 只写一个条目，默认的两个就都没了。`compute.resources` 例外：它整体替换，文件里没写 `local` 时就没有默认的本机资源。
- 警告出现在三个地方：
  - 普通输出末尾的 `warning:` 行；命令失败时，警告和错误一起打印，它们可能正好说明失败的原因；
  - `--json` 结果里的 `warnings`；
  - 会话开始时的 hook 提示。
- 下面这些情况会警告：
  - **文件解析不了**（YAML 写错，或者顶层不是键值对）：沿用上一次成功读取的设置，直到文件改好。这份设置由 CLI 自动保存在 `research/state/config.last-read.json`；从没读成功过时用默认值；
  - `version` 不是 `1`：按版本 1 读取；
  - 不认识的键（拼错了？）：没有作用；
  - 某一节（如 `models`、`context`）不是键值对：这一节全用默认值；
  - 模型列表不是列表（一个字符串当作只有一个条目的列表）：用默认列表；某个条目格式不对：跳过这一条，用列表里其余的条目；
  - `advice.max_rounds` 不是非负整数：按 3 计算；
  - `agents.watch_minutes` 不是分钟数：按 5 分钟计算；`agents.stall_minutes` 不是正的分钟数：按 30 分钟计算；
  - `usage.switch_left_percent`、`usage.coordinator_host_left_percent` 不是 0 到 100 的数：用默认值 10 和 20；
  - `agents.args.<宿主>` 不是参数列表：用默认参数（写成一个字符串时，按 shell 的规则拆开，不警告）；
  - `context` 的 token 目标不是正整数：用默认值；
  - `manuscript.latex` 不是一条命令：写 Markdown 草稿；
  - 计算资源有问题（见下面的“计算资源”）：不认识的键没有作用；`connect`、`jobs`、`code` 的值不认识时分别按 `ssh`、`shell`、`git` 计算；远程资源缺 `host` 或 `workspace`、`workspace` 不是绝对路径、名字含有不允许的字符时，这个资源不能用；`compute.default` 不是能用的资源时，用第一个能用的。

### 目录（固定）

目录不能配置，所有项目都一样。路径都相对于项目根目录。

| 位置 | 内容 |
|---|---|
| `src/` | 实验代码。init 会创建它，并放一个忽略 `__pycache__` 和 `*.pyc` 的 `.gitignore`。`rl init` 先把项目目录建成 uv 项目（见第 4 节 `rl init`），代码包在 `src/<包名>/`。工作包和 `rl delegate` 的提示词让 agent 把代码放这里，但不强制，审查范围和 RunSpec 可以引用任何路径 |
| `docs/` | 只放给人读的文档：`plan.md`（`rl init --plan` 导入的原始计划，封存）、`constraints.md`（约束文档，见第 2 节）、`research.html`（`rl view`，研究说明页，见第 4 节"研究说明页"）、论文草稿（`rl manuscript`，见下面的"论文草稿"）、`explanations/`（用户让 agent 解释某件事时，agent 把说明写在这里） |
| `research/` | 其余的一切。agent 的工作文档：`experiments/`（实验契约）、`tasks/`（任务说明和交接笔记）、`notes/`（researcher 的笔记，写在它自己的 checkout 里，随分支合并）、`reviews/` 和 `advice/`（审查和建议的回答）、`reports/`（`rl report` 的阶段报告）。机器记录：数据库 `state/research.lattice` 和写锁、封存的文档 `documents/objects/`、请求包 `packages/`、模型调用日志 `calls/`、run 产物 `artifacts/<attempt>/` 等。init 写的 `.gitignore` 让 `state/`、`calls/`、`artifacts/` 不进 git |

- **导入的计划不能换。** 再导入不同的计划会报 CONFLICT。判断依据是已经封存的计划，和文件现在在哪里无关。计划的变化要记成假设修订和决定。

### 模型

三个角色各有一个模型列表，每一项是一个模型条目，格式是 `<入口>:<模型>[@effort]`：

- **`claude`**：用 `claude -p` 调用。审查和建议是只读会话，只开放 Read、Grep、Glob 三个工具。
- **`codex`**：用 `codex exec` 调用。审查和建议按你自己的 Codex 配置正常启动，rl 不加沙箱参数，审查者自己读文件。不改文件靠的是提示词里的要求。（以前用 read-only 沙箱，可有的机器跑不了这个沙箱，比如 AppArmor 限制了非特权 user namespace 时 bubblewrap 起不来，Codex 就一个命令也执行不了，读不了文件。）
- **`pi`**：用 `pi -p --mode json` 调用。模型写成 `<provider>/<模型>`，比如 `pi:openai/gpt-6-astra@high`。审查和建议只开放 read 工具。
- **`manual`**：不自动调用。CLI 给出请求包的路径，由人提交给模型（代码审查要连同包里列出的文件一起提交），再用 `rl review import` 或 `rl advise import` 导入回答。
- **`@effort`**：传给 Claude 的 `--effort`、Codex 的 `model_reasoning_effort`，或 pi 的 `--thinking`。
- **headless 和 herdr 会话。** 上面说的 `claude -p`、`codex exec`、`pi -p` 是 headless 调用（`rl delegate`、`rl review run`、`rl advise run`）。协调者在 herdr 里时，`rl agent open` 把同一个条目开成交互会话，放在单独的 tab 里，见下面的“agent 会话”。
- **模型家族**：先按模型名的最后一段判断：`claude`、`opus`、`sonnet`、`haiku`、`fable` 开头的属于 anthropic，`gpt`、`o1`–`o9`、`codex`、`chatgpt` 开头的属于 openai。deepseek、google（gemini、gemma）、alibaba（qwen、qwq）、moonshot（kimi）、zhipu（glm）、xai（grok）、mistral、meta（llama）也认得。名字判断不了时看 pi 的 provider 前缀（如 `deepseek/`），再不行按入口推断（`claude` 是 anthropic，`codex` 是 openai）。审查者和作者的模型名相同时，不论家族表认不认得，都算同一家族。

| 键 | 默认值 | 怎样使用 |
|---|---|---|
| `models.researcher` | `claude:claude-opus-5-5@high`、`codex:gpt-6-astra@high` | `rl delegate` 和 `rl agent open --role researcher` 不带 `--entry` 时按顺序使用。跳过 `manual` 和没有安装的入口。`rl delegate` 的会话失败就换下一个，角色名不变，下一个会话接着做同一个任务 |
| `models.reviewer` | `codex:gpt-6-astra@medium`、`claude:claude-opus-5-5@medium` | 代码审查和结论审查按顺序尝试，第一个给出可解析回答的生效。`rl agent open --role reviewer --ticket RV-x` 用工单候选里第一个能开会话的条目 |
| `models.advisor` | `codex:gpt-6-astra@max`、`claude:claude-fable-5-1@high` | 一轮建议里每个条目有自己的工单。`rl advise run` 同时调用它们，每个独立回答；herdr 里每个工单开一个 advisor 会话 |

几条容易忽略的行为：

- **审查者只由配置决定。** CLI 不再按作者的模型家族排除候选。回答和作者的关系（relation）仍按实际模型记录：`cross_model`、`cross_model_unverified`、`same_family_independent_session` 或 `same_family_unverified`。代码审查的复用、`rl run register` 的覆盖检查和 `rl decide` 的检查都不看 relation。要跨家族审查，把另一家族的模型排在列表前面。
- **按顺序尝试。** 调用失败，或者回答里没有可解析的结果块，就试下一个。
- **审查遇到 `manual` 就停。** 工单变成 pending，等人提交回答，排在它后面的候选不会被尝试。所以 `manual` 要放在最后。
- **候选列表在建工单时写入。** `rl review request` 和 `rl advise prepare` 建工单时就把候选固定下来，之后改配置不影响已经建好的工单。要换模型，用 `rl review run RV-x --entry <条目>`。
- **advisor 配置参与建议请求的去重。** 改了 advisor 列表后，同一个问题不会再被判为重复请求。
- **`rl models` 只检查命令行工具。** 它列出三个角色的条目，检查 `claude`、`codex`、`pi` 命令在不在 PATH 上，不验证模型本身；它还给出已经用了几轮建议。实际使用的模型在第一次调用时记录。

### agent 会话（herdr）

协调者在 herdr 里运行时（`HERDR_ENV=1`），`rl agent open` 为 researcher、reviewer 和 advisor 各开一个交互会话，每个会话一个 tab。用哪个模型按上面的列表选。

| 键 | 默认值 | 含义 |
|---|---|---|
| `agents.args.claude` | `["--permission-mode", "auto"]` | 追加给交互式 Claude Code 会话的参数。三个宿主都可以写成列表，也可以写成一个字符串（按 shell 的规则拆开） |
| `agents.args.codex` | `[]` | 追加给交互式 Codex 会话的参数。沙箱和审批方式要用户自己写在这里，比如 `["-s", "workspace-write"]` |
| `agents.args.pi` | `[]` | 追加给交互式 pi 会话的参数 |
| `agents.watch_minutes` | `5` | 监视程序每隔几分钟走一拍（查看一次各个会话和协调者）；`0` 表示关闭。还写着旧默认值 20 的项目，监视程序启动时和 `rl upgrade` 会提示改成 5 |
| `agents.stall_minutes` | `30` | 一个回合在会话日志里多久没有进展算作停滞，报告给协调者 |

- **CLI 自己加的参数只有模型、effort、会话 id、名字和订阅额度的两项。**
  - Claude：`--model`、`--effort`、`--session-id`、`-n`，以及运行 `rl usage statusline` 的状态栏（`--settings`，见下面的“订阅额度”）；reviewer 另加 `--disallowedTools Edit,Write,NotebookEdit`。
  - Codex：`-m`、`-c model_reasoning_effort="<effort>"`，以及 `-c notice.hide_rate_limit_model_nudge=true`（见“订阅额度”）；advisor 另加 `--search`。
  - pi：`--model`、`--thinking`、`--session-id`、`--name`。
  - 不加任何绕过沙箱、审批或权限的参数。
- **researcher 的 worktree。** 每个 researcher 会话一个 git worktree，在 `<项目目录>/../<项目名>-worktrees/<会话名>`。每个任务一个分支 `sr/<会话名>/<任务>`，从主 checkout 当前的分支建（`--base` 可以改）。项目必须是至少有一次提交的 git 仓库，否则用 `--no-worktree`。研究记录总是写进主项目的 `research/`。
- **信任文件夹的问题由 CLI 回答，其他询问由协调者判断。** Claude Code 和 Codex 在新目录里会先问是否信任这个文件夹。如果问的正是 `rl agent open` 开会话的目录（会话的 worktree，或者用户启动协调者时已经信任的项目目录），CLI 就替协调者选“信任”，并记一条 `session.trusted` 事件。Codex 把信任记在主仓库的根目录下（`~/.codex/config.toml` 的 `[projects]`），之后同一项目的 worktree 不再问。
- **各角色的询问和许可请求交给协调者，不转给用户。** 角色会话用 `rl agent tell coordinator` 提问或请求许可；会话里的其他对话框（审查 hooks、启用 MCP 服务器、审批、问别的目录的信任问题）由 `rl agent open`、没能送达的通知或监视程序报告给协调者。协调者能判断的就直接判断并回答：问题用 `rl agent tell` 回答；对话框用 `rl agent dialog <名字>` 查看选项，用 `rl agent answer <名字> <选项> --reason ..` 选择并记下理由（`session.answered` 事件）。记录不足以判断时选稳妥的选项。只有协调者不能判断的才问用户：超出范围、预算或约束文件留给用户的动作，超过 `advice.max_rounds` 的建议轮，用户的账户和凭据（登录、安全密钥、付款），以及项目以外不可撤销的操作。角色会话不能回答对话框。
- **监视程序。** `rl agent open` 同时在单独的 tab（`sr-watch`）里启动监视程序。它启动时先走一拍，之后每 `agents.watch_minutes` 分钟走一拍。每一拍对每个会话用一次 `herdr agent get`（状态、`state_change_seq`、宿主的会话 id，id 写回 `Session.host_session_id`），对 Codex 会话再读它的会话日志（`$CODEX_HOME/sessions/*/*/*/rollout-*-<id>.jsonl`）上次之后新写的部分；额度低的宿主每拍算一次。每个会话归入下面第一个符合的类别：
  - **退出**：agent 退出了，或者 tab 被关掉了。在原来的目录里重新启动，开一个新对话，从 `rl agent brief` 读回任务；一小时最多重启 3 次。额度低的宿主上不重启，请协调者换模型；
  - **对话框**：只回答该会话自己目录的信任问题，其他对话框报告一次；
  - **额度低**：额度低的宿主上的 researcher 和 reviewer。通知它结束工作、通知协调者换模型；这个宿主上不提醒、不重试、不重启（见下面的“订阅额度”）；
  - **回合出错**：日志显示最后一个回合以错误结束（`task_complete` 带 `error`）。暂时性错误（`codex_error_info` 为 `server_overloaded`）在下一拍重试，之后隔 2 拍、再隔 4 拍，一串错误最多重试 3 次；只有一个回合正常结束并且做了事，这一串才算结束（herdr 显示 working 不算）。3 次之后报告协调者：researcher 和 reviewer 建议 `rl agent switch`，advisor 只报告。额度用完（`usage_limit_exceeded`）按额度低处理；其他错误报告一次。回合内部的重连不算错误；
  - **停滞**：回合没结束、没有未完成的工具调用、超过 `agents.stall_minutes` 没有进展。读一次屏幕，把末尾附在报告里，不输入任何东西。一个工具调用挂起超过 4 倍的停滞时间也报告一次。进展指回合事件、工具调用及其输出、回复和推理；设置事件、token 计数和输入进会话的提示（包括监视程序自己的）不算；
  - **空闲未完成**：空闲、任务没做完、又没在等什么。连续两拍 `state_change_seq` 不变才提醒它继续；同一项任务最多提醒 2 次，之后报告协调者；
  - **其他**：在工作，或者正常的暂停：任务记为 waiting（`rl task wait`，比如等集群作业）或 blocked；在等自己申请的审查；任务所测实验的 run 还处于 queued 或 running；发出的消息还没有回复（发给协调者的，以协调者还没读为准）。回合出错的会话不算在等回复。

  日志说回合已经结束时，以日志为准，不看 herdr 的 working。Claude Code 和 pi 的会话只看 herdr（Claude 的日志还没有错误样本，不定规则）；没有会话 id 时也只看 herdr，不按目录去猜日志，`rl agent list` 显示“log unavailable”。

  非斜杠命令的提示用 `herdr agent prompt <目标> <文字> --wait --until working --until blocked --timeout 20000` 发送：herdr 看到 working 或 blocked 才算送达；Codex 会话的日志在发送之后出现新的 `task_started` 也算。确认不了的报告“delivery not confirmed”（附屏幕末尾；屏幕上像是 Codex 的 Hooks 面板时注明可能被面板挡住），不计数，在会话有动静之前不再输入；只有确定没送出去（对话框挡着、agent 不在）才在下一拍重发。斜杠命令照旧。提醒、重试、重启的次数分别记在 `Session.watch` 里，只在确认送达后加一；写入时在写事务里重新读出再合并。“已经告诉过协调者”的标记和那一拍的 NOTE 在同一个事务里写入。

  每一拍的发现汇成一条来自 `watch` 的 NOTE 发给协调者。协调者自己的回合以暂时性错误结束、又空闲了一整拍时，按同样的规则重试；协调者空闲、又有消息一整拍没读时，提醒一次。所以协调者不需要自己轮询会话。监视程序的一拍和 `rl agent check` 用同一把锁（`research/state/watch.lock`），锁被占着时这一拍跳过并说明。监视程序的 tab 只打印发现了什么的那些拍。没有打开的会话时，监视程序自己退出。正常结束一个会话要用 `rl agent close`；用别的方式关掉的会话算作意外停止，会被重新启动。
- **审查轮次。** 每轮最多两次审查（第一次加一次复查），是写死的常量，见第 5 节。

### 订阅额度

Claude Code 和 Codex 一般用订阅，有 5 小时和每周两个窗口。某个窗口剩得太少时，这个宿主上的 researcher 和 reviewer 把工作交给角色列表里的下一个模型。

| 键 | 默认值 | 含义 |
|---|---|---|
| `usage.switch_left_percent` | `10` | 某个窗口只剩这么多（百分比）或更少时，这个宿主算作额度低 |
| `usage.coordinator_host_left_percent` | `20` | 协调者自己所在的宿主用这个阈值，因为协调者还要接着用它 |

- **额度从哪里读。** `rl agent list` 在会话之后为每个宿主列一行：窗口、还剩多少、读数是什么时候的、阈值、是否额度低；没有打开的会话时也列出。
  - Codex 自己把窗口写进会话日志（`$CODEX_HOME/sessions/**/rollout-*.jsonl` 里的 `token_count` 事件）。CLI 比较最后写入的 5 个日志各自的最后一条读数，取事件时间最新的那条（文件写得晚不等于读数新）。
  - Claude Code 只把额度交给状态栏命令。`rl agent open` 启动的 Claude 会话的状态栏运行 `rl usage statusline`：它把最新读数存进 `$SMOOTH_RESEARCH_HOME/usage/claude.json`，并显示模型和各窗口还剩多少。
  - 协调者用 Claude Code 时，`rl session start --role coordinator --host claude` 在项目的 `.claude/settings.local.json` 里加上同一个状态栏（第一次写时同时写 `.claude/.gitignore`）。用户自己已经设了状态栏（在 `$CLAUDE_CONFIG_DIR/settings.json`，默认 `~/.claude/settings.json`，或者项目的 `.claude/settings.json`、`.claude/settings.local.json`）时不动它，只给出提示。
  - 读数只有这台机器上这个宿主最后一次调用那么新。超过 15 分钟的读数算过期：照样显示，但不让宿主算作额度低；没有读数也一样。重置时间已经过了、之后又没有新读数的窗口，显示“reset …, no reading since”，不算额度低。pi 不报告窗口。
- **额度低时。** 协调者不换自己的模型。
  - 新开的 researcher 和 reviewer 会话、`rl delegate`、`rl review run` 跳过额度低的宿主上的条目，用角色列表里的下一个。审查跳过的条目记在工单的 `calls` 里。`--entry` 指定的条目照常使用。
  - 监视程序每一拍看一次额度。额度低的宿主上的 researcher 收到通知：在安全的地方停下，在任务分支上提交，写交接说明，运行 `rl task handoff`，然后结束这一轮。reviewer 收到通知：不交答案，马上停下。每个窗口只通知一次（确定没送出去时下一拍再送）。协调者收到一条 NOTE。这些会话不会被提醒继续；它们的 agent 退出了，也不在这个宿主上重启。
  - 会话空闲以后（researcher 的 HANDOFF 已经到了），协调者运行 `rl agent switch <名字>`。它关掉这个会话但保留 worktree，在下一个不在额度低的宿主上的条目上开一个同名的新会话，带上同一个任务或工单；新会话从 brief 里读到交接说明。reviewer 从工单的候选条目里选。之后的复查（`--recheck-of`）交给替换后的同名会话。
  - advisor 不换模型，也不收通知：留下的额度给 advisor 用。
- **Codex 不弹换模型的对话框。** 额度快用完时，Codex 会弹出 "Approaching rate limits" 对话框，问要不要换一个更便宜的模型。CLI 启动的 Codex 会话加了 `-c notice.hide_rate_limit_model_nudge=true`，不弹这个对话框，交接由 CLI 来做。协调者自己的 Codex 会话不受这个参数影响：在对话框里选 "Keep current model (never show again)"，或者在 `~/.codex/config.toml` 里写 `[notice]` 和 `hide_rate_limit_model_nudge = true`。

### 建议轮数

| 键 | 默认值 | 含义 |
|---|---|---|
| `advice.max_rounds` | `3` | 每个项目最多几轮建议。一次 `rl advise prepare` 开一轮 |

- **怎样计数。** 一轮里只要有一个工单已经回答、还在等待或正在运行，这一轮就计数。所有 advisor 都调用失败或不可用的一轮不计数。
- **用完以后。** `rl advise prepare` 报 LIMIT。协调者先向用户说明为什么还需要建议。用户同意后，用 `--user-approved "<用户的原话>"` 开新一轮。这句话记在工单和事件日志里。
- **重复的请求不占轮数。** 同一个问题、同样的证据会返回已有的工单。

### 上下文包

| 键 | 默认值 | 含义 |
|---|---|---|
| `context.work_target_tokens` | `4000` | 工作包和恢复包的 token 目标，用于 `rl context --purpose work/resume`、`rl resume`，以及 `rl delegate` 提示词里附带的工作包 |
| `context.advice_target_tokens` | `10000` | 建议请求包的 token 目标 |

- **token 数是估算值。** ASCII 字符数除以 4，每个非 ASCII 字符算 1。
- **超出目标的段落会被省略。** 段落按优先级加入，超出目标的不放进包里，只在包尾的 "Omitted (read on demand)" 列出名字。第一个段落总会保留。
- **`--budget` 可以单次覆盖目标。**
- **审查请求包不用这两个目标。** 代码审查包不复制代码，只列出被审文件的路径（在请求者的 checkout 里，可能是 worktree），审查者自己去读。请求时记下这些文件的摘要；回答到达时文件已经变了，这个回答照样记录，但不算数：它不算通过，也不覆盖任何 run，重新审查（`--recheck-of`）顶替它在这一轮里的位置。结论审查包从记录生成。

### 论文草稿

| 键 | 默认值 | 含义 |
|---|---|---|
| `manuscript.latex` | `null` | `null` 时 `rl manuscript` 写 Markdown 草稿 `docs/manuscript.md`。写成编译命令（字符串或列表，如 `latexmk -pdf -interaction=nonstopmode`）时改写 `docs/manuscript/manuscript.tex`，并在 `docs/manuscript/` 里运行这个命令，编译出 `manuscript.pdf` |

- **草稿由两部分组成。** `rl manuscript` 按论文的结构生成各节：摘要、引言、相关工作、方法、结果、讨论、结论和附录。每节有一个 `[write: ...]` 占位（LaTeX 里是 `\towrite{...}`），协调者在阶段结束时把它换成正文。从记录来的事实放在 `rl:begin X` 和 `rl:end X` 两个标记之间：假设、文献、契约字段、每个 arm 的结果表、带等级的证据、决定和记录编号。
- **每次运行只刷新标记之间的内容。** 标记以外的文字原样保留，包括正文和 LaTeX 导言区。删掉某一块的标记，这一块就不再生成。输出会列出还有占位的节。
- **事实错了改记录，不改块。** 块里的内容下次运行会被覆盖。
- **编译。** 命令里没有 `.tex` 参数时，CLI 在末尾加上 `manuscript.tex`。命令的标准输入是空的，超时 300 秒。编译失败时报 BUILD，给出 LaTeX 的错误行和日志路径；命令找不到时报 UNAVAILABLE。两种情况下 `.tex` 都已经写好。`docs/manuscript/.gitignore` 让 `.aux`、`.log` 这类辅助文件不进 git。
- **编译在哪里跑。** `rl manuscript` 在当前 shell 里直接运行编译命令，在集群的登录节点上也一样，不经过作业执行器。小篇幅的 LaTeX 编译很轻，但站点规定登录节点不许跑这类程序时，在允许的地方运行 `rl manuscript`（例如交互节点上），或者把 `manuscript.latex` 留空，只写草稿。
- **中文和日文。** 默认的导言区在 pdflatex 下用 T1 编码和 Latin Modern 字体，在 xelatex 和 lualatex 下用 fontspec，都不带 CJK 字体。正文是中文或日文时，用 xelatex 或 lualatex 编译，在导言区设定一个含中日文字形的字体。下面这套在 Miyabi 的计算节点上测过（TeX Live 带 fontspec，没有 xeCJK、ctex 和 luatexja，系统里只有 Droid Sans Fallback 一个中文字体），抽取检查能通过：
  - `manuscript.latex: xelatex -interaction=nonstopmode -halt-on-error`
  - 导言区（在 `\begin{document}` 之前）：`\setmainfont{DroidSansFallbackFull.ttf}[Path=/usr/share/fonts/google-droid-sans-fonts/]` 和 `\XeTeXlinebreaklocale "zh"`（让中文能在字间断行）。
  - 字体按文件指定。`fc-list :lang=zh file` 列出带中文字形的字体文件。按族名指定可能找错：这台机器上 `Droid Sans` 也是这个文件的族名之一，xelatex 按 `Droid Sans` 找到的却是只有拉丁字母的那个文件，PDF 里的中文全成了缺字。字体路径因机器而异，这里只是例子，不是默认值。
  - 装了 xeCJK 时，改用 `\usepackage{xeCJK}` 加 `\setCJKmainfont{...}`，西文仍用原来的字体。
  - 全角引号：“”在 Unicode 里属于西文标点，没有 xeCJK 时由主字体来排。Droid Sans Fallback 没有这两个字形，排出来是缺字（日志里有 Missing character）。可以改用「」，或者换一个带这两个字形的字体；有 xeCJK 时它按中文标点处理。
- **编译后的文字检查。** 源文件正文里有中文或日文时，编译成功后用 pypdf 抽出 PDF 的文字，从 `.tex` 正文（去掉注释）里均匀取最多 8 段、每段至少 4 个字的连续中日文字，去掉空白后在抽出的文字里查找，找不到的超过一半就报 BUILD。这能挡住“PDF 看着对、文字是乱码或缺字”的情况，比如上面字体找错的例子。源文件里没有中日文字时不查。
- **从 Markdown 换到 LaTeX。** 已有的 `docs/manuscript.md` 不会被转换，正文要自己搬过去。

### 计算资源

`compute` 一节写实验在哪里跑：本机，以及远程主机或集群。每个资源写清楚怎样连上去、用什么启动作业、远程的工作目录，以及代码怎样过去。rl 不连接任何资源，不复制文件，也不启动作业：它只读配置，检查本地能看到的东西，打印步骤，由 agent 用 Bash、ssh、pi-env-mcp 和提交作业的 MCP 或 skill 执行。唯一的例外是 git 这一侧：对 `code: git` 的资源，`rl compute sync`（协调者的 `rl session start` 会运行它）在本地推送当前分支，remote 不存在时先建一个 private GitHub 仓库（见下面的“开始时同步”）；主机上的 clone 仍然是打印出来、由 agent 执行的步骤。预算和队列限制不写在这里，写在约束文档里（第 2 节）。

专门针对协调者位于超级计算机登录节点的配置是 **`compute.coordinator: login`**。默认 `local` 保持普通本机工作流；`login` 禁止把 checkout 或 shell 资源登记为执行目标，登记必须明确指定 `--resource`。它是显式策略，不根据机器名称猜测，也不替代站点工具的 allocation 检查。

协调者已经在登录节点、代码与结果目录由计算节点共享时：

```yaml
compute:
  coordinator: login
  default: cluster
  repository: none
  resources:
    cluster:
      kind: remote
      connect: local
      jobs: mcp:cluster-jobs
      workspace: /shared/user/my-project
      code: archive
```

`kind: remote` 表示实验在另一个执行环境里运行；`connect: local` 表示协调者直接访问共享文件系统，不需要 SSH、上传或下载。共享目录必须在登录节点和计算节点上具有相同的绝对路径。MCP 的注册方式、启动程序、队列和账户都放在宿主或研究项目配置里，插件只引用服务名。

协调者在工作站、需要远程传输时，可以配置 `coordinator: local`，资源使用 `host: cluster-login`、`connect: ssh`（或 `pi-env-mcp:<服务名>`），其余作业工具与工作目录仍然独立配置。

| 键 | 默认值 | 含义 |
|---|---|---|
| `compute.coordinator` | `local` | `local`：允许普通本机工作流。`login`：协调者处于登录节点，所有 workload（含测试、smoke、构建和分析）通过配置的作业 MCP 或 skill；拒绝 checkout 执行和 `jobs: shell` 资源，登记必须带 `--resource`。未知值警告并按 `login` 处理，不回退成本机执行 |
| `compute.default` | `local` | 工作包和 `compute steps` 的默认资源；`run register` 不自动采用此默认值。普通 `local` 模式允许省略 `--resource`、不记录资源；`login` 模式拒绝省略 |
| `compute.repository` | `github-private` | `code: git` 的资源缺少 git remote 时，`session start` / `compute sync` 创建 private GitHub 仓库后推送。`none` 不创建。`connect: local` 不使用它，也不触发 push |
| `compute.resources` | `{local: {kind: local}}` | 资源名到设置的映射，整体替换默认值。名字只能用字母、数字、`.`、`_`、`-`。登录节点没有有效资源时停止提供执行步骤，不启用本机兜底 |
| `<资源>.kind` | 有 `host` 或 `connect: local` 时为 `remote`，否则 `local` | `local` 使用调用者 checkout，输出到 `research/artifacts/<attempt>/`；`remote` 在其他执行环境使用 commit 快照，包含共享目录上的集群作业 |
| `<资源>.host` | 无 | ssh 或 pi-env-mcp 的连接目标；`connect: local` 不需要 |
| `<资源>.connect` | `ssh` | `ssh`、`pi-env-mcp`、`pi-env-mcp:<服务名>` 或 `local`。`local` 要求作业 MCP/skill 及 `code: archive`，不会启动本机 workload。无效连接方式跳过该资源 |
| `<资源>.jobs` | `shell` | `shell`、`mcp:<服务名>` 或 `skill:<名字>`。`coordinator: login` 和 `connect: local` 均不允许 `shell`；无效值跳过资源，不回退到 shell |
| `<资源>.workspace` | 无，`remote` 必填 | 集群的绝对路径，包含 `snapshots/<commit>/`、`runs/<attempt>/`，code 为 git 时还有 `repo.git`。一个研究项目一个 workspace；需要项目目录的 executor 使用此目录 |
| `<资源>.code` | `connect: local` 时 `archive`，否则 `git` | `git` 推送后由远端 fetch；`archive` 从调用者 commit 导出，上传到远端，或直接生成共享文件系统上的只读快照。`connect: local` 仅支持 archive |
| `<资源>.git_remote` | `origin` | code 为 git 时本地推送用的 remote |
| `<资源>.git_url` | 本地 `git_remote` 的 URL | code 为 git 时主机 clone 的 URL，主机须有读权限 |
| `<资源>.notes` | 无 | 给 agent 看的说明；不能代替明确的 coordinator 策略 |

- **登录节点只做控制工作。** 编辑、研究记录、有限的检查和小型源码快照可以在登录侧进行；实验程序、测试、构建、分析及应用导入必须通过 executor 到计算 allocation。环境安装遵循站点工具规则。工作包、恢复包和 session hook 均传递此限制，研究者和 subagent 也须遵守。MCP 不可用时报告缺少工具，不本地回退。插件不是 shell 沙箱，也不自行验证或创建 allocation。
- **共享目录不走网络。** `compute steps` 打印本地 `git archive` 和只读快照步骤，源代码取自调用者 worktree 的指定 commit，和队列启动时的 HEAD 无关。仅小型源码可在登录侧 staging；大目录在 allocation 中准备。结果直接从共享目录导入，manifest 的 artifact `path` 使用绝对共享路径（相对路径仍按研究项目根目录解释）。

- **远程只跑快照。** 远程的 run 只执行一个完整 commit 的只读快照 `<workspace>/snapshots/<commit>/`。哪个分支、哪个 worktree 上的提交都可以，分支之后再动也不影响已经排队的作业。同一个 commit 只同步一次，重复执行步骤是安全的。RunSpec 本来就只接受完整的 commit id，不接受分支名。
- **`rl compute` 只检查本地。** 它列出各资源，检查 `ssh` 在不在 PATH 上、git remote 在不在，加 `--commit` 时还看这个 commit 是否已经推送（按最后一次 push 或 fetch，不访问网络）。pi-env-mcp、MCP 服务和 skill 是 agent 的工具，由 agent 自己检查。
- **`rl compute steps` 给出一次运行的全部步骤。** 带 `--attempt` 时用 attempt 记录的资源和 RunSpec 里的 commit；不带时用 `--commit`，再不带就用 checkout 的 HEAD。步骤依次是：推送分支（code 为 git，已经推送时说明），在主机上 fetch 并取出那个 commit 的脚本（code 为 archive 时是打包、传输、解开），启动方式和要设的环境变量（`SR_ATTEMPT_ID`、`SR_SPEC_DIGEST`），以及取回 `manifest.json` 和 `rl run import-result`。connect 为 ssh 时打印传输命令，为 pi-env-mcp 时写明服务和主机，为 local 时只准备共享快照并直接读结果。attempt 的 RunSpec 只有内容摘要、没有 commit 时，它说明远程的 run 要用 `--code-commit` 登记，而不是改用 HEAD。
- **`--resource` 把资源记到 attempt 上。** `rl run register ... --resource <名字>` 记下资源的设置和计划中的位置（远程的快照目录和 `runs/<attempt>/`），`rl run show` 显示 `on <名字>`。名字不在配置里时报 NOT_FOUND。资源名属于请求的一部分：同一个 `--request-id` 换了资源会报 CONFLICT。资源不属于 RunSpec，同一个 RunSpec 可以在不同资源上各跑一个 attempt。
- **开始时同步（code 为 git）。** 协调者的 `rl session start` 运行 `rl compute sync`（`--no-sync` 跳过）。对每个 `code: git` 的资源用到的 git remote：
  1. remote 不存在、`compute.repository` 是 `github-private`、项目自己就是一个 git 仓库（不是更大仓库里的一个目录）、也没有写 `git_url` 时，用 `gh repo create <项目目录名> --private --source <项目> --remote <git_remote>` 建仓库，记一条 `compute.repository` 事件。gh 不在、没有登录或重名时说明原因和手工的做法，不让会话启动失败。
  2. 用 `git push`（没有 upstream 时加 `-u`）推送当前分支，记一条 `compute.push` 事件。不强推；失败时说明原因。还没有 commit、HEAD 是 detached 时只说明，不推送。没有提交的修改不会过去，它也会提醒。
  3. 给每个资源打印一个脚本：在 `<workspace>/repo.git` bare clone 仓库（已经有就 fetch），并检查主机上有刚推送的 commit。agent 要立刻用这个资源的连接（pi-env-mcp 或 ssh）执行它，这样主机读不了仓库（要在主机上 `gh auth login`，或者配 deploy key）在第一次运行之前就能发现。重复执行是安全的。
  没有 `code: git` 的远程资源时，什么都不做，也不输出。
- **researcher 推送自己的任务分支。** code 为 git 时，researcher 在需要时用 `rl compute sync` 推送自己 worktree 的任务分支 `sr/<名字>/<任务>`，从不强推：记录过的 run 指向这些 commit。合并后的 main 由协调者用 `rl compute sync` 再推送一次。
- **工作包和恢复包带上资源列表。** researcher 和接手的会话不用读约束文档就知道去哪里跑。有远程资源时还提醒 Python 环境的做法：所有资源共用一份提交的 `pyproject.toml` 和 `uv.lock`（CPU、CUDA 等版本写成互斥的 extras，各个 extra 用同一个版本），只在本地改和 lock；主机上从快照建环境，放在快照外、按 lock 区分，用 `--environment-ref` 记下来。详见 skill 的 workflow.md（Python environments）。

## 2. 约束文档 `docs/constraints.md`

`rl init` 生成一个模板，有四项要填：

- 研究目标和结论范围；
- 允许的数据，以及哪些计算资源可以用于什么；
- 资源上限（每个实验、每批、总量）；
- 需要用户先决定的动作。

模型选择和计算资源不在这里：它们写在 `smooth-research.yaml` 的 `models` 和 `compute` 里，由 CLI 读取。

这份文档的作用：

- **CLI 不解析、不执行它。** 资源预算、可用的队列和各资源的使用范围只写在这里。`smooth-research.yaml` 的 `compute` 只写资源在哪里、怎样访问、代码怎样过去，不放预算和队列限制（设计文档 §13.1）。
- **各请求包怎样引用它。** 工作包只给出它的路径，提醒 agent 遵守。建议请求包会附上它的前 3000 个字符。它的摘要还参与建议请求的去重。

## 3. 环境变量

| 变量 | 含义 |
|---|---|
| `SMOOTH_RESEARCH_PROJECT` | 项目目录。查找顺序：先看 `--project`，再看这个变量，最后从当前目录往上找 `smooth-research.yaml` |
| `SMOOTH_RESEARCH_HOME` | 插件自己的 CLI 环境的安装位置（项目外的命令和还没有自己环境的旧项目用它；项目内的命令用项目的 `.venv`，见第 6 节）。设了 `XDG_DATA_HOME` 时默认为 `$XDG_DATA_HOME/smooth-research`，否则为 `~/.local/share/smooth-research`。`rl setup` 在这里建 `env-<build-id>`；Claude 的额度读数存在这里的 `usage/claude.json` |
| `SMOOTH_RESEARCH_RL` | `bin/rl` 启动器自动把它设成自己的绝对路径，让 hook 提示、delegate 子会话和 agent 会话调用同一个 CLI。一般不用手动设 |
| `SMOOTH_RESEARCH_BUILD` | `bin/rl` 启动器设成插件的 build id（源码里运行时没有，按 `dev` 计算）。项目记下最后一次 `rl upgrade` 时的 build；两者不同时，`rl status`、`rl resume` 和会话开始的提示会说插件变了，要运行 `rl upgrade`。一般不用手动设 |
| `SMOOTH_RESEARCH_CHECKOUT` | `rl agent open` 给 researcher 会话设成它的 worktree。代码路径、代码审查的指纹和 `rl run register` 的代码检查都按这个 checkout 读；研究记录仍写进 `SMOOTH_RESEARCH_PROJECT` 指向的项目。不设时从当前目录判断：在项目的 linked worktree 里就用这个 worktree |
| `SMOOTH_RESEARCH_SESSION`、`SMOOTH_RESEARCH_ROLE` | `rl agent open` 给每个会话设成它的会话记录（S-x）和会话名（如 `reviewer-1`）。设了 `SMOOTH_RESEARCH_ROLE` 的会话不能批准审查轮次（`rl review approve`），发消息时默认以这个名字发送 |
| `HERDR_ENV`、`HERDR_PANE_ID` | herdr 设置，CLI 只读。`HERDR_ENV=1` 时才能开 agent 会话、发通知；协调者 `rl session start` 时记下 `HERDR_PANE_ID`，通知发到这个 pane |
| `CODEX_HOME` | 默认 `~/.codex`。CLI 从其中的 `sessions/` 读 Codex 实际使用的模型和订阅额度。如果其中的 `config.toml` 设了 `sandbox_mode = "danger-full-access"`，`rl delegate` 启动的可写 Codex 会话也用这个模式，否则用 `workspace-write` |
| `CLAUDE_CONFIG_DIR` | 默认 `~/.claude`。`rl session start --role coordinator --host claude` 看其中的 `settings.json` 里有没有用户自己的状态栏 |

## 4. 命令行选项

- **全局选项：** `--project`、`--json`（输出完整 JSON）、`--actor`（写进事件日志的名字）、`--read-only`。`--project`、`--json` 和 `--read-only` 也可以写在子命令后面。
- **只读：** `rl --read-only <命令>` 什么都不改：不升级旧格式的记录（记录格式比 CLI 旧时说明要先升级，然后退出），不更新项目的 Python 环境（launcher 不跑 uv add，用现有的 `.venv`，没有时用插件自己的环境），不写记录、文件和缓存，锁文件不存在时也不创建。会写东西的命令直接拒绝（READ_ONLY）。能用的有 status、resume、show、history、graph、context、doctor、digest、inbox（不带 `--ack`）、hypothesis/evidence/task/paper list、run/review/advise show、agent brief/list、compute steps、`view --todo` 和 `export --records-only`。用来查看别人的项目或者项目的副本。
- **单次覆盖：**
  - `--budget`：`rl context` 和 `rl resume` 的 token 目标；
  - `--entry`：`rl review run`、`rl advise run`（只能给一个工单）、`rl delegate` 和 `rl agent open` 只用这一个模型；
  - `--timeout`：模型调用超时，审查和建议默认 1800 秒，`rl delegate` 默认 3600 秒；`rl agent open` 等会话启动，默认 90 秒；
  - `--background`：在后台进程运行。
- **一轮建议一起运行。** `rl advise run AD-x AD-y ...` 给每个工单开一个进程，同时运行。
- **超出建议轮数：** `rl advise prepare --user-approved "<用户的原话>"`，见第 1 节。
- **审查轮次：** `rl review request ... --recheck-of RV-x` 是同一轮的第二次审查；第三次报 LIMIT，要开新一轮写 `--new-round "<原因>"`。`rl review approve RV-x --rationale ..` 由协调者批准还有中等以上问题的一轮：接受剩下的问题，这次审查从此算通过，它看过的文件都算审过。
- **被复查解决的轮次：** `rl review resolve RV-x --by RV-y --reason ..` 记下 RV-x 的问题已由通过的复查 RV-y 解决。RV-y 必须沿 `--recheck-of` 链在 RV-x 之后（新一轮的复查也算），并且通过。RV-x 从 attention 移走，结论不变，仍然不算通过，它看过的文件不因它算审过。这类轮次的 attention 条目会写出已通过的复查和 resolve 的写法；对这样的轮次 approve 时，输出提示改用 resolve。只有协调者能 resolve。
- **run 的审查覆盖：** `rl run register` 检查通过的代码审查看过的每个路径，在这次 run 执行的代码（提交，没有提交时是工作树）里是否还和某次通过的审查看过的版本一致。对不上的路径各有原因，`rl run register` 和 `rl run show ATT-x` 逐行列出：
  - **changed。** 内容变了，只有这一种让状态变成 stale。项目里另一个实验的通过审查看过现在这份内容时，写出它的编号：确认它回答的也是这个实验的问题后，加 `--review RV-x` 再登记。不自动算作覆盖。
  - **absent。** 代码里没有这个路径，run 执行不到，只列出。
  - **test。** 测试文件（在 `tests/` 目录下，或名为 `test_*.py`、`*_test.py`、`conftest.py`）改了，run 计算的东西不变，只列出。只按名字判断，所以逐个写出文件名，名字像测试的生产文件一眼能看出来。
  - **其他。** 通过的审查看过的路径都不在代码里或都是测试文件时，记为没有审查。覆盖来自多次审查、没有一次看过全部文件时注明“file by file”，表示这些文件的组合没有一起审过。这个实验没有通过、之后也没有通过的复查的审查，单独列为 not passed。
- **契约随引用封存：**
  - **封存。** `rl task create --targets E-x` 和 `rl review request --target E-x` 封存实验现在的契约草稿，并把修订和封存的文档记在关系上。草稿里还有空字段或 TODO 时，命令报错，列出要填的字段，什么也不封存。只指向假设的任务不封存它下面的实验。
  - **之后的修订。** 契约之后有了新修订，任务照常进行。任务的工作包（`rl agent brief`、`rl context --purpose work`）列出任务绑定的修订、现在的修订、两者正文的差异（最多 80 行，`DIFF_LINES`）和草稿路径。已经登记的 run 仍绑定登记时的修订。
- **结论审核绑定修订：**
  - **记录。** `rl review conclusion` 记下每个假设在审核材料里的修订，以及它看到的证据。
  - **判断缺口。** `rl decide` 和 attention 只认修订与决定所针对的修订一致的通过审核。不一致时，缺口写明审核针对的修订和现在的修订：重新审核，或者在决定的理由里说明为什么这次修订不改变结论。
  - **之后的证据。** 审核之后才挂到假设上的证据只提示（decide 的输出和 attention），不算缺口。
- **agent 会话：** `rl agent open [名字] --role researcher|reviewer|advisor [--task T-x | --ticket RV-x/AD-x] [--entry ..] [--no-worktree] [--base ..]`，以及 `assign`、`tell`、`reset --compact|--clear`、`close [--remove-worktree]`、`switch <名字> [--entry ..] [--now]`（换一个模型继续这个会话的工作，见第 1 节“订阅额度”；`--now` 连还在工作的会话也换）、`brief`、`list [--all]`、`dialog <名字>`（会话现在显示的对话框和选项）、`answer <名字> <选项编号或文字> --reason ..`（协调者回答对话框）。
- **订阅额度：** `rl agent list` 显示各宿主的窗口；`rl usage statusline` 是 Claude Code 的状态栏命令，从标准输入读状态栏的 JSON，任何输入都不会让它失败。不带参数的 `rl usage` 报错并指向 `rl agent list`。
- **层次编号：** 假设和实验除了内部编号（H-0013、E-0016）还有创建时定下、以后不变的层次编号。`rl hypothesis add` 默认挂在最晚创建的假设来源下（H2.1 在 H2 下），`--parent <来源之一>` 指定另一个，`--root` 开一个新的顶层编号（开启新研究线时用，来源关系照常记录）；只源自论文或顾问回答的是顶层。`rl experiment add` 挂在所检验假设的最深共同祖先下（EH2.1/3 是锚在 H2.1 的第三个实验），没有共同祖先时取 `--hypotheses` 的第一个，`--anchor` 指定另一个假设。凡是接受假设或实验编号的参数都接受两种写法，也可以写在逗号列表里或带 `@修订`；未知的层次编号报 NOT_FOUND。`rl status`、各类列表、报告和草稿的记录块只用层次编号，`rl show` 两种都显示，`--json` 有 `label` 字段；路径、作业名和提交信息用内部编号。
- **论文草稿：** `rl manuscript [--title ..]` 写或刷新草稿，`--title` 只用于新建的草稿，见第 1 节"论文草稿"。
- **研究说明页：** `rl view` 渲染 `docs/research.html`，事实和文字分开：
  - **事实来自记录。** 包括层次编号、标题、状态、关系、契约指标、各 arm 的数字、成本、决定和各类计数，每次渲染都在一个读事务里现取。
  - **文字来自 `research/notes/explanation.yaml`。** 由协调者或它派的写作者写，rl 只读不改。文件有三部分：`lines`（研究线的 `key`、`name`、`desc`、`roots`）、`summary`（`text`，只写判断，不写数字）、`nodes`（键用层次编号或内部编号，值是 `title`、`purpose`、`did`、`result`）。
  - **页面的组织。** 研究线下按层次编号排出假设树，实验挂在锚点假设下；其他来源和被检验的假设在节点里列出并加链接。没分到研究线的顶层假设放进 Unclassified，没写文字的节点照样显示事实，标 "explanation to write"。
  - **自包含、确定性。** 页面不加载外部资源，不含渲染时间和 data_version；内容没变就不写文件。
  - **`rl view --todo`** 列出要写和要改的说明，附上它们现在的事实。
  - **`rl view --stamp H3,EH3.3/3[,summary]`** 记下这些说明核对过的事实摘要，写进 rl 自己的 `research/notes/explanation.basis.yaml`，然后渲染。摘要只在相关事实变化时改变：假设看陈述、修订、状态、证据及其 validity 和决定；实验看契约修订、stage、attempt 状态计数、结果和决定。消息和心跳不会让它变。摘要变了，节点标 "may be out of date"。导语核对的是阶段决定，出现更新的阶段决定就算过期。
  - **自动重渲染。** 页面存在时，`hypothesis`、`experiment`、`evidence`、`decide`、`link` 和 `run import-result/reject-result` 结束后重渲染一次；失败只给警告，命令本身照常完成。`run register/record/usage` 很频繁，页面只统计它们，所以不触发渲染；attempt 状态和成本等下一次渲染（或 `rl view`）时更新。
  - **提醒。** `rl status`、`rl resume` 和会话开始的提示只给一条汇总：几处待写、几处可能过期。说明缺失或过期不阻塞 decide。
  - **实验表格。** 按契约修订、arm、数据版本、模型版本和配置分组，条件不同的结果分行列出，不做跨条件平均；只有变化的条件才单列一栏。每个 run 只取一个结果：最后一个 complete，没有时才用 partial，并在 notes 里标出。被拒绝的结果不算；和 RunSpec 不符的结果标出来，不进均值。组内代码提交不同时也标出来。契约声明了 `metrics:` 就只列这些指标；没有时先列契约和 RunSpec 的主指标，其余按字母序，最多 6 个。成本分开写已报告、共享作业和未知。`rl report`、论文草稿和结论审核包用同一张表。
- **插件升级：** `rl upgrade` 显示项目的记录格式和插件 build、做过的升级、新版本读法不同的地方和可以自己调整的范围；`--note "<改了什么>"` 记下 agent 手工做的调整；`--move <旧> <新>` 移动旧布局里的文件或目录，并在同一个事务里改写记录里的路径。见第 6 节。
- **`rl init [--python 3.13]`：** 记录由项目自己的 Python 环境生成，所以顺序是（与 research-tree 的 prepare + init 相同）：
  1. 在项目目录运行 `uv init . --no-workspace --python=<版本> --name <目录名>`：写 `pyproject.toml`、`.python-version` 和 `src/<包名>/__init__.py`；项目不在任何 git 仓库里时 uv 还会 `git init`。已经有 `pyproject.toml` 时跳过。`--no-workspace` 让放在别的 uv 项目里面的研究项目（比如一个仓库里的 demo）保持独立，不被加进那个项目的 workspace。
  2. `uv add` rl 的依赖（`latticedb`、`pyyaml`，取自插件的 `pyproject.toml`）：写进项目的 `pyproject.toml` 和 `uv.lock`，并建好 `.venv`。项目已经在一个 git 仓库里时 uv 不写 `.gitignore`，rl 写 uv 的那份（或者在已有的 `.gitignore` 后面加 `.venv`）。
  3. 用项目的 `.venv/bin/python` 运行 init：生成配置、目录和数据库，导入计划。init 的输出和 `project.init` 事件写明是哪个环境（Python 版本、latticedb 版本）。

  前两步在 `bin/rl` 启动器里用 shell 完成，不需要 Python。`--python` 低于 rl 要求的版本（3.13）时拒绝。uv 失败时 init 停下，不生成记录。之后把新项目提交一次（包括 `pyproject.toml` 和 `uv.lock`），`rl session start` 才有东西可推送。
- **计算资源：** `rl compute [--commit <sha>]` 列出资源和本地检查；`rl compute steps [<资源>] [--attempt ATT] [--commit <sha>]` 给出同步、运行和取回结果的步骤；`rl compute sync [<资源>]` 推送当前分支并打印 clone 步骤（见第 1 节“开始时同步”）；`rl session start --no-sync` 跳过它；`rl run register ... --resource <资源>` 把资源记到 attempt 上。见第 1 节“计算资源”。
- **监视程序：** `rl agent list` 只查看，不动作（每个会话的 herdr 状态、类别、在等什么、多久没有进展、最后的错误、重试次数、下一步，以及各宿主的额度）；`rl agent check` 立刻走一拍（监视程序正在走一拍时跳过）；`rl agent watch --start` 在单独的 tab 里启动（已经在运行就不重复启动），`--stop` 停止并关掉它的 tab；`--every <分钟>` 改间隔，不写就用 `agents.watch_minutes`。不带 `--start` 或 `--stop` 时在当前终端前台运行。

## 5. 写死的常量（不能配置）

| 常量 | 值 | 含义 |
|---|---|---|
| `LOCK_TIMEOUT_S` | 60 秒 | 读写等锁的上限，超时报 LOCKED，写明等了多久、锁被哪个进程占着（写入方把自己记在锁文件里）。什么都没写入，不自动重试（`store.py`） |
| `STALE_AFTER_H` | 6 小时 | 未结束的 attempt 超过这么久没有新观察，描述里会加一句 "stale?" 提示（`runs.py`） |
| `REVIEWS_PER_ROUND` | 2 | 一轮审查最多几次。第二次后还有中等以上问题，就交给协调者批准或退回（`reviews.py`） |
| `NOTICE_CHARS` | 600 字符 | 发给 herdr 会话的一行通知里，消息正文最多保留多少字符；完整消息在 `rl inbox`（`herdr.py`） |
| `MAX_RESTARTS` | 一小时 3 次 | 监视程序一小时内最多重启同一个会话几次，再退出就交给协调者（`watch.py`） |
| `BUILD_TIMEOUT` | 300 秒 | `rl manuscript` 运行一次 LaTeX 编译命令的上限（`manuscript.py`） |
| `MAX_REMINDERS` | 2 次 | 同一项任务，监视程序最多提醒几次；之后就报告协调者（`watch.py`） |
| `MAX_RETRIES` | 3 次 | 一串暂时性的回合错误最多重试几次，分别在下一拍、隔 2 拍、隔 4 拍（`RETRY_BEATS`，`watch.py`） |
| `TOOL_STALLS` | 4 倍 | 一个工具调用挂起超过 `agents.stall_minutes` 的这么多倍时报告一次（`watch.py`） |
| `SUBMIT_MS` | 20 秒 | 发送提示后等 herdr 看到 working 或 blocked 的上限（`herdr.py`） |
| `CELL_CHARS` | 60 字符 | 实验表格里单元格的长度上限，超出的截断；完整内容见 `rl run show`（`view.py`） |
| `MAX_METRICS` | 6 个 | 契约没有声明 `metrics:` 时，实验表格最多列几个指标（`view.py`） |
| `DIFF_LINES` | 80 行 | 工作包里契约差异的行数上限，超出的部分用 `rl show` 看两个修订（`context.py`） |
| `STALE_MINUTES` | 15 分钟 | 比这更旧的额度读数算过期，不让宿主算作额度低（`usage.py`） |

## 6. 插件升级

插件升级不会让进行中的研究停下来。

- **项目的环境。** 研究项目里的 `rl` 命令都在项目自己的 `.venv` 上运行：代码来自插件（`src/`，不装进项目），依赖（`latticedb`、`pyyaml`）是项目 `uv.lock` 锁定的版本。researcher 的 worktree 里也用主 checkout 的 `.venv`。插件升级后 rl 的依赖变了时，项目里第一条命令先 `uv add` 新的依赖（几个命令同时来时只做一次），并提示提交 `pyproject.toml` 和 `uv.lock`；会话开始的 hook 在后台做，状态栏不等它。删掉 `.venv` 也没关系，下一条命令会重建。
- **插件自己的环境。** 每个插件版本有一个（`$SMOOTH_RESEARCH_HOME/env-<build>`），只给研究项目以外的命令（比如别处的会话 hook、状态栏）和还没有自己环境的旧项目用。会话开始的 hook 在后台开始构建，第一条要用它的命令等它构建完；几个会话同时启动时只构建一次。`rl setup` 强制重建它，在项目里还会更新项目的环境。需要 uv。
- **旧项目。** 在 `pyproject.toml` 里没有 rl 依赖的项目（这个版本之前建的）照常在插件自己的环境上运行；`rl upgrade` 的 `environment:` 一行会说明。在项目根目录运行 `rl init` 就给它建项目环境（没有 `pyproject.toml` 时先 `uv init`），记录不变。
- **记录自动升级。** 新版本的 CLI 第一次打开旧项目时，在写锁下先把数据库文件复制到 `research/state/backups/schema-<旧格式>-<时间>/`，再在一个事务里运行旧格式到当前格式之间的全部迁移，记一条 `project.upgrade` 事件。迁移只补充和填写，不删除记录，不改记录下来的事实，不碰封存的文档。做了升级的那条命令会给出一条警告。`rl restore` 也接受旧版本的快照，恢复后同样自动升级。
- **层次编号的迁移（记录格式 3）。** 按内部编号顺序用新建记录的默认规则补上层次编号：假设只看比它先建的假设来源，实验只看建它时已有的被检验假设，没有共同祖先时取编号最小的那个。迁移不知道哪些假设开过新研究线（`--root`），所以旧项目的编号会偏深。这次升级还留在 `rl upgrade` 的历史里时，`rl upgrade` 打印对照表。
- **结论审核的修订（记录格式 4）。** 迁移按事件日志补上旧结论审核所审的假设修订，也就是请求审核时的修订。旧审核看到了哪些证据没有记录，所以它们不给“之后的新证据”提示。旧任务没有绑定契约修订，工作包只列现在的修订。
- **旧的图页面。** 以前的 `rl view` 写 `docs/research-graph.html`（mermaid 图，从 CDN 加载）。现在 `rl view` 写 `docs/research.html`，旧文件不删也不再更新，`rl upgrade` 会提示，不需要时手动删掉。
- **更新的项目只读。** 项目由更新的插件写过（记录格式比 CLI 新）时，CLI 照常读取，但拒绝写入（UNSUPPORTED），提示更新插件。
- **`rl upgrade`。** 插件 build 和项目记下的不同时，`rl status`、`rl resume` 和会话开始的提示都会提醒。`rl upgrade` 补建新布局要的目录，列出旧布局放在别处的文件、配置警告、找不到的合同文件和记录问题，并说明 agent 可以自己调整什么；运行后记下当前 build，提醒消失。
- **agent 自己调整旧资产，不用问用户。** 可以改配置文件、只是格式变了的约束文档措辞、旧布局里的文件位置、未封存的合同草稿、任务说明、笔记、worktree 和分支，也可以用 `rl` 命令补上新版本需要的记录。只有会改变研究目标、结论范围、数据或预算的调整才问用户。记录下来的事实（观察、结果、证据、决定、审查）和封存的文档不改：新版本要求另一种形式时，新增一条引用旧记录的记录或修订。改完用 `rl upgrade --note` 记下来并提交。
- **移动旧布局的文件。** 记录里存着合同和文档的路径，直接 `mv` 会让记录指向不存在的文件。`rl upgrade --move <旧> <新>` 移动文件或目录，并在同一个事务里改写这些路径；事务失败时文件移回原处。文件当初从哪里封存（修订记录、事件）保持原样。数据库和封存对象（`research/state/`、`research/documents/`）不能移动。父目录里还有另有去处的旧子目录时（比如 `reports/reviews`），先移子目录。
- **开发规则。** 改记录格式时提高 `SCHEMA_VERSION`，在 `upgrade.MIGRATIONS` 里加迁移，并在 `tests/fixtures/projects/` 冻结一份新格式的项目；旧的冻结项目保留，测试用每一份恢复项目、运行全部命令。不要删除读取旧项目的能力。
