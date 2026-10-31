# Smooth Research 配置说明

配置分四处：项目配置文件 `smooth-research.yaml`、约束文档、几个环境变量、命令行选项。程序读取的只有 `smooth-research.yaml`。约束文档写给人和 agent 看，CLI 不执行它。

## 1. 项目配置 `smooth-research.yaml`

它放在研究项目的根目录，由 `rl init` 生成。默认值在 `src/smooth_research/project.py` 的 `DEFAULT_CONFIG`。

### 加载规则

- `version` 必须是 `1`，否则命令报错。
- 文件里的值和默认值深度合并：字典逐键覆盖，列表整体替换。比如只写一个 `candidates`，默认的两个候选就都没了。
- 不认识的键不会让命令失败，但每条命令都会给出警告：
  - 普通输出末尾的 `warning:` 行；
  - `--json` 结果里的 `warnings`；
  - 会话开始时的 hook 提示。
- 下面两种情况也会警告：
  - 旧版本写进配置、但从来没有代码读取的键（见本节最后）；
  - `relation_to_author` 不是 `different_family`。
- 配置文件不能加载时（YAML 写错或 `version` 不对），每条命令都报错。会话开始的 hook 也会直接说明这个错误，不会当成"没有项目"。

### 目录和文件

所有路径都相对于项目根目录。

| 键 | 默认值 | 含义 |
|---|---|---|
| `code_root` | `src` | 实验代码目录。init 会创建它，并放一个忽略 `__pycache__` 和 `*.pyc` 的 `.gitignore`。工作包和 `rl delegate` 的提示词会让 agent 把代码放这里，但不强制，审查范围和 RunSpec 可以引用任何路径 |
| `docs_root` | `docs` | 人读的文档：`plan/`、`experiments/`（实验合同草稿）、`tasks/`、`notes/` |
| `reports_root` | `reports` | 审查回答 `reviews/`、建议回答 `advice/`、`rl report` 的报告、`rl view` 的 `research-graph.html` |
| `research_root` | `research` | 机器记录：数据库 `state/research.lattice` 和写锁、封存的文档 `documents/objects/`、请求包 `packages/`、模型调用日志 `calls/`、run 产物 `artifacts/<attempt>/` 等。init 写的 `.gitignore` 让 `state/`、`calls/`、`artifacts/` 不进 git |
| `plan` | `docs/plan/original.md` | `rl init --plan` 把原始计划复制到这里并封存。之后再导入不同的计划会报 CONFLICT，计划的变化要记成假设修订和决定 |
| `constraints_ref` | `docs/plan/constraints.md` | 约束文档的位置，见第 2 节 |

- **目录要在 init 之前定好。** init 不覆盖已有的配置文件，所以可以先写好 `smooth-research.yaml`，再运行 `rl init`。init 之后再改 `research_root`，CLI 会到新位置找数据库，找不到就报 NO_PROJECT。旧记录还留在原来的目录里，要手动移过去。
- **改了目录名，agent 看到的路径也跟着变。** 恢复包（`rl resume`）和工作包都有一行 Locations，列出本项目实际的目录。skill 文档里的路径按默认值写，并注明这一点；researcher 子代理按工作包的 Locations 放代码、笔记和 run 产物。

### 模型

模型条目的格式是 `<入口>:<模型>[@effort]`：

- **`claude`**：用 `claude -p` 调用。审查和建议是只读会话，只开放 Read、Grep、Glob 三个工具。
- **`codex`**：用 `codex exec` 调用。审查和建议在 read-only 沙箱里运行。
- **`manual`**：不自动调用。CLI 给出请求包的路径，由人提交给模型，再用 `rl review import` 或 `rl advise import` 导入回答。
- **`@effort`**：传给 Claude 的 `--effort`，或 Codex 的 `model_reasoning_effort`。
- **模型家族**：按模型名判断。`claude`、`opus`、`sonnet`、`haiku`、`fable` 属于 anthropic；`gpt`、`o1`–`o9`、`codex`、`chatgpt` 开头的属于 openai。从名字判断不了时，按入口推断。

| 键 | 默认值 | 含义 |
|---|---|---|
| `models.reviewer.candidates` | `codex:gpt-6-astra@medium`、`claude:claude-opus-5-5@medium` | 代码审查和结论审查的候选，按顺序尝试 |
| `models.reviewer.relation_to_author` | `different_family` | 排除和作者同一家族的候选（`manual` 条目不受影响）。只认 `different_family`：写成其他任何值都会关闭这个过滤，CLI 会给出警告 |
| `models.advisor.primary` | `claude:claude-fable-5-1@high` | `rl advise` 首先尝试的模型 |
| `models.advisor.fallback` | `codex:gpt-6-astra@high`、`manual:gpt-pro` | 首选失败后依次尝试 |

研究者用的模型不在这里配置：

- Claude 的 researcher 子代理定义里没有指定模型，由 Claude Code 决定，默认继承协调者会话的模型；
- `rl delegate` 每次都要用 `--entry` 指定模型。

几条容易忽略的行为：

- **按顺序尝试。** 第一个给出可解析回答的候选生效。调用失败，或者回答里没有可解析的结果块，就试下一个。
- **遇到 `manual` 就停。** 工单变成 pending，等人提交回答，排在它后面的候选不会被尝试。所以 `manual` 要放在最后。
- **候选列表在建工单时写入。** `rl review request` 和 `rl advise prepare` 建工单时就把候选固定下来，之后改配置不影响已经建好的工单。要换模型，用 `rl review run RV-x --entry <条目>`。`--entry` 直接替换候选列表，不做家族过滤，但回答的 relation 仍按实际模型记录。
- **同家族的回答不算跨模型。** 同家族的回答记为 `same_family_independent_session`。代码审查的复用和 `rl run register` 的覆盖检查都不承认它；`rl decide` 的检查目前承认，见第 6 节。
- **advisor 配置参与建议请求的去重。** 改了 advisor 配置后，同一个问题不会再被判为重复请求。
- **`rl models` 只检查命令行工具。** 它列出所有候选，检查 `claude` 和 `codex` 命令在不在 PATH 上，不验证模型本身。实际使用的模型在第一次调用时记录。

### 上下文包

| 键 | 默认值 | 含义 |
|---|---|---|
| `context.work_target_tokens` | `4000` | 工作包和恢复包的 token 目标，用于 `rl context --purpose work/resume`、`rl resume`，以及 `rl delegate` 提示词里附带的工作包 |
| `context.advice_target_tokens` | `10000` | 建议请求包的 token 目标 |

- **token 数是估算值。** ASCII 字符数除以 4，每个非 ASCII 字符算 1。
- **超出目标的段落会被省略。** 段落按优先级加入，超出目标的不放进包里，只在包尾的 "Omitted (read on demand)" 列出名字。第一个段落总会保留。
- **`--budget` 可以单次覆盖目标。**
- **审查请求包不用这两个目标。** 它有自己写死的上限，见第 5 节。

### 旧项目里的废弃键

2026-10-31 以前生成的配置文件里还有下面三个键，但没有任何代码读取它们。现在它们会触发警告，删掉即可：

| 键 | 实际情况 |
|---|---|
| `models.researcher: inherit` | researcher 子代理的模型由宿主决定，默认是会话的模型；`rl delegate` 用 `--entry` 指定模型 |
| `artifacts.content_digest: blake2b` | 摘要算法写死为 blake2b |
| `artifacts.keep_raw_evidence: true` | CLI 从不删除原始产物 |

## 2. 约束文档 `docs/plan/constraints.md`

`rl init` 生成一个模板，有五项要填：

- 研究目标和结论范围；
- 允许的数据、模型和环境；
- 资源上限（每个实验、每批、总量）；
- 模型选择和替代；
- 需要用户先决定的动作。

这份文档的作用：

- **CLI 不解析、不执行它。** 资源预算、允许的集群这类限制只写在这里。`smooth-research.yaml` 不放执行 profile、队列或预算（设计文档 §13.1）。
- **各请求包怎样引用它。** 工作包只给出它的路径，提醒 agent 遵守。建议请求包会附上它的前 3000 个字符。它的摘要还参与建议请求的去重。
- **"模型选择"一项只是记录。** 这里写谁选了什么、为什么。实际调用哪个模型由 `smooth-research.yaml` 决定，两边要手动保持一致。

## 3. 环境变量

| 变量 | 含义 |
|---|---|
| `SMOOTH_RESEARCH_PROJECT` | 项目目录。查找顺序：先看 `--project`，再看这个变量，最后从当前目录往上找 `smooth-research.yaml` |
| `SMOOTH_RESEARCH_HOME` | 插件 CLI 环境的安装位置。设了 `XDG_DATA_HOME` 时默认为 `$XDG_DATA_HOME/smooth-research`，否则为 `~/.local/share/smooth-research`。`rl setup` 在这里建 `env-<build-id>` |
| `SMOOTH_RESEARCH_RL` | `bin/rl` 启动器自动把它设成自己的绝对路径，让 hook 提示和 delegate 子会话调用同一个 CLI。一般不用手动设 |
| `CODEX_HOME` | 默认 `~/.codex`。CLI 从其中的 `sessions/` 读 Codex 实际使用的模型。如果其中的 `config.toml` 设了 `sandbox_mode = "danger-full-access"`，`rl delegate` 启动的可写 Codex 会话也用这个模式，否则用 `workspace-write` |

## 4. 命令行选项

- **全局选项：** `--project`、`--json`（输出完整 JSON）、`--actor`（写进事件日志的名字）。`--project` 和 `--json` 也可以写在子命令后面。
- **单次覆盖：**
  - `--budget`：`rl context` 和 `rl resume` 的 token 目标；
  - `--entry`：`rl review run` 和 `rl advise run` 只用这一个模型；
  - `--timeout`：模型调用超时，审查和建议默认 1800 秒，`rl delegate` 默认 3600 秒；
  - `--background`：在后台进程运行。
- **`rl delegate --entry` 必须写。** 配置文件里没有它的默认值。

## 5. 写死的常量（不能配置）

| 常量 | 值 | 含义 |
|---|---|---|
| `INLINE_FILE_CHARS` | 60,000 字符 | 审查请求包内联单个文件的上限，超过的文件只给路径（`reviews.py`） |
| `INLINE_TOTAL_CHARS` | 240,000 字符 | 审查请求包内联文件的总上限（`reviews.py`） |
| `LOCK_TIMEOUT_S` | 60 秒 | 写入等待写锁的上限，超时报 LOCKED（`store.py`） |
| `STALE_AFTER_H` | 6 小时 | 未结束的 attempt 超过这么久没有新观察，描述里会加一句 "stale?" 提示（`runs.py`） |

## 6. 已知的不一致（暂不修改）

`rl decide` 检查关键结论时，只看结论审查的 verdict（`pass` 或 `issues`），不看 relation。

- **默认配置下不会出现。** 建工单时已经排除了同家族候选，所以结论审查默认都是跨模型的。
- **两种情况下会出现：** 用 `--entry` 指定同家族模型，或者关闭了 `relation_to_author` 过滤。这时同家族的结论审查也能通过 `rl decide` 的检查，而同样情况下的代码审查不算覆盖。
- **和设计不一致。** 设计文档 §8.6 要求结论审查也必须跨模型。按用户决定，这一处暂不修改。
