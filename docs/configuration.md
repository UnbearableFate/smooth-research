# Smooth Research 配置说明

配置分四处：项目配置文件 `smooth-research.yaml`、约束文档、几个环境变量、命令行选项。程序读取的只有 `smooth-research.yaml`。约束文档写给人和 agent 看，CLI 不执行它。

## 1. 项目配置 `smooth-research.yaml`

它放在研究项目的根目录，由 `rl init` 生成。默认值在 `src/smooth_research/project.py` 的 `DEFAULT_CONFIG`。

### 加载规则

配置里的任何问题都不会让命令失败。CLI 用能用的值继续运行，并给出警告，说明它实际用了什么。

- 文件里的值和默认值深度合并：字典逐键覆盖，列表整体替换。比如 `models.reviewer` 只写一个条目，默认的两个就都没了。
- 警告出现在三个地方：
  - 普通输出末尾的 `warning:` 行；命令失败时，警告和错误一起打印，它们可能正好说明失败的原因；
  - `--json` 结果里的 `warnings`；
  - 会话开始时的 hook 提示。
- 下面这些情况会警告：
  - **文件解析不了**（YAML 写错，或者顶层不是键值对）：沿用上一次成功读取的设置，直到文件改好。这份设置由 CLI 自动保存在 `research/state/config.last-read.json`；从没读成功过时用默认值；
  - `version` 不是 `1`：按版本 1 读取；
  - 不认识的键（拼错了？）：没有作用；
  - 旧版本写进配置、现在不再读取的键，包括以前的目录键（见本节最后）；
  - 某一节（如 `models`、`context`）不是键值对：这一节全用默认值；
  - 模型列表还是 2026-10-31 以前的写法（见本节最后）：照旧读取；不是列表：用默认列表；某个条目格式不对：跳过这一条，用列表里其余的条目；
  - `advice.max_rounds` 不是非负整数：按 3 计算；
  - `agents.watch_minutes` 不是分钟数：按 20 分钟计算；
  - `agents.args.<宿主>` 不是参数列表：用默认参数（写成一个字符串时，按 shell 的规则拆开，不警告）；
  - `context` 的 token 目标不是正整数：用默认值；
  - `manuscript.latex` 不是一条命令：写 Markdown 草稿。

### 目录（固定）

目录不能配置，所有项目都一样（2026-10-31 起）。路径都相对于项目根目录。

| 位置 | 内容 |
|---|---|
| `src/` | 实验代码。init 会创建它，并放一个忽略 `__pycache__` 和 `*.pyc` 的 `.gitignore`。工作包和 `rl delegate` 的提示词让 agent 把代码放这里，但不强制，审查范围和 RunSpec 可以引用任何路径 |
| `docs/` | 只放给人读的文档：`plan.md`（`rl init --plan` 导入的原始计划，封存）、`constraints.md`（约束文档，见第 2 节）、`research-graph.html`（`rl view`）、论文草稿（`rl manuscript`，见下面的"论文草稿"）、`explanations/`（用户让 agent 解释某件事时，agent 把说明写在这里） |
| `research/` | 其余的一切。agent 的工作文档：`experiments/`（实验契约）、`tasks/`（任务说明和交接笔记）、`notes/`（researcher 的笔记，写在它自己的 checkout 里，随分支合并）、`reviews/` 和 `advice/`（审查和建议的回答）、`reports/`（`rl report` 的阶段报告）。机器记录：数据库 `state/research.lattice` 和写锁、封存的文档 `documents/objects/`、请求包 `packages/`、模型调用日志 `calls/`、run 产物 `artifacts/<attempt>/` 等。init 写的 `.gitignore` 让 `state/`、`calls/`、`artifacts/` 不进 git |

- **导入的计划不能换。** 再导入不同的计划会报 CONFLICT。判断依据是已经封存的计划，和文件现在在哪里无关。计划的变化要记成假设修订和决定。
- **旧项目。** 2026-10-31 以前生成的项目把计划和约束放在 `docs/plan/original.md` 和 `docs/plan/constraints.md`。新位置没有文件时，CLI 继续读旧位置。旧项目的 `reports/` 目录留在原处，里面回答的封存副本在 `research/documents/objects/`；新的回答写进 `research/reviews/` 和 `research/advice/`。旧配置里的目录键见"旧版本的写法"。

### 模型

三个角色各有一个模型列表，每一项是一个模型条目，格式是 `<入口>:<模型>[@effort]`：

- **`claude`**：用 `claude -p` 调用。审查和建议是只读会话，只开放 Read、Grep、Glob 三个工具。
- **`codex`**：用 `codex exec` 调用。审查和建议在 read-only 沙箱里运行。
- **`pi`**：用 `pi -p --mode json` 调用。模型写成 `<provider>/<模型>`，比如 `pi:openai/gpt-6-astra@high`。审查和建议只开放 read 工具。
- **`manual`**：不自动调用。CLI 给出请求包的路径，由人提交给模型，再用 `rl review import` 或 `rl advise import` 导入回答。
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
| `agents.watch_minutes` | `20` | 监视程序每隔几分钟查看一次各个会话；`0` 表示关闭 |

- **CLI 自己加的参数只有模型、effort、会话 id 和名字。**
  - Claude：`--model`、`--effort`、`--session-id`、`-n`；reviewer 另加 `--disallowedTools Edit,Write,NotebookEdit`。
  - Codex：`-m`、`-c model_reasoning_effort="<effort>"`；advisor 另加 `--search`。
  - pi：`--model`、`--thinking`、`--session-id`、`--name`。
  - 不加任何绕过沙箱、审批或权限的参数。
- **researcher 的 worktree。** 每个 researcher 会话一个 git worktree，在 `<项目目录>/../<项目名>-worktrees/<会话名>`。每个任务一个分支 `sr/<会话名>/<任务>`，从主 checkout 当前的分支建（`--base` 可以改）。项目必须是至少有一次提交的 git 仓库，否则用 `--no-worktree`。研究记录总是写进主项目的 `research/`。
- **信任文件夹的问题由 CLI 回答，其他询问由协调者判断。** Claude Code 和 Codex 在新目录里会先问是否信任这个文件夹。如果问的正是 `rl agent open` 开会话的目录（会话的 worktree，或者用户启动协调者时已经信任的项目目录），CLI 就替协调者选“信任”，并记一条 `session.trusted` 事件。Codex 把信任记在主仓库的根目录下（`~/.codex/config.toml` 的 `[projects]`），之后同一项目的 worktree 不再问。
- **各角色的询问和许可请求交给协调者，不转给用户。** 角色会话用 `rl agent tell coordinator` 提问或请求许可；会话里的其他对话框（审查 hooks、启用 MCP 服务器、审批、问别的目录的信任问题）由 `rl agent open`、没能送达的通知或监视程序报告给协调者。协调者能判断的就直接判断并回答：问题用 `rl agent tell` 回答；对话框用 `rl agent dialog <名字>` 查看选项，用 `rl agent answer <名字> <选项> --reason ..` 选择并记下理由（`session.answered` 事件）。记录不足以判断时选稳妥的选项。只有协调者不能判断的才问用户：超出范围、预算或约束文件留给用户的动作，超过 `advice.max_rounds` 的建议轮，用户的账户和凭据（登录、安全密钥、付款），以及项目以外不可撤销的操作。角色会话不能回答对话框。
- **监视程序。** `rl agent open` 同时在单独的 tab（`sr-watch`）里启动监视程序，每 `agents.watch_minutes` 分钟查看一次所有打开的会话：
  - agent 退出了，或者 tab 被关掉了：在原来的目录里重新启动，开一个新对话，从 `rl agent brief` 读回任务。一小时最多重启 3 次；
  - 空闲、任务没做完、又没在等什么：提醒它继续。同一项任务最多提醒 2 次，之后报告协调者；
  - 正常的暂停不唤醒：任务记为 waiting（`rl task wait`，比如等集群作业）或 blocked；在等自己申请的审查；任务所测实验的 run 还处于 queued 或 running；发出的消息还没有回复（发给协调者的，以协调者还没读为准）；
  - 停在对话框：只回答该会话自己目录的信任问题，其他对话框报告一次。

  每轮的发现汇成一条来自 `watch` 的 NOTE 发给协调者。协调者空闲、又有消息一整轮没读时，提醒一次。没有打开的会话时，监视程序自己退出。正常结束一个会话要用 `rl agent close`；用别的方式关掉的会话算作意外停止，会被重新启动。
- **审查轮次。** 每轮最多两次审查（第一次加一次复查），是写死的常量，见第 5 节。

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
- **审查请求包不用这两个目标。** 它有自己写死的上限，见第 5 节。

### 论文草稿

| 键 | 默认值 | 含义 |
|---|---|---|
| `manuscript.latex` | `null` | `null` 时 `rl manuscript` 写 Markdown 草稿 `docs/manuscript.md`。写成编译命令（字符串或列表，如 `latexmk -pdf -interaction=nonstopmode`）时改写 `docs/manuscript/manuscript.tex`，并在 `docs/manuscript/` 里运行这个命令，编译出 `manuscript.pdf` |

- **草稿由两部分组成。** `rl manuscript` 按论文的结构生成各节：摘要、引言、相关工作、方法、结果、讨论、结论和附录。每节有一个 `[write: ...]` 占位（LaTeX 里是 `\towrite{...}`），协调者在阶段结束时把它换成正文。从记录来的事实放在 `rl:begin X` 和 `rl:end X` 两个标记之间：假设、文献、契约字段、每个 arm 的结果表、带等级的证据、决定和记录编号。
- **每次运行只刷新标记之间的内容。** 标记以外的文字原样保留，包括正文和 LaTeX 导言区。删掉某一块的标记，这一块就不再生成。输出会列出还有占位的节。
- **事实错了改记录，不改块。** 块里的内容下次运行会被覆盖。
- **编译。** 命令里没有 `.tex` 参数时，CLI 在末尾加上 `manuscript.tex`。命令的标准输入是空的，超时 300 秒。编译失败时报 BUILD，给出 LaTeX 的错误行和日志路径；命令找不到时报 UNAVAILABLE。两种情况下 `.tex` 都已经写好。`docs/manuscript/.gitignore` 让 `.aux`、`.log` 这类辅助文件不进 git。
- **中文和日文。** 默认的导言区在 pdflatex 下用 T1 编码和 Latin Modern 字体，在 xelatex 和 lualatex 下用 fontspec，都不带 CJK 字体。正文是中文或日文时，用 xelatex 或 lualatex 编译，并在导言区加载 xeCJK 或 luatexja。
- **从 Markdown 换到 LaTeX。** 已有的 `docs/manuscript.md` 不会被转换，正文要自己搬过去。

### 旧版本的写法

2026-10-31 以前生成的配置文件可能有下面的写法。命令照常运行，每条命令都会警告，按最后一栏改写即可：

| 旧写法 | 现在怎样读 | 改成 |
|---|---|---|
| `models.researcher: inherit` | 默认的 researcher 列表 | 模型条目的列表 |
| `models.reviewer: {candidates: [...], relation_to_author: different_family}` | 只读 `candidates`，`relation_to_author` 不再起作用 | `models.reviewer: [...]` |
| `models.advisor: {primary: X, fallback: [...]}` | 只读 `primary` | `models.advisor: [...]`，列出每一轮都要咨询的模型 |
| `artifacts.content_digest: blake2b`、`artifacts.keep_raw_evidence: true` | 不读：摘要算法写死为 blake2b，CLI 从不删除原始产物 | 删掉 |
| `plan`、`constraints_ref`、`code_root`、`docs_root`、`reports_root` | 不读：目录固定（见"目录"）。旧位置的计划和约束文档仍然读得到 | 删掉。`code_root` 不是 `src` 时，把代码移到 `src/` |
| `research_root` | 不读，只警告。记录总在 `research/`；改过名的旧项目在改回之前找不到记录，命令报 NO_PROJECT，同时打印这条警告 | 把记录目录改名为 `research/`，再删掉这个键 |

## 2. 约束文档 `docs/constraints.md`

`rl init` 生成一个模板，有四项要填：

- 研究目标和结论范围；
- 允许的数据和环境；
- 资源上限（每个实验、每批、总量）；
- 需要用户先决定的动作。

模型选择不在这里：它写在 `smooth-research.yaml` 的 `models` 里，由 CLI 读取。

这份文档的作用：

- **CLI 不解析、不执行它。** 资源预算、允许的集群这类限制只写在这里。`smooth-research.yaml` 不放执行 profile、队列或预算（设计文档 §13.1）。
- **各请求包怎样引用它。** 工作包只给出它的路径，提醒 agent 遵守。建议请求包会附上它的前 3000 个字符。它的摘要还参与建议请求的去重。

## 3. 环境变量

| 变量 | 含义 |
|---|---|
| `SMOOTH_RESEARCH_PROJECT` | 项目目录。查找顺序：先看 `--project`，再看这个变量，最后从当前目录往上找 `smooth-research.yaml` |
| `SMOOTH_RESEARCH_HOME` | 插件 CLI 环境的安装位置。设了 `XDG_DATA_HOME` 时默认为 `$XDG_DATA_HOME/smooth-research`，否则为 `~/.local/share/smooth-research`。`rl setup` 在这里建 `env-<build-id>` |
| `SMOOTH_RESEARCH_RL` | `bin/rl` 启动器自动把它设成自己的绝对路径，让 hook 提示、delegate 子会话和 agent 会话调用同一个 CLI。一般不用手动设 |
| `SMOOTH_RESEARCH_CHECKOUT` | `rl agent open` 给 researcher 会话设成它的 worktree。代码路径、代码审查的指纹和 `rl run register` 的代码检查都按这个 checkout 读；研究记录仍写进 `SMOOTH_RESEARCH_PROJECT` 指向的项目。不设时从当前目录判断：在项目的 linked worktree 里就用这个 worktree |
| `SMOOTH_RESEARCH_SESSION`、`SMOOTH_RESEARCH_ROLE` | `rl agent open` 给每个会话设成它的会话记录（S-x）和会话名（如 `reviewer-1`）。设了 `SMOOTH_RESEARCH_ROLE` 的会话不能批准审查轮次（`rl review approve`），发消息时默认以这个名字发送 |
| `HERDR_ENV`、`HERDR_PANE_ID` | herdr 设置，CLI 只读。`HERDR_ENV=1` 时才能开 agent 会话、发通知；协调者 `rl session start` 时记下 `HERDR_PANE_ID`，通知发到这个 pane |
| `CODEX_HOME` | 默认 `~/.codex`。CLI 从其中的 `sessions/` 读 Codex 实际使用的模型。如果其中的 `config.toml` 设了 `sandbox_mode = "danger-full-access"`，`rl delegate` 启动的可写 Codex 会话也用这个模式，否则用 `workspace-write` |

## 4. 命令行选项

- **全局选项：** `--project`、`--json`（输出完整 JSON）、`--actor`（写进事件日志的名字）。`--project` 和 `--json` 也可以写在子命令后面。
- **单次覆盖：**
  - `--budget`：`rl context` 和 `rl resume` 的 token 目标；
  - `--entry`：`rl review run`、`rl advise run`（只能给一个工单）、`rl delegate` 和 `rl agent open` 只用这一个模型；
  - `--timeout`：模型调用超时，审查和建议默认 1800 秒，`rl delegate` 默认 3600 秒；`rl agent open` 等会话启动，默认 90 秒；
  - `--background`：在后台进程运行。
- **一轮建议一起运行。** `rl advise run AD-x AD-y ...` 给每个工单开一个进程，同时运行。
- **超出建议轮数：** `rl advise prepare --user-approved "<用户的原话>"`，见第 1 节。
- **审查轮次：** `rl review request ... --recheck-of RV-x` 是同一轮的第二次审查；第三次报 LIMIT，要开新一轮写 `--new-round "<原因>"`。`rl review approve RV-x --rationale ..` 由协调者批准还有中等以上问题的一轮。
- **agent 会话：** `rl agent open [名字] --role researcher|reviewer|advisor [--task T-x | --ticket RV-x/AD-x] [--entry ..] [--no-worktree] [--base ..]`，以及 `assign`、`tell`、`reset --compact|--clear`、`close [--remove-worktree]`、`brief`、`list [--all]`、`dialog <名字>`（会话现在显示的对话框和选项）、`answer <名字> <选项编号或文字> --reason ..`（协调者回答对话框）。
- **论文草稿：** `rl manuscript [--title ..]` 写或刷新草稿，`--title` 只用于新建的草稿，见第 1 节"论文草稿"。
- **监视程序：** `rl agent check` 立刻查看一轮；`rl agent watch --start` 在单独的 tab 里启动（已经在运行就不重复启动），`--stop` 停止并关掉它的 tab；`--every <分钟>` 改间隔，不写就用 `agents.watch_minutes`。不带 `--start` 或 `--stop` 时在当前终端前台运行。

## 5. 写死的常量（不能配置）

| 常量 | 值 | 含义 |
|---|---|---|
| `INLINE_FILE_CHARS` | 60,000 字符 | 审查请求包内联单个文件的上限，超过的文件只给路径（`reviews.py`） |
| `INLINE_TOTAL_CHARS` | 240,000 字符 | 审查请求包内联文件的总上限（`reviews.py`） |
| `LOCK_TIMEOUT_S` | 60 秒 | 写入等待写锁的上限，超时报 LOCKED（`store.py`） |
| `STALE_AFTER_H` | 6 小时 | 未结束的 attempt 超过这么久没有新观察，描述里会加一句 "stale?" 提示（`runs.py`） |
| `REVIEWS_PER_ROUND` | 2 | 一轮审查最多几次。第二次后还有中等以上问题，就交给协调者批准或退回（`reviews.py`） |
| `NOTICE_CHARS` | 600 字符 | 发给 herdr 会话的一行通知里，消息正文最多保留多少字符；完整消息在 `rl inbox`（`herdr.py`） |
| `MAX_RESTARTS` | 一小时 3 次 | 监视程序一小时内最多重启同一个会话几次，再退出就交给协调者（`watch.py`） |
| `BUILD_TIMEOUT` | 300 秒 | `rl manuscript` 运行一次 LaTeX 编译命令的上限（`manuscript.py`） |
| `MAX_REMINDERS` | 2 次 | 同一项任务，监视程序最多提醒几次；之后，或者提醒后会话毫无反应，就报告协调者（`watch.py`） |
