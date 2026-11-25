# bypass-lora 复盘改进设计（定稿）

2026-11-05 定稿。底稿是 2026-11-03 的复盘稿 `reports/bypass-lora-improvements-2026-11-03.md`（下称原稿）。这一版合并了两份独立审核：Claude（claude-opus-5-5）对照代码、bypass-lora 的记录和会话日志逐条核对，codex（gpt-6-astra，max）的审核全文在 `reports/DOING/design_review/codex_73d0124_bypass-lora-improvements.md`。同日早些时候的审核页 `design/review-bypass-lora-improvements.html` 也作为输入，其中的判断都重新核对过。按你的要求，另加了监视合并和层次编号两项设计。审核时留下的 22 个待定事项，你确认全部按建议定。正文直接写定下的做法，文末的决定记录写明选了什么、为什么，以及没采纳的方案。代码以 73d0124 为准，行号都指这一版。标“推断”的地方没有直接证据，其余都能在所列位置查到。

## 审核结论

两份审核的大方向一致。codex 的总体意见是不宜照原稿直接开工，先要纠正几处基础认识，Claude 的核对得出同样的结论：

- 存活监视和用量监视本来就在同一个 watcher 进程里，分开的是检查分支、节奏和说明。
- codex 会话的 host_session_id 从来没有建立，原稿“按会话 id 找日志”目前做不到。
- stale 已经按 run 提交里的内容摘要逐文件判断，误报来自审核集合的选取。
- arm 名不一致的结果会保留，只标为不能用于预定比较。
- `rl export` 已经能在一个读事务里批量导出记录。

两份审核建议不同的地方，都按 Claude 的建议定了，codex 的主张和没采纳的理由见决定记录。事实上的分歧只有第 8 条，会话记录支持原稿的说法。

| 原稿条目 | 结论 | 这一版的处理 |
|---|---|---|
| 1 阶段报告读不懂 | 修改后采纳 | rl 渲染说明页，说明文字放旁挂文件，arm 汇总先改对再上页面 |
| 2 容量错误无人处理 | 修改后采纳 | 和用量监视合成一个监视循环，以 codex 日志为准 |
| 3 第一条提示被吞 | 诊断更正 | assign 根本没有确认送达，改为确认回合开始 |
| 4 task create 不封存 | 采纳 | 和第 9 条合并 |
| 5 stale 误报 | 修改后采纳 | 逐路径给原因，测试文件和缺失路径只提示 |
| 6 CLI 小摩擦 | 修改后采纳 | inbox 和写锁的描述更正，批量 JSON 改用 rl export |
| 7 arm 名对不上 | 修改后采纳 | 契约声明 arms，登记时校验 |
| 8 研究者猜作业号 | 修改后采纳 | 允许带证据的更正改绑 |
| 9 任务说明和契约不同步 | 采纳，事实更正 | DOC-0372 是任务说明，契约是 DOC-0374，并入第 4 条 |
| 10 顾问材料包不知产物已删 | 修改后采纳 | 检查范围扩到 Result 的产物，约束不被截掉 |
| 11 产物生命周期 | 推迟 | 只做第 10 条的检查，生命周期等第二个项目再定 |
| 12 合并后测试失败 | 修改后采纳 | 交付时记录验证，适用于所有改代码的任务 |
| 13 永远挂着的 attention | 采纳 | 加确认命令，未满足的条件不能确认 |
| 14 批准旧轮次让覆盖变 stale | 修改后采纳 | 新增 resolve，approve 的含义不变 |
| 15 中文手稿 | 现象更正 | 方案提示已有，缺的是测过的配置和抽取检查 |
| 16 文献没用上 | 修改后采纳 | 结论审核包带上 nearest_work，缺时提示 |
| 17 协调者自己写代码 | 修改后采纳 | skill 写明分工，不做 git 启发式检查 |

原稿没有提到、审核中发现的问题：

- 协调者自己写了两类监视。一类是每 30 秒跑一次 `rl agent list`、比对状态变化的 Monitor 循环，每次最长 30 分钟，从 11-01 08:08 到 11-03 00:26（UTC）共启动 82 次。另一类是“assign 或 tell 之后等它变成 working”的临时检查，11-02 有 13 次（按 tool_use id 去重，来源是 `~/.claude/projects/-work-xg24i002-x10041-bypass-lora/` 下的会话记录）。rl 缺的正是状态变化的及时通知和送达确认。（Claude）
- 11-05 herdr 更新后，codex 集成不再用钩子报状态，原稿第 2 条的两个前提随之改变，见下一节。（两份审核）
- 空闲提醒没有等待期：一轮恰好在会话刚停下时运行，就会马上提醒它（watch.py:106-129 只看当下状态）。（Claude）
- watcher 在通知真正送出之前就记下“已说过”：`_once`（watch.py:330-336）先写 told，通知到 watch.py:75 才发。空闲提醒和给协调者的提醒送达失败时，次数照样增加（watch.py:127、311）。手动 `rl agent check` 和后台 watcher 也可能同时动作。（codex）
- codex 的用量按日志文件的修改时间挑（usage.py:146），不比较读数本身的时间，一个刚被写过的旧会话日志可能挡住更新的读数。窗口的重置时间过了就直接当作 100%（usage.py:85-89）。（codex）
- arm 汇总按 arm 名把一个实验的全部 attempt 平均在一起（view.py:117-148），不区分契约修订和运行条件，同一 run 的重跑也算进去。报告和结论审核包都用这张表。（codex）
- 结论审核只核对假设编号：review_gap（research.py:390）看通过的审核指向了哪些假设，不看假设的修订，假设改了主张以后旧的通过照样算数。（codex）
- 读命令也会改东西：store 第一次打开旧项目时就地升级（store.py:322），launcher 可能同步项目依赖（plugin/bin/rl:115）。两份审核为了不动 bypass-lora，都只能绕开 rl 直接读数据库。（codex）
- 顾问材料包按固定顺序拼接，超出预算就丢掉后面的段，而 Constraints 排在最后（context.py:205-239，拼接在 context.py:57）。（codex）
- `rl manuscript` 在当前 shell 里直接跑 LaTeX（manuscript.py:308），不经执行策略。（codex）

## herdr 更新改变了第 2 条的前提

11-05 16:31 herdr 升到 0.9.5（服务端仍是 0.9.4，`herdr status` 报 server_binary_stale），codex 集成重装为 v10。`~/.codex/hooks.json` 里属于 herdr 的只剩一个 SessionStart 钩子，它把 codex 的会话 id 报给 herdr（`pane.report_agent_session`），不再报 working 和 idle。codex 的状态改由 herdr 读屏判断，规则在 `~/.local/state/herdr/agent-detection/remote/codex.toml`（2026.10.11.1）：标题栏里有转圈字符就算 working，`herdr agent explain` 能看到命中的是哪条规则。`~/.codex/config.toml` 还留着旧钩子的信任记录（user_prompt_submit:1:0、stop:1:0、interrupt:0:0），和原稿描述的旧集成一致。11-02 当时装的是哪一版、钩子有没有执行，现在已经查不到，原稿“Stop 钩子没触发所以一直 working”只能算推断。

这带来两个变化。一是 `herdr agent get` 现在返回 `agent_session.value`，就是 codex 的会话 id，和 rollout 文件名末尾一致。已在 tab 2 的 codex 会话上核对：`01a1aaf7-34de-7340-93fb-17d049b69dcc` 对应 `~/.codex/sessions/2026/11/05/rollout-2026-11-05T18-08-10-01a1aaf7-34de-7340-93fb-17d049b69dcc.jsonl`。原稿说“会话记录里有 host_session_id”，对 codex 不成立：launch.py:71 只给 claude 和 pi 生成 id，`rl agent reset --clear` 还会清空它（agents.py:271）。有了 herdr 报的 id，就不必按 cwd 去猜日志，同一 cwd 下常有多个 codex 会话。二是按读屏规则，出错结束的回合大概率显示 idle，原稿“一直显示 working”的现象可能不再出现。这一点没有复现（推断）。下面的设计不依赖 herdr 的 working，一律以 codex 自己的日志为准。

## P0：会话监视合成一个循环（原稿第 2 条，加上你的补充要求）

### 现在三处各看一部分

watcher 在自己的标签页（sr-watch）里跑 `rl agent watch`（watch.py:389-428），由 `rl agent open` 启动，最后一个会话关闭时退出。它每 5 分钟醒一次：到了 `agents.watch_minutes`（默认 20）分钟就做一轮完整检查，其余几次只看用量（USAGE_MINUTES，watch.py:44）。启动后先睡，不立即检查。

完整检查对每个会话跑 `_round`：agent 不见了就重启，停在对话框就答信任问题或上报，空闲且任务未完成就提醒，每个 assignment 最多两次。显示 working 的会话直接跳过（watch.py:103-104）。只看用量的那几次走 `usage_only` 分支，只处理低额度主机上的 researcher 和 reviewer（watch.py:59-68），只通知收尾交接，不看存活，advisor 不在范围内。用量按主机读：codex 取最近改过的 5 个 rollout 里的 token_count（usage.py:143-166），claude 取状态栏缓存。每次检查之后都写一次心跳（watch.py:421），所以心跳正常不说明最近做过完整检查。

第三处是上面说的协调者自写的 Monitor。三者合起来：存活 20 分钟才看一次，working 的会话从来不看。用量 5 分钟看一次，却只看低额度主机。协调者为了补缺口自己轮询，又只盯非 working 的状态。三次容量错误合计停了约 4 小时 52 分（codex 按日志逐次核对为 4:51:31）。

还有一个没被利用的事实：codex 出错的回合以带 error 的 task_complete 结束（`~/.codex/sessions/2026/11/02/rollout-2026-11-02T18-23-23-01a19b92-0eb8-7782-a636-d74a591227b1.jsonl` 第 980 行，`codex_error_info` 为 `server_overloaded`），紧挨着的上一行就是带 rate_limits 的 token_count。同一个日志既能判断存活，也能读用量。错误之后还会有别的事件（第 981、982 行是 settings 事件），所以要看回合的生命周期事件，不能只看最后一行。

### 设计：一个节拍，一次探测，分开计次

**节拍**，`agents.watch_minutes` 改为监视节拍，默认 5 分钟，删掉 USAGE_MINUTES 和 `usage_only` 分支。watcher 启动时先做一拍。每一拍依次：确认 herdr 可用，算一次低额度主机，对每个打开的会话（含协调者）探测、分类、处理，有新情况时给协调者发一条 NOTE。提醒多久发一次由下面的等待期和次数上限决定，节拍变短不会让提醒变多。watcher 的生命周期不变。已有项目的配置里写着 `watch_minutes: 20`（init 会写入，bypass-lora 的 smooth-research.yaml:45 就是），新默认值对它们不生效，watcher 启动时和 `rl upgrade` 在它大于 5 时提示改小。

**探测**，每个会话一次 `herdr agent get`（现在也要调），取 agent_status、state_change_seq 和 agent_session，再读宿主日志：

- codex：用 agent_session.value 找 `$CODEX_HOME/sessions/*/*/*/rollout-*-<id>.jsonl`。路径和读到的位置缓存在 Session.watch 里，之后每拍只读新增的部分，文件变短或换了就重新定位。取当前回合的生命周期：最后的 task_started 之后有没有 task_complete，task_complete 带不带 error，有没有还没返回的工具调用，以及最后一次有效进展的时间。回合内部的模型请求重试不算出错，只看回合怎样结束。
- claude：rl 启动时已经用 `--session-id` 定了 id（launch.py:39），`/clear` 之后 herdr 也会报新 id。日志在 `~/.claude/projects/*/<id>.jsonl`。出错回合在日志里长什么样，现在没有样本，要先抓一次再定匹配规则。
- pi：没有日志读取，只用 herdr 状态，和现在一样。

会话 id 以 herdr 报的为准，写回 Session.host_session_id，顺带解决 reset --clear 之后 id 未知的问题。id 变了（reset、restart、switch、/clear）就重新绑定日志，旧日志里的事件不再触发任何动作。拿不到 id 时这个会话只用 herdr 状态，并在 `rl agent list` 里标“日志不可用”，不按 cwd 去猜。

“有效进展”指日志里新的回合事件、工具调用和回复。settings 这类事件、watcher 自己打进去的提示和面板刷新都不算，否则停滞永远计不满。

**分类**，按顺序取第一个成立的：

| 分类 | 判据 | 处理 |
|---|---|---|
| 退出 | herdr 找不到 agent | 重启，每小时最多 3 次（不变）。低额度主机上不重启，请协调者 switch |
| 对话框 | herdr 报 blocked | 只答本目录的信任问题，其余上报一次（不变） |
| 低额度 | researcher 或 reviewer 所在主机低于阈值 | 通知收尾交接，协调者在它空闲时 switch（逻辑不变，改为每拍都看）。同一主机上的重试和重启暂停 |
| 回合出错 | 当前回合以带 error 的 task_complete 结束 | 临时错误自动重试，额度类错误按低额度处理，其他错误上报 |
| 停滞 | 回合未结束，没有进行中的工具调用，超过 `agents.stall_minutes`（默认 30）没有有效进展 | 读一次面板，末尾几行随 NOTE 上报，不自动输入 |
| 空闲未完成 | idle，任务未完成，也不在等任何东西 | 连续两拍都是同一状态（state_change_seq 不变）才提醒，每个 assignment 最多两次 |
| 其他 | 在工作，或在正常等待 | 不动 |

日志说回合已经结束、herdr 还报 working 时，以日志为准，旧集成“出错后一直 working”的问题就绕过去了。工具调用长时间不返回（例如超过停滞时限的 4 倍）也上报一次。`rl agent switch` 判断会话是否还在工作（agents.py:332）时也用这个分类，否则 herdr 误报 working 的会话切不了。

**重试**，临时错误指 codex_error_info 为 server_overloaded 一类，取值清单要从日志里整理，至少包含这次的 server_overloaded。重试是往会话里打一行固定文字：上一回合被模型服务的错误打断，请从断点继续（`rl agent brief <name>`），并带上“来自 watcher，不是用户”的标记。出错后的下一拍发第一次，之后隔 2 拍、4 拍（默认节拍下约 5、10、20 分钟），每段连续出错最多 3 次。只有一个回合正常结束、并且有有效进展才清零，只看到 working 不算。3 次都失败时给协调者一条 NOTE：researcher 和 reviewer 建议 `rl agent switch`，advisor 只上报（switch 现在拒绝 advisor，agents.py:328-330）。自动重试要等分类在历史日志上验证正确之后才打开，见实施顺序。

**计次**分开记在 Session.watch：reminders（空闲提醒，按 assignment）、retries（出错重试，按连续出错段）、restarts（按小时）、told（只说一次的事）。现在一个 MAX_REMINDERS 管所有提醒（watch.py:40），重试如果也记在里面，几次容量错误就会把它用完。次数只在确认送达后才加，told 也只在通知送出后才写，现在两处都是先记后发。写 Session.watch 时在写事务里重读再合并，`_remember`（watch.py:318-327）现在用的是事务外读到的旧值，两次写会互相覆盖。

**一个执行者**，同一时刻只有一个进程对会话动作。watcher 每拍的处理阶段和手动 `rl agent check` 取同一把锁，拿不到就跳过这一次并说明。

**等回复的豁免**，`_awaited_reply`（watch.py:217-229）只对健康的空闲会话生效，回合出错的会话一律按出错处理，豁免的其余规则不变。advisor-1 在 03:29 出错时 MSG-1011 还没被读，按这条规则它会在下一拍被重试。

**协调者**，同样探测协调者会话。它的回合以临时错误结束、之后又空闲满一拍时，按同样的重试规则提醒它继续。空闲满一拍才输入，可以避开你正在和它对话的时候。未读消息的提醒不变（watch.py:286-315）。

**用量**，按读数事件的时间取最新的读数，不按文件修改时间。读数带上时间，超过 15 分钟标为过期。窗口的重置时间过了以后，没有新读数就显示“已重置，暂无读数”，不当作 100%。过期或没有读数的主机不算低额度，这和现在一样，只是显示出来。

**送达确认**，rl 打给会话的提示都要确认送达，见下文第 3 条一节。

**呈现**，`rl agent list` 每个会话一行，写 herdr 状态、监视分类、在等什么、最后有效进展距今多久、最近的错误、重试次数和下一步动作，末尾每个主机一行用量窗口、读数时间和是否低额度，没有打开的会话时也显示这几行。`rl usage` 只留 statusline 子命令（Claude 状态栏要用），不带参数时报错并指向 `rl agent list`。`rl agent check` 会动手处理，只看状态用 `rl agent list`。watcher 标签页只打印有变化的拍。skill 写明协调者不必自己挂轮询循环，watcher 的通知会叫醒它。

### 不采纳的部分

原稿建议按面板文字匹配错误（“Selected model is at capacity”、529、rate limit 等）。这会误报：会话自己在读或写这些字符串时，面板上就有这些字，正在审核本文的 codex 就是例子。日志里的 error 字段是结构化的，不会混淆。面板文字只在上报停滞时作为摘录，不触发自动动作。

原稿第 2 条建议 6（herdr 把出错回合映射成 idle）不再需要跟进：集成已经不靠钩子报状态，rl 也改以日志为准。

自动换模型不做，由协调者决定：任务中途换模型，记录里的模型身份跟着变。

### 测试与改动范围

tests/test_watch.py 和 tests/test_agents.py:108-119 已有假 herdr。加一个假的 codex sessions 目录，用这次三个出错回合的日志片段作夹具，覆盖：

- herdr 报 working、日志是带 error 的 task_complete 时会被重试，错误之后跟着 settings 事件也一样。
- 旧回合出错、新回合已经正常结束时不重试。
- 会话 id 变了以后，旧日志里的错误不触发动作。
- 重试按拍退避，3 次后只上报一次，只看到 working 不清零。
- 停滞只上报一次，settings 事件不重置停滞计时。
- 空闲提醒要连续两拍，送达失败不计次。
- 低额度时同一主机上不重试。
- watcher 和手动 check 同时运行时只有一个动作。
- 旧日志文件刚被修改、但读数更旧时，取更新的读数。
- `rl agent list` 在没有会话时也列出各主机的用量，`rl usage statusline` 照旧可用。

改动集中在 watch.py，另有 herdr.py（get 带出 agent_session，prompt 增加确认）、usage.py（读数时间）、agents.py（list_sessions 的列，assign 的确认，switch 的判断）、launch.py（启动后记录会话 id）、cli.py（`rl usage` 只留 statusline）、project.py（watch_minutes 的默认值和说明，新增 stall_minutes）、rl upgrade 的提示和测试。说明文字里关于 watcher 节奏和 `rl usage` 的说法一并改：

- SKILL.md:95（“every 20 minutes”）。
- team.md:17-31 和 127-143。
- README.md:46-49。
- docs/configuration.md 第 25、80 行（默认值），90-97 行（监视程序），109-110 行（额度从哪里读），第 255、278 行。
- project.py:37、616、619 的配置注释，watch.py、cli.py、usage.py 的文档字符串和帮助文字。

## P0：层次编号（你新加的要求）

要求：给人看的假设编号要看得出层次，例如 H1.1.2 一看就是 H1.1 的子节点。实验编号要看得出和哪个假设节点相关，例如 EH1.1.2/1。数据库内部仍用递增的唯一主键。

### 现状

假设和实验的编号由 Meta 里的计数器按创建顺序发放（store.py:147-156），形如 H-0013、E-0021，被拒绝的记录也不让出编号。结构在关系里：derived_from 记假设的来源，目标可以是假设、顾问回答（AD-）或论文（P-），可以有多个（schema.py:157）。tests 记实验检验哪些假设，至少一个（research.py:195）。这些关系要靠 `rl show` 或图才看得到。

### 设计

**两种编号**，内部编号（H-0013）不变，仍是主键，关系、事件、文件名、作业名、提交信息、RunSpec 和封存文档都只用它。层次编号是假设和实验新增的 `label` 字段，创建时算好写入，以后不改。

**假设**，`H` 加点分的序号。没有假设来源的是顶层，按创建顺序编 H1、H2……，只源自顾问回答或论文的也是顶层。有假设来源的挂在一个主父节点下，编号是父节点的编号加点加序号，序号是它在这个父节点下的创建次序。主父节点这样定：

- 只有一个假设来源时就是它。
- 有多个时默认取最晚创建的那个，通常也是最具体的那个。`--parent H-x` 可以指定。
- `--root` 让它开一个新的顶层编号，用于开启新研究线的假设，来源关系照常记录。

**实验**，`E` 加锚点假设的编号、斜杠和序号，序号是它在这个锚点下的创建次序。锚点是它检验的各假设在编号树上最深的共同祖先，也就是各编号的最长公共前缀，例如同时检验 H3.3.1、H3.3.2、H3.3.3 的实验挂在 H3.3 下。检验的假设分属不同顶层、没有共同祖先时，取 `--hypotheses` 里的第一个。`--anchor H-x` 可以指定。

**序号不复用**，被拒绝或退役的假设和实验保留编号，和内部编号一样（store.py:148 的注释）。下一个序号取同一父节点下已有子编号的最大值加一，在写事务里算，不需要新的计数器。祖先关系从编号本身就能读出，算锚点也只用比较编号。

**为什么存下来**，编号会写进说明页、决定理由、消息和手稿。每次按当前关系现算的话，后来补一条来源关系或改了主父节点，整棵子树都会改号，之前写下的文字就指错了节点。存下来的编号只记创建时的位置。它只体现一个主父节点，其余来源和检验关系照常是图，`rl show` 和说明页把它们都列出来。

**解析**，CLI 凡是接受假设或实验编号的地方都同时接受两种写法，解析参数时换成内部编号，函数内部只见内部编号。两种写法不会混淆：内部编号一定带连字符（util.py:15 的 REF_RE、store.label_of 都依赖它），层次编号不带。层次编号里也没有逗号和 @，不影响逗号分隔的列表（util.py:102-107）和 `@修订` 的写法。

**显示**，给人看的地方只用层次编号：说明页、`rl status`、各类列表、报告和手稿生成的块。brief 开头写一次对应关系（例如 EH3.3/3 即 E-0016），研究者写路径、作业名和提交信息时要用内部编号。除此之外，内部编号只出现在 `rl show`（两种都显示）、JSON（加 `label` 字段）和路径、作业名这类机器用的地方。排序按各段序号的数值，H2.10 排在 H2.9 之后（store._sort_key 现在只认内部编号）。页面锚点用内部编号，`/` 和 `.` 不进锚点。说明页渲染记录里的文字（例如决定理由）时，把其中的内部编号换成层次编号并加链接，旧记录也能读。

**迁移**，加一个 schema 3 的迁移（upgrade.py 的 MIGRATIONS），按内部编号顺序用默认规则给已有的假设和实验补上编号，`rl upgrade` 打印对照表。迁移不知道哪些假设开过新研究线，所以已有项目的编号会偏深。

**skill**，SKILL.md 说明两种编号的分工：和用户说话、写说明和决定时用层次编号，路径、作业名和提交信息里用内部编号。假设开启了一条新的研究线时加 `--root`，只是细化来源假设时不加。

### 在 bypass-lora 上模拟

以只读方式读取 bypass-lora 的记录，按上面的规则算编号。共 33 个假设、32 个实验，假设之间有 36 条来源关系，4 个假设没有假设来源。

- 5 个假设有多个假设来源（H-0014、H-0015、H-0016、H-0032、H-0033），默认规则下都落在最具体的上下文里。例如 H-0016 源自 H-0002（FFN 融合）和 H-0013（残差旁路），落在残差旁路之下。
- 24 个实验只检验一个假设，8 个检验多个，这 8 个都有共同祖先。E-0016 同时检验 H-0010、H-0011、H-0012，编为 EH3.3/3。
- 问题在深度。全按来源关系编号时最深 8 层，H-0032 是 H2.1.2.1.1.1.1.1。来源关系记的是推导链，另起的研究线也挂在旧链上。残差旁路线的起点 H-0013 源自 V5 的 H-0006，编号是 H2.1.2。分段信息访问线的起点 H-0019 编为 H2.1.2.1.1。
- bypass-lora 自己的说明页把 H-0006（V5）、H-0013（残差旁路）、H-0019（分段信息访问）当作研究线的起点（research/notes/hypothesis-tree-generator/nodes.py 的 LINES）。这三个加 `--root` 后最深 4 层。

| 记录 | 全按来源关系 | 三个研究线起点加 `--root` |
|---|---|---|
| H-0013 残差旁路线起点 | H2.1.2 | H6 |
| H-0016 | H2.1.2.3 | H6.3 |
| H-0019 分段信息访问线起点 | H2.1.2.1.1 | H7 |
| H-0032 | H2.1.2.1.1.1.1.1 | H7.1.1.1 |
| E-0016 | EH3.3/3 | EH3.3/3 |
| E-0021 | EH2.1.2/2 | EH6/2 |
| E-0032 | EH2.1.2.1.1.1.1/2 | EH7.1.1/2 |
| 最深层数 | 8 | 4 |

所以 `--root` 是编号保持好读的关键，skill 里那条规则要写清楚。

### 测试与改动范围

- 顶层、单来源、多来源默认取最晚创建的、`--parent`、`--root`，只有论文或顾问来源时为顶层。
- 实验锚点取最长公共前缀，没有共同前缀时取第一个，`--anchor` 可改。
- 被拒绝的节点不让出序号。
- show、link、decide、experiment add 等命令里两种写法都能用，内部编号照旧可用。
- H2.10 排在 H2.9 之后。
- 迁移给旧项目补的编号，和按同样顺序新建得到的一致。
- 说明页把记录文字里的内部编号换成层次编号。

改动：schema.py（label 字段）、research.py（add_hypothesis、add_experiment）、store.py（按编号查找、排序）、cli.py（`--parent`、`--root`、`--anchor` 和参数解析）、upgrade.py（schema 3）、view.py 和各处显示、delegate.py（brief 的对应关系）、SKILL.md、测试。说明页用到层次编号，这一项要先于说明页做。

## P0：可读的研究说明页（原稿第 1 条）

### 现状

`rl report` 有研究问题、假设、arm 汇总、阶段交付和用量（view.py:190-234），但以罗列记录为主，没有研究叙述。实验表格按字母序取前 6 个数值指标当列（view.py:139），所以会出现 `check.src/test_core.py::...` 这样的列名。bypass-lora 七份报告共约 1.61 MB，最新一份 1,268 行（codex 统计）。`rl view` 是 mermaid 图加记录列表（view.py:84-110），mermaid 从 CDN 加载（view.py:98），离线打不开。

bypass-lora 的 hypothesis-tree.html 靠项目自写的生成器维护。nodes.py 里有研究线分组（LINES）、每个节点的目的、做法和结果、术语表。build.py 里的计数（build.py:24 的 N_H、N_E）和导语里的数字（“33 个假说、32 个实验分属 7 条研究线”）都是手写的，状态一变就得改。rebuild.sh 逐个调 `rl --json show`，末尾另取一次 status，一页里的数字可能来自不同时刻的记录。

### 设计：事实、文字、页面分开

事实由 rl 每次渲染时在一个读事务里从记录现取，不进任何手写文件，包括节点的层次编号、标题、状态、关系、契约指标、各 arm 的数字、成本、相关决定和各类计数。文字由协调者或它派的子代理写进旁挂文件，rl 只读不改。页面由 rl 自带的模板确定性地渲染成自包含 HTML，不加载外部资源，不再每次让 LLM 生成页面。

旁挂文件放在 `research/notes/explanation.yaml`。SKILL.md:25-30 规定协调者在 research/ 下只写契约、任务说明和笔记，说明文字属于笔记，docs/ 只放用户读的成品。结构如下：

```yaml
lines:              # 研究线：名字、一句说明、这条线包含的子树的根
  - key: m
    name: M 线：记忆读取、修复与 V3
    desc: 问答时主干看不到事实，只能靠侧支的低维记忆。
    roots: [H3]
summary:            # 导语，只写判断，不写数字
  text: ...
  basis: D-0080     # 写它时最新的阶段决定
nodes:
  H3:
    title: ...
    purpose: ...
    did: ...
    result: ...
    basis: "blake2b:..."   # rl view --stamp 写入
```

节点的键用层次编号或内部编号都可以，rl 读的时候统一换成内部编号。数字一律由 rl 填，说明和导语里不写计数。没写说明的节点照样显示事实，标“说明待写”。没分到任何研究线的顶层假设放进“未归类”。记录怎么变，构建都不会失败。

**页面上的数字先要对**，现在的 arm 汇总按 arm 名把一个实验的全部结果平均（view.py:117-148），说明页和报告都不能直接用。改为按契约修订、arm、数据版本和配置分组，组内代码提交不同时标出来。每个 run 取一个结果（最后一个 complete，没有才用 partial，并标出来），同一 run 的重跑不重复计入。每个数字旁边写 seed 数、attempt 状态和契约修订。契约声明了 `metrics:`（第 7 条）就只列这些指标，没声明时才退回现在的规则。条件不同的结果分行列出，不做跨条件平均。成本分开写已报告、未知和共享作业。结论审核包也用这张表，改用同一个函数。

**过期判定**，basis 是节点叙事相关事实的摘要。假设取 statement、revision、evidence_status、disposition、相关证据的 id 与 validity、相关决定的 id。实验取契约修订、stage、各 attempt 的状态计数、结果 id、相关决定的 id。摘要用 util.digest_json（blake2b）。不用 data_version 或 updated_at，watcher 的心跳和每条消息都会改它们（store.py:354）。摘要变了，节点标“说明可能过期”。导语的 basis 是阶段决定，出现更新的阶段决定就算过期。

**命令**，`rl view` 渲染 `docs/research.html`，不再生成 mermaid 页面 research-graph.html。已有项目里的旧文件不删，`rl upgrade` 提示它不再更新。`rl view` 同时报告缺失和过期的说明各有几处。`rl view --todo` 列出要写或要改的节点和它们现在的事实，交给写的人。`rl view --stamp H3,EH3.3/3` 在改完文字后记下当前摘要。`rl status` 的 attention 只加一条汇总（说明页有几处待写或过期），不按节点逐条列。提到 research-graph.html 的地方一起改：SKILL.md:26、workflow.md:295、docs/configuration.md:39、README.md:97、context.py:82 和 cli.py:498。

**自动重渲染**，会改变页面事实的命令（hypothesis、experiment、evidence、decide 和 run import-result 这几类）结束时，如果 docs/research.html 已经存在，就重渲染一次。页面里不放渲染时间，内容没变就不写文件，只有事实变了 git status 里才会看到它。重渲染失败只打印警告，不影响命令本身。

**页面结构**，研究线下按层次编号排出假设树，实验挂在锚点假设下。层次编号只体现一个主父节点，其他来源关系和一个实验检验的其他假设都在节点里列出并加链接，页面不把关系当成严格的树。

**和已有机制的关系**，manuscript 已经是“记录生成的块加作者文字”（manuscript.py:1-7），说明页是同一个分工：事实由 rl 生成，文字由作者写，过期由摘要判定。外部工具要批量 JSON 就用 `rl export`（第 6 条）。`rl graph --json` 现在输出文本树（cli.py:67-72），不改它的含义。

**rl report** 保留，作审计附录，和页面共用同一套取数函数，各自在一个读事务里取数。实验表格用上面改过的汇总，过长的单元格截断，并指向 `rl run show`。

**工作流**，skill 的 Finish a stage 改为：

1. `rl decide --stage ...`
2. `rl view --todo`，写或改说明
3. `rl view --stamp ...`，然后 `rl view`
4. `rl report`

说明缺失或过期不阻塞 decide，只在 attention 里汇总。

bypass-lora 的生成器可作参考：页面结构、三段式说明和 DOM 测试可以借鉴，手写计数和逐个 show 的取数方式不要照搬。

测试覆盖：

- 记录变化（新增假设、状态改变）后渲染不失败，计数正确。
- 缺失和过期的标记正确，摘要只在相关事实变化时改变，消息和心跳不让它变。
- 一个假设有多个来源、一个实验检验多个假设时都能正确显示。
- 契约的主要指标不在字母序前 6 个时照样列出。
- 同 arm 不同契约修订不合并，同一 run 的重跑不重复计入。
- 改记录的命令之后页面跟着变，事实没变时文件不被改写。
- HTML 转义正确，页面不发外部请求。

## P1：提示要确认送达（原稿第 3 条）

原稿推断被吞的提示走了 `agent_prompt_stalled` 分支（herdr.py:61-62），这不对。assign 调 `herdr.prompt` 时没有传 wait_ms（agents.py:240-241），所以不带 `--wait`。按 herdr 的约定，只有带 `--wait` 才会检查提交后 5 秒内有没有出现 working、并返回 agent_prompt_stalled，不带时 herdr 写完就返回成功。rl 没做任何确认，自然报告“told to read its brief”。stalled 分支只在 reset --compact（agents.py:267）这类带等待的调用上起作用。同样不确认的还有 answer 之后的通知（agents.py:191）和 watcher 的各种提醒（watch.py:121、140、274、306）。协调者 11-02 手动做的 13 次“等它变成 working”的检查，补的就是这个缺口。

设计：非斜杠命令的提示改用 `herdr agent prompt <target> <text> --wait --until working --until blocked --timeout 20000`。

- 变成 working，算送达。很短的回合可能在两次检测之间就结束，所以 codex 会话的日志里出现发送之后的新 task_started 也算送达。
- 变成 blocked，按对话框处理（现有逻辑）。
- 超时或 stalled 时先查日志，有新回合就算送达，没有才读面板。仍确认不了就返回“未确认送达”，调用方照实报告：assign 的输出写未送达，watcher 记为未送达，不算提醒次数。确定没送到才重发，不确定时不重发，免得同一件事做两遍。
- 斜杠命令（/compact、/clear、/new）不显示 working，保持现状。

codex 的 Hooks 面板：协调者在 bypass-lora 里遇到它时，处理都是按 esc、重发（d2bf5e61 会话的压缩摘要）。面板的确切文字现在没有样本，第一版只上报“疑似被面板挡住”，附面板末尾几行。下次出现时把面板文字抓下来写成测试夹具，之后对这个面板自动按一次 esc 并重发，仍未送达就上报。认不出的面板一律不按 esc，codex 空闲时按 esc 可能进入编辑上一条消息。

测试：

- 普通 assign 被吞时报告未送达。
- 很短的回合只在日志里留下 task_started 时，算送达。
- 超时但日志显示已经开始时不重发。
- 斜杠命令照旧，真实的权限对话框按 blocked 处理。

## P1：契约随引用封存，任务绑定契约版本（原稿第 4、9 条）

SKILL.md:68-69 说契约第一次被 run、review 或 task 引用时封存。代码只在 `rl experiment seal`（research.py:258-264）和 `run register`（runs.py:71、90）里封存。`task create`（tasks.py:59-77）只封存任务说明，review request 封存的是审核材料包（reviews.py:494-521），都不封存目标实验的契约。bypass-lora 的记录里，T-0008、T-0009 在 11-01 08:01:31、08:01:33 创建，它们指向的 E-0009、E-0008 分别到 08:01:49、08:01:48 才封存（EVT-000634 至 000640）。

第 9 条是同一缺口的后果，原稿的事实要更正：T-0035 的任务说明是 DOC-0372@1，E-0032 的封存契约是 DOC-0374@1，原稿第 112 行把 DOC-0372 当成了契约。说明第 5 点和契约的规则不一致，researcher-2 自己发现并提问（MSG-1021），协调者答复以契约为准（MSG-1022，T-0035-progress.md:3-5）。

设计：

- `task create` 封存它直接指向的每个实验，把封存的修订记在 targets 关系上（schema 已有 revision_bound 的用法）。只指向假设的任务不封存它下面的实验。契约不完整、封存失败时，task create 报错并说明缺什么。读契约文件、算摘要放在写锁之外，和 seal_experiment 现在的做法一样（research.py:258-261）。
- `review request --target E-x` 同样封存。封存只是记下一个修订，之后契约照样能改，下次封存成为新修订，所以不妨碍先审契约再改。
- `rl agent brief`（delegate.prompt_for 和 context.work_pack）列出任务依据的契约修订。实验后来有了新修订时，brief 同时列出两个修订和它们正文的差异，并给出新契约的路径。已经登记的 run 仍绑定登记时的修订，不受影响。
- 契约改了以后任务不暂停，靠 brief 里的差异让研究者看到。

这样 skill 的说法和代码一致，也符合预登记的本意：任务一建，研究者照着做的就是封存的版本，之后的修改都有修订记录。

## P1：审核覆盖逐路径给原因（原稿第 5 条）

读 bypass-lora 的记录核对了 8 个解释 stale 的决定：

| 情况 | 决定 | rl 能否自动判断 |
|---|---|---|
| 审核过的路径不在 run 的提交里 | D-0016 | 能 |
| 只改了测试文件 | D-0049、D-0054、D-0055，D-0061 的两个测试文件 | 能，按命名约定 |
| 另一实验的通过审核看过同一份字节 | D-0061 的 src/bypass_model.py（RV-0078、RV-0079） | 能 |
| 审核的发现或改动都在这次 run 不执行的文件里 | D-0020、D-0053，D-0061 的 src/rb_sink_read.py | 不能 |
| 两个审过的改动合并进同一文件，组合没人审过 | D-0017 | 本来就该报 |

D-0020 的审核 RV-0049 还有没解决的发现，只是发现所在的文件不在 stage A 的路径上，它不全是 stale 的问题（codex 指出）。D-0055 另记了一个 arm 名不一致（RES-0273），见第 7 条。

D-0017 说明 stale 机制本身有用，不能简单放宽。原稿第三项（摘要相同就不报）代码已经在做（scope.py:171-183）。D-0061 那个逐字节相同的文件之所以报 stale，是因为审核集合只取本实验、code_from 链和 `--review` 点名的审核（scope.py:99-114），RV-0078、RV-0079 不在其中。早先审核页推断这些例子是 RunSpec 没写提交、读了工作树造成的，记录不支持：D-0016、D-0017 的 run 都带提交。

设计：code_coverage 给每个路径一个原因，run register 和 run show 逐行打印，并把“没有通过的审核”和“通过的审核已过期”分开写。只有 `changed` 会让状态变成 stale，其余原因只提示。

- `changed`：内容变了，不是测试文件。
- `absent`：run 的提交里没有这个路径，执行不到。现在它和内容改变一样处理（scope.py:165-169 读不到时返回 None）。
- `test`：路径符合测试文件约定（tests/ 下，或 test_*.py、*_test.py、conftest.py）。测试文件改了，run 计算的东西不变。提示里逐个列出文件名，按名字误认的生产文件一眼能看出来。bypass-lora 的测试就放在 src/ 下（src/test_rb1_artifacts.py 等），只认 tests/ 目录不够用。
- `seen_by`：项目里另一条通过的代码审核看过这份字节，提示 `--review RV-x`，不自动算作覆盖。审核回答的是那个实验的问题，换了实验，同一个文件的用法可能不同。

覆盖来自多条审核、每条只看过其中一部分文件时，输出注明“逐文件覆盖”，表示这些文件的组合没有被一起审过。不在执行路径上的改动 rl 判断不了，逐路径列出后，协调者只需看 `changed` 的那几个文件，不必再对全部路径做 git diff。

## P1：被复查解决的旧轮次（原稿第 14 条）

读记录核对：RV-0004 是 RV-0003 轮次的第二次审核，留着 medium 升级给协调者。RV-0005 新开一轮，recheck_of 指向 RV-0004，结论 pass。attention 只看 escalated 和 approved（research.py:564-567），RV-0004 就一直挂着。协调者为清掉它批准了 RV-0004，理由写明“结论以 RV-0005 为准”。可 approve 的含义是“接受剩余的发现”（reviews.py:433-434），批准让 RV-0004 算作通过（research.py:370-377），它的审核范围进了覆盖计算。codex 核对，RV-0004 的范围比 RV-0005 宽，还含配置说明，覆盖因此多出了新审核没看过的路径。原稿记录两个 run 因此变成 stale（具体路径未复核）。

设计：

- 新增 `rl review resolve RV-x --by RV-y --reason ..`，说明 RV-x 的发现已由通过的复查 RV-y 解决。记一个事件，RV-x 从 attention 移走，它的结论不变，范围也不进覆盖计算。RV-y 必须沿 recheck_of 链在 RV-x 之后，并且通过。
- `review approve` 保持“接受剩余发现”的含义。对一个之后已有通过复查的轮次执行 approve 时，提示改用 resolve。
- attention 里这类轮次的提示写明哪次复查已通过，以及 resolve 的写法。

attention 不自动跳过这类轮次：recheck_of 只说明 RV-0005 是对 RV-0004 的复查，不说明 RV-0004 的每个发现都被看过。

## P1：结论审核要绑定假设的修订（审核新发现）

review_gap（research.py:390）只看通过的结论审核指向了哪些假设。targets 关系不记修订（schema.py:172），审核时的结论和现在的是否一致也不核对。一个假设改了主张（revise 之后修订加一），或者证据从支持变成挑战，引用旧的通过审核照样能让 decide 不报缺口。codex 没有发现 bypass-lora 实际碰到过这种情况。

设计：结论审核在 targets 上记下每个假设的修订。decide 和 attention 判断审核缺口时，只认修订和当前一致的通过审核。修订不同时提示“审核针对的是 H3@1，现在是 @2”，需要重审，或在 decide 的理由里说明为什么不影响结论。审核之后又有新证据挂到这个假设上时只提示，不算缺口。

测试：

- 同一假设改了修订，旧审核不再算数。
- 修订不变、只加了证据时给提示。
- 不同假设引用旧审核仍然报缺口（现有 tests/test_reviews.py:672）。

## P1：CLI 的小摩擦（原稿第 6 条）

- `rl decide` 给了 `--rationale-file` 时 `--rationale` 可省（cli.py:620、627），短理由取文件第一行非空文字。两者都给时行为不变。分工也不变：短理由进记录和报告，文件封存成文档（research.py:425-437）。
- `--based-on` 允许指向 Decision（schema.py:168）。阶段决定常要引用上一个决定，现有的 supersedes 是“取代”，意思不对。这只是引用：被引用的决定需要复查时，复查标记不往下传（recheck_dependents 只沿证据和结果传，research.py:340-350），关系也不许成环。
- `rl inbox --ack MSG-x` 只确认到这一条。原稿说读完之后才到的消息也会被确认，不准确：`--ack` 在同一个写事务里取消息并推进游标（messages.py:76-89），确认的就是这次返回的消息。风险出在先 `rl inbox` 读、过一会儿再 `--ack`：第二次会把两次之间新到的消息一起确认，这些消息虽然在第二次的输出里，容易被当成读过的内容略过。不带 id 的 `--ack` 保持现状。
- `rl agent tell --file <path>`（cli.py:858-862）。
- 新增 `rl hypothesis list`、`rl evidence list`，支持按状态过滤和 `--json`。批量导出用 `rl export --records-only`：把 snapshot.json 的内容打到标准输出，不复制封存文件（snapshot.py:36-42 现在总会复制）。
- 写锁已经会等 60 秒（store.py:25、285-296），原稿说的“加等待”现在就有。本项目两次碰到 LOCKED，都是重试成功。先量出谁占锁久：review request 在写事务里生成并封存审核材料包（reviews.py:504-521），是一个嫌疑。能挪到锁外的准备工作挪出去，超时的报错写明等了多久、锁被哪个进程占着。不加整条命令的自动重试，命令没有幂等保证时，重试可能重复写入。

## P1：arm 名在登记时按契约校验（原稿第 7 条）

RunSpec 的 arm 是自由文本，导入结果时才和 manifest 的 arm 比对（runs.py:378-381）。不一致时结果保留，只标为不能用于预定比较，提示里已经这么说（runs.py:420-428，tests/test_runs.py:157）。契约模板把 arm 写在 Design 正文里（research.py:217），没有结构化字段。除原稿列的 RES-0168 至 0171、RES-0319、RES-0368 至 0370，D-0055 还记了 RES-0273（登记为 W-02-interventions，manifest 写 W-02）。

设计：契约 front matter 加两个可选列表，`arms:` 和 `metrics:`。有 `arms` 时 `run register` 拒绝列表外的 arm，名字从源头就对。`metrics` 供报告和说明页选列。导入时 arm 不一致的处理照旧（保留结果、标为不可用），提示改为写清两边的名字和两条出路：代码对，就按代码的名字登记新 run。登记对，就改代码。旧实验没有 `arms` 时不校验，也不批量猜旧记录的对应关系。不做别名映射：登记过的 RunSpec 不能改，别名会让同一个 arm 在记录里有两个名字，报告和说明页还得处理映射。

codex 建议把阶段（如 dev-replay）和处理条件（如 A2）拆成两个字段。这次的不一致都是同一个名字写法不同，`arms:` 校验已能挡住，不拆。

## P2：作业号记错后无法更正（原稿第 8 条）

经过以 researcher-2 的会话记录为准（11-03，UTC）：

1. 06:15:34 登记 RUN-0405，得到 ATT-0405-1。
2. 06:15:44 用 miyabi-mcp 做提交的预检（dry run），同一回合里并行执行了 `rl run record --exec-id 3786622.opbs`，作业号是猜的。
3. 06:15:56 研究者发现记错。06:16:02 正式提交，环境变量 SR_ATTEMPT_ID=ATT-0405-1，真实作业号 3786487.opbs。
4. 06:16:09 用 `--correction-of` 把 ATT-0405-1 改记成真实作业号，rl 报 CONFLICT：“ATT-0405-1 is bound to X-0403; a re-execution is a new attempt”（runs.py:264-267）。
5. 06:16:20 改为只撤回错误的观测，撤回被接受，但 ATT-0405-1 仍绑定在错号上。
6. 06:16:55 照提示新开 ATT-0405-2，把真实作业记在它下面。

结果是记录和产物对不上：作业按 ATT-0405-1 提交，输出在 research/artifacts/ATT-0405-1，记录却在 ATT-0405-2 下，ATT-0405-1 以 cancelled 结束。codex 认为真实作业属于 ATT-0405-2、是运行脚本带错了 attempt 标识。会话记录显示作业在 ATT-0405-2 创建（06:16:49）之前就按 ATT-0405-1 提交了，标识当时是对的，是更正被拒之后记录才挪到新 attempt 上。原稿的诊断成立。

设计：

- `run record --correction-of OBS-x` 所更正的正是当初带来绑定的那条观测时，允许改绑到新的执行引用，要求 `--note`，记一个事件。其他情况下来了不同的作业号仍报 CONFLICT，提示分开写两种情况：记错了用 `--correction-of`，重新执行才开新 attempt。
- 资源的 jobs 是 `mcp:` 或 `skill:` 时，人工 `run record --exec-id` 打印提示：作业号以执行器的导出为准（workflow.md:182-184）。rl 不解析执行器的账本格式（workflow.md:166-168）。
- 不强制这类资源只收执行器的导出。这次的错误研究者十几秒后就自己发现了，缺的是更正的路，改绑和提示用过一段仍有问题再收紧。codex 建议由登记结果生成作业的环境变量、作业启动时核验，这次环境变量本身没错，不做。

## P2：其余各条

- 第 10 条：advise prepare 准备材料包时，检查相关产物还在不在。现在的 Raw artifact index 只来自 Evidence 的 doc_ref 和 artifact_refs（context.py:235-238），不含 Result manifest 里的 checkpoint，检查范围要扩到相关 Result 的产物。状态分为在、不在、远程未检查、无权限，无权限不写成已删除。只查本地路径，不算摘要、不下载。不可用的产物和 Constraints 放到材料包前部，预算不够时先压缩历史证据，不能把约束截掉。不依赖第 11 条。
- 第 11 条：产物生命周期这一轮不做，等第二个项目再定。bypass-lora 的清理笔记（research/notes/artifact-cleanup.md）写到第 13 节，很多是这个项目特有的判断，一个项目的经验不够定通用规则。下一个项目如果很快碰到 /work 配额，先做产物清单（大小、引用来源、pin、保留期限和删除事件），plan/apply 再往后。
- 第 12 条：改了代码的任务交付时记录验证，`task deliver --verified "<命令> @<提交>: pass|fail，日志 <路径>"`。不只 implement 任务，T-0035 是 experiment 任务，也改了核心代码。`task accept` 显示它，没有记录、或分支 head 不是验证过的提交时给提示，不拦 accept，记录用过一段再考虑收紧。原稿说记成 attempt，可 attempt 挂在实验的 run 下（runs.py:59 的 register 需要 Experiment），测试不是实验，硬塞进去会混进运行记录。原稿的“全量测试”也要更正：T-0036 跑的是 20 个模块的选定套件（T-0036-post-merge-regression.md:27-29）。
- 第 13 条：attention 加确认，`rl attention ack <key> --reason ..` 记一个事件，这一条从 attention 移走，事实仍留在记录和报告里。底层事实再变化（例如契约又有新修订）时重新出现。E-0011 那条（research.py:561-562）就这样处理。只有陈述事实的条目能确认，审核缺口这类还没满足的条件不能靠确认消失。
- 第 15 条：现象改为“只有方案提示，没有测过的配置”。workflow.md:332-333、manuscript.py:51 和 docs/configuration.md:153 都写了用 xelatex 或 lualatex 加 CJK 包。docs/configuration.md 补一套测过的配置：构建命令、字体、全角引号的写法，不把某台机器上的字体路径写成通用默认。`rl manuscript` 构建后用 pypdf 抽取 PDF 的文字做检查：从生成的 .tex 正文里取若干段连续的中日韩文字，去掉空白后在抽出的文字里查找，找不到的超过一半就报错，挡住“PDF 看着对、文字是乱码”的情况。源文件里没有中日韩文字时不查。新增依赖 pypdf（纯 Python）。另外 `rl manuscript` 在当前 shell 里直接跑 LaTeX（manuscript.py:308），在登录节点上也一样，配置说明里要写清构建在哪里跑。
- 第 16 条：结论审核的材料包（reviews.py:647 起）现在完全不读文献，相关假设登记了 nearest_work 也送不到审核者手里。改为包里带上当前假设修订的 nearest_work 和评估，没有时写明空缺。一键导入顾问的引用，需要顾问在答案里给结构化的引用块，放到以后。
- 第 17 条：skill 的默认规则写明协调者不改 src/ 和测试，只做只读的小检查，紧急修复先问用户。SKILL.md:8-9 的“小检查”没说清边界，bypass-lora 的协调者把修测试断言（a1317f2）也当成了小检查。原稿建议的检查（主分支上不经任务分支、又改了 src/ 的提交）不做：默认分支未必叫 master，共享的 git 作者认不出是谁改的，不用 worktree 时任务的提交本来就直接落在主分支上（codex 指出）。把失败的 attempt 转成任务用现有的 `rl task create` 就够，不另加命令。
- 只读模式（审核新发现）：加全局参数 `rl --read-only`，不升级记录、不同步依赖、不写缓存和心跳。项目格式比 CLI 旧时，说明需要先升级，然后退出。

## 实施顺序

1. 统一监视的观测部分：会话 id 绑定、日志增量读取、分类、送达确认、通知和计次的可靠性、用量读数时间、`rl agent list` 的呈现和 `rl usage` 的收缩。先用这次三个出错回合的日志作夹具，确认分类正确，暂不自动输入。
2. 打开自动重试和协调者提醒。
3. 层次编号（schema 3 迁移）。和第 1、2 项互不依赖，可以并行。
4. 说明页，含 arm 汇总的分组更正和自动重渲染。依赖第 3 项。
5. 契约随引用封存、任务绑定契约版本、结论审核绑定假设修订。
6. 审核覆盖的逐路径原因、旧轮次的 resolve。
7. CLI 摩擦、arm 校验、作业号更正。
8. 第 10、12、13、15、16、17 条和只读模式。

产物生命周期不在这一轮。

## 决定记录

2026-11-05，审核时列出的 22 项全部按建议定。每项写选定的做法、理由和没采纳的方案，codex 的主张单独注明。

**1. 监视节拍**，一个节拍，默认 5 分钟，每拍做全部检查，沿用 `agents.watch_minutes`。容量错误这次让会话停了几个小时，5 分钟和 1 分钟的发现时间差别不大，一个节奏的配置和测试都简单。没采纳 codex 的 60 秒一拍（用量和提醒另设间隔，另起配置键）：三个间隔要分别配置和测试，一拍也装不下最多等 90 秒的操作（watch.py:97、251）。没采纳保留两档：两套路径还在。

**2. 临时错误的自动重试**，自动重试，按 1、2、4 拍退避，最多 3 次，之后上报并建议 switch。三次停顿都是一句 continue 就恢复的，自动输入的内容固定，带 watcher 标记。codex 建议的 2、5、10、20 分钟配合的是 60 秒节拍，没采纳，它建议的上线顺序（先诊断，验证分类后再打开重试）采纳了。没采纳只上报（停顿会持续到有人看到）和自动换模型（任务中途换模型应由协调者判断）。

**3. 等回复的豁免**，出错的会话不享受豁免，其余规则不变，正好覆盖 advisor-1 那次。没采纳 codex 的显式等待语义（`--expects-reply`、in_reply_to）：要改 tell、inbox、brief 和 skill，旧消息没有标记，过渡期仍要靠现在的规则。没采纳限时豁免：会在协调者忙时去提醒正当等待回答的会话。

**4. 协调者自己出错**，回合以临时错误结束、又空闲满一拍时提醒它继续。协调者停下，整个项目都停。没采纳只显示不提醒。

**5. 会话日志的定位**，用 herdr 报的会话 id。watcher 每拍本来就调 `herdr agent get`，两份审核都选了这条路。没采纳 rl 自己的 SessionStart 钩子记录：hooks.py 刻意只给提示、不写记录（hooks.py:1-4）。herdr 以后不再报会话 id 时再换。

**6. rl usage**，用量并入 `rl agent list`，`rl usage` 只留 statusline。存活和用量放在一起看，只有一个入口。没采纳 codex 的两处都显示：两套输出要同步。

**7. codex 的 Hooks 面板**，先上报，抓到面板文字、写成夹具之后，对这一个面板自动按一次 esc 并重发。协调者每次的处理都是这个动作，只认准确匹配的面板，误按的风险限定在这一个面板上。没采纳 codex 的一直只上报：协调者每次都要自己处理。

**8. 层次编号的存储**，创建时算好存下来，不改。编号会写进说明、决定理由和消息，现算会让整棵子树改号。没采纳现算，也没采纳允许 relabel 加别名：一个节点会有两个层次编号，解析、显示和说明页都要处理别名。

**9. 主父节点**，默认取最晚创建的假设来源，`--parent` 可改，`--root` 开新的顶层编号。bypass-lora 的 5 个多来源假设按默认规则都落在合适的位置。没采纳不提供 `--root`（bypass-lora 最深 8 层），也没采纳多来源时强制 `--parent`（默认规则已经够用，只多一步操作）。

**10. 实验的锚点**，检验的各假设的最深共同祖先，`--anchor` 可改。EH3.3/3 表示 E-0016 属于 H3.3 这一组。没采纳取第一个假设：EH3.3.1/1 看起来只和第一个子假设有关。

**11. 内部编号的露出**，给人看的地方只用层次编号，brief 开头写一次对应关系。没采纳处处写成 H2.1.2（H-0013）：每行多一串数字。

**12. 说明页的重渲染**，改记录的命令结束时自动重渲染。审核时列的命令是 decide、evidence add/set、hypothesis set/revise、experiment seal。按同一个理由（页面始终和记录一致），定稿时把范围扩到所有会改变页面事实的命令，包括新增假设、实验和导入结果。页面不放渲染时间，内容不变不写文件。没采纳只在阶段结束时渲染。

**13. 说明页的入口**，`rl view` 生成说明页，不再生成 research-graph.html。说明页反映当前状态，和 `rl view` 的用途一致，报告按阶段存档，生命周期不同。没采纳 codex 的 `rl report --html`：它的好处是同一快照，现在的做法让两者共用取数函数、各在一个读事务里取数，同样不会混用不同时刻的记录。没采纳新增 `rl explain`：两套输出。

**14. 契约改了以后的任务**，brief 列出两个修订和正文差异，不阻塞任务。T-0035 的冲突是研究者自己发现的，缺的是让差异一眼可见。没采纳 codex 的规则类字段改动后暂停任务：每次多一轮协调者操作，而按旧修订登记的 run 本来就绑着旧修订。

**15. 测试文件和缺失路径**，`absent` 和 `test` 只提示。bypass-lora 的 8 个 stale 决定里 5 个属于这两类，提交里没有的文件执行不到，测试文件改了不改变 run 计算的东西。没采纳 codex 的照旧算 stale、按路径记录例外：例外决定照旧要写。它担心的按文件名误认，靠提示里逐个列出文件名兜底。

**16. 另一实验的审核看过同一份字节**，只提示 `--review RV-x`。按实验挑审核是有意的。没采纳自动算作覆盖：会悄悄放宽现有约定。

**17. 被复查解决的旧轮次**，新增 `rl review resolve`，这是 codex 的主张。recheck_of 不说明每个发现都被看过，这次 RV-0005 的范围就比 RV-0004 窄。没采纳 attention 自动跳过（Claude 起初的方案）：复查没覆盖原发现时也会把提醒清掉。

**18. arm 别名**，不做。登记过的 RunSpec 不能改，别名会让同一个 arm 有两个名字。

**19. 产物生命周期**，只在准备顾问材料包时检查，生命周期等第二个项目再定。一个项目的经验不够定通用规则。没采纳 codex 的先做产物清单（只为第 10 条服务的话，现查就够），也没采纳现在就做 plan/apply。

**20. 中文手稿**，文档给一套测过的 CJK 配置，构建后做抽取检查。字体和机器有关，预设很难在别的机器上成立，抽取检查和机器无关。审核时没定的抽取工具，定稿补定为 pypdf：纯 Python，结果不随机器上装的工具变化。没采纳 codex 的内置可选预设。

**21. 交付时的测试验证**，`task deliver --verified` 记在任务上，`task accept` 只提示。测试不是实验，不记成 attempt。没采纳 accept 拒绝没有验证记录的任务，codex 也建议先让记录可见再收紧。

**22. 协调者的分工**，skill 的默认规则写明协调者不改 src/ 和测试。SKILL.md 的“小检查”没说清边界。没采纳 codex 的只在项目约束里写：每个项目都要再提一次。

## 实施记录

按实施顺序逐项实现，每项一个提交，测试都在 Miyabi 的交互节点上跑。这里只记和上文不同、或上文没定下的地方。

**第 1、2 项**（7dc6b30，两项在同一个提交里）。按上文实现，分类用这次三个出错回合的日志片段作夹具测试（tests/fixtures/codex/）。临时错误目前只认 server_overloaded，额度类错误是 usage_limit_exceeded（agentlog.py）。claude 会话的日志不读，因为还没有它出错回合的样本，claude 和 pi 会话只看 herdr 状态。Hooks 面板按第一版只上报：送达确认不了、面板里有 hooks 字样时注明可能被面板挡住，不按 esc。

**第 3 项**（c943c58）。按上文实现。SKILL.md 里两种编号的分工和 `--root` 的规则放到最后一项，和 Finish a stage 的改动一起写，因为 SKILL.md 限 150 行（tests/test_packaging.py:56）。

**第 4 项**（5b46727）：

- 摘要不写进 explanation.yaml 的 `basis:`，写在 rl 自己的 research/notes/explanation.basis.yaml。上文既说 rl 对文字文件只读不改，又让 `rl view --stamp` 往里写 basis，拆成两个文件后文字文件只由人写。explanation.yaml 里写了 `basis:` 时，rl 报告这个键不用。
- 写了说明、但从没 stamp 过的节点算“说明可能过期”，`rl view --todo` 注明“written but not stamped”。导语也一样。
- 自动重渲染的命令是 hypothesis add/revise/set、experiment add/seal、evidence add/set、decide、link 和 run import-result/reject-result，比上文多了 link 和 reject-result，它们也改变页面上的事实。run register、run record 和用量不触发重渲染，这和第 12 条决定说的“所有改变页面事实的命令”不同：attempt 的状态计数会变，但这些命令很频繁，一次渲染要读整张记录图，在 bypass-lora 的副本上约 9 秒。计数由下一个触发重渲染的命令或 `rl view` 补上。
- 在 bypass-lora 的副本上，第一版渲染要 62 秒，时间主要花在取入边时扫描全部边。store.py 改成从有索引的 id 出发匹配入边后，渲染约 9 秒，所有读入边的地方都受益。
- 页面上的固定文字用英文（explanation to write、may be out of date、Unclassified），和 rl 的其他输出一致。说明文字用什么语言，由写的人决定。
- 契约的 `metrics:` 在这一项就用上了：arm 表只列声明的指标，没声明时主指标排在第一列。第 7 项只剩 `arms:` 的登记校验。
- 契约草稿的 front matter 被手改得不是映射时，按空处理，页面和其他读草稿的地方不再出错。
- 说明页存在时，`rl status` 的 attention 要多做一次检查，在 bypass-lora 的副本上多花约 2.4 秒。

**第 5 项**：

- `task create` 和 `review request` 封存前先检查契约是否完整：字段缺失、为空或写着 TODO 时拒绝，报错列出要填的字段。所有目标检查完才开始封存，命令被拒时不会留下文档对象。读契约、算摘要都在写锁之外。`run register` 和 `rl experiment seal` 的行为不变。
- targets 关系上记 `revision` 和 `contract_ref`。brief 里，绑定的修订就是当前修订时只写一行。契约之后又有新修订时，写出两个修订、新草稿的路径和正文的 unified diff，diff 最多 80 行（context.DIFF_LINES）。
- 结论审核在 targets 关系上记 `revision`，以及审核时挂在这个假设上的支持和挑战证据（`evidence`），用来判断哪些证据是审核之后才挂上的。
- 审核缺口比较的是审核的修订和决定所依据的修订。decide 时就是假设的当前修订。attention 检查已有的关键决定时，用 decides 关系上记的修订，不用假设现在的修订：决定之后假设又有新修订，那个决定当时是审过的，不应因此变成缺口。
- 审核之后才挂上的证据，在 decide 的输出和 attention 里都给提示，不算缺口。没有记录证据清单的旧审核不给这个提示。
- 记录格式 4 的迁移从事件日志恢复旧结论审核的修订：在这条审核的请求事件（review.requested，复用时是 review.reused）之前，取这个假设最后一次 hypothesis.revised 记下的修订，之前没有修订事件时取 1。找不到请求事件的审核不绑定，对任何修订仍然算数。旧审核看过哪些证据补不出来，所以不补。旧任务的 targets 也不补修订，因为创建任务时契约还没有封存。
- 新增冻结项目 tests/fixtures/projects/schema-4/。在 bypass-lora 的副本上升级，41 个结论审核的假设目标绑定了修订。升级前后，attention 里关键结论的条目都是 0，迁移没有造成误报。

**第 6 项**：

- `seen_by` 不是第四种原因，而是附在 `changed` 路径上的提示。上文既说只有 `changed` 让状态变成 stale，又说 `seen_by` 不自动算作覆盖，所以这样的路径仍然是 stale，提示写出 `--review RV-x`。
- 通过的审核看过的路径全都不在代码里、或者全是测试文件时，状态记为没有审查（none），不记为 covered：这时没有一个被执行的文件是审过的。上文没有讨论这个边界。
- “没有通过的审核”单独列出，但只列之后没有通过复查的那些。bypass-lora 的 21 个没通过的代码审核，后来全部有复查，如果都列出来，每次登记都会多出一串旧轮次。attention 仍按上文的规则，不自动跳过被复查过的轮次。
- 逐路径的输出只在 `rl run register`、`rl run show RUN-x` 和 `rl run show ATT-x` 里打印。`rl run show E-x` 每个 attempt 一行，附简短的提示：一个实验可能有几十个 attempt。
- 记录格式不变，仍是 4。attempt 上的检查结果里，`changed` 的含义不变，另加 `absent`、`tests`、`seen_by`、`per_file`、`not_passed` 几个键。旧 attempt 保留登记时记下的结果，不迁移。Ticket 加了可选字段 `resolved_by`、`resolved_reason`、`resolved_at`，旧记录不需要补。
- `rl review resolve` 只有协调者能用。RV-y 必须是沿 `--recheck-of` 链在 RV-x 之后、并且通过的审核，新一轮的复查也算。已经通过的审核不能 resolve，同一个审核 resolve 第二次报 CONFLICT。`review approve` 照常执行，之后已有通过复查时，输出加一条提示。
- 在 bypass-lora 副本的 102 个 stale attempt 上，用新规则重新归类它们登记时的路径：每个 attempt 按它的提交，只算它登记之前的审核。27 个不再是 stale，75 个仍是。路径按 attempt 计，changed 281 个（其中 224 个附带 `seen_by` 的提示），absent 57 个，tests 182 个。和上文的表一致：D-0016 的 ATT-0154-1 全是 absent，D-0049、D-0054、D-0055 只有测试文件，这些都不再是 stale。D-0017 的 src/v2_train.py 仍是 stale。D-0053 仍是 stale，但提示了 `--review RV-0077`。D-0061 的 src/bypass_model.py 提示了 RV-0078、RV-0079，src/rb_sink_read.py 仍是 changed（运行时不执行它，rl 判断不了）。

**第 7 项**：

- `rl decide` 只给 `--rationale-file` 时，短理由取文件第一行非空文字，去掉 Markdown 标题的 `#`。两者都没给、或者文件里没有文字时报错，两者都给时行为不变。
- `based_on` 可以指向 Decision，并且标为不许成环，`rl link` 加的也一样。复查标记只从改了有效性的证据或被拒绝的结果出发走一步（recheck_dependents），本来就不沿决定之间的引用往下传，不用另加规则。
- `rl inbox --ack MSG-x` 只确认到这一条。这条不是发给该角色的报 INVALID，已经确认过的不再改动游标。不带 id 的 `--ack` 照旧。
- `rl agent tell --file <path>`，文字和 `--file` 只能给一个。
- `rl hypothesis list` 按层次编号排序，`rl evidence list` 按 id 排序，每条证据写出它支持或挑战的假设和修订。可按证据状态、处置、有效性、级别过滤，`--json` 给记录。
- `rl export --records-only` 把 snapshot.json 的内容打到标准输出，不建目录，也不复制封存文件，和 `--out` 一起用时报错。加 `--json` 时直接输出这些记录，不装进 summary 字符串里。
- 锁：没有量到具体的占锁时长，事件日志里没有时长。按代码看，写事务里慢的是 review request 调的 `git diff`（上限 30 秒），已挪到锁外。材料包仍在写事务里写，因为它要用新分配的审核 id，写一个文件很快。写入方拿到锁后，把进程号、主机、命令和开始时间写进锁文件，释放前清空。读者不写。等不到锁时，报错写明等了多久、是要读还是要写、谁占着锁：写入方的进程，或者“没有写入方，是读者占着”。同一主机上记下的进程已经结束时，也说是读者占着。flock 不让等待的写入方优先，读得很频繁时写入方可能一直等，这时报错会写出是读者占着。没有加整条命令的自动重试。
- arm 校验放在准备封存契约之前，直接读草稿的 `arms:`，命令被拒时不留文档对象，和第 5 项的做法一致。`arms:` 不是名字列表时报错。只有新 run 校验，同一 run 的新 attempt 用 run 已封存的契约，不再校验。报错里的实验用层次编号。导入时 arm 不一致，提示写出两边的名字和两条出路。
- 作业号更正：attempt 到执行引用的 bound_to 关系上记下带来绑定的观测（`observation`）。`--correction-of` 指的正是这条观测时改绑，并记一个 run.rebound 事件。更正本来就要求 `--note`。其他情况仍报 CONFLICT，提示分两种情况写：记错了用 `--correction-of`，重新执行就开新 attempt。以前的绑定没有记这个属性，不知道是哪条观测带来的，所以不能改绑，报错里会说明。记录格式不变，只是关系上多了一个可选属性。
- 资源的 jobs 是 `mcp:` 或 `skill:` 时，`rl run record --attempt ATT --exec-id ..` 的输出加一条提示。
- 在 bypass-lora 的副本上：`rl hypothesis list` 2.3 秒（33 条），`rl evidence list` 3.7 秒，`rl export --records-only` 30 秒，输出 11.6 MB（10769 条记录、3717 条关系），期间一直持有读锁。它和普通 export 读的是同一份数据，这次没有另做优化。

**第 8 项**：

- 第 10 条：advise 材料包里的产物索引原来只有证据自己的引用和封存文档，现在加上证据所取自的结果在 manifest 里列的文件，检查点也在内。每个产物标一个状态：在、不在、没有读取权限、远程未检查（`<资源>:<路径>` 或 URL）。只对本地路径做一次 stat，不读内容、不算摘要、不下载。没有读取权限的不写成不在。不在和没有权限的放在材料包前部，排在问题和约束之后。问题和约束总会保留。预算不够时，有效证据、诊断和已停的路线这三节先压缩，各自保留放得下的最新条目，并标出略去了多少条较早的。以前预算不够时整节略去，约束排在最后，最先被截掉。
- 第 12 条：`rl task deliver --verified "<命令> @<提交>: pass|fail, log <路径>"` 可以给多次，格式不对时报错并给出例子。验证记在任务上（Task.verified，新的可选字段，记录格式不变），`task accept` 显示最近三条。任务有自己的分支、分支上有基线没有的提交时，下面两种情况给一条提示：一条验证记录都没有；或者分支 head 不是任何一条通过的验证所在的提交（按前缀比较，所以短提交号也行）。不拦 accept。没有分支的任务只显示记录，因为判断不了它改没改代码。
- 第 13 条：attention 里只有两类条目陈述事实，可以确认：契约在出结果之后又改了（条目带契约修订号），以及结论审核之后又挂上的证据。这类条目带一个键，取条目原文（内部编号）摘要的前 8 位。事实一变（契约有新修订、又挂了证据），原文就变，键也跟着变，条目重新出现。`rl attention ack <键> --reason ..` 把键记在 Meta 的 attention_acks 里，并记一个 attention.ack 事件，挂在条目所说的记录上，`rl history E-x` 能看到。其他条目（需要复查、审核缺口、没结束的审核轮次、要核实的 attempt、插件变化）是还没满足的条件，没有键，确认不了。键不用可读的名字，因为条目显示时其中的编号会换成层次编号，可读的键也会被一起换掉。
- 第 15 条：Miyabi 计算节点上的 TeX Live 有 fontspec，没有 xeCJK、ctex 和 luatexja，中文字体只有 Droid Sans Fallback。docs/configuration.md 写的测过的配置是 xelatex，按文件指定字体，加 `\XeTeXlinebreaklocale "zh"`。按族名指定会找错：`Droid Sans` 也是这个字体文件的族名之一，xelatex 却用了只有拉丁字母的 DroidSans，PDF 里的中文全是缺字，抽取检查当场报了错。“”在 Droid Sans Fallback 里没有字形，文档建议用「」或者换字体。检查从 `.tex` 正文（去掉注释）里均匀取最多 8 段、每段至少 4 个字的连续中日文字，标点不算，因为各字体和抽取器对标点的处理不一样。pypdf 读不出文字时也报 BUILD，并写明原因。新增依赖 pypdf（>=6.21.4），launcher 在项目里下一次运行 rl 时会用 uv add 加上它。`rl manuscript` 在当前 shell 里编译，登录节点上也一样，配置说明写了该在哪里跑。
- 第 16 条：结论审核的材料包列出当前假设修订的 nearest_work，包括论文标题、arXiv 号或 DOI、年份、评估、位置和备注。当前修订没有时写明空缺，并说明有几条挂在旧修订上、没有沿用：假设修订之后要重新 link。材料包的内容变了，所以改动之前的审核请求不会再被复用。
- 第 17 条：SKILL.md 开头写明协调者不改 src/ 和测试，自己的检查只读或运行；改一处断言也是任务；紧急修复先征得用户同意。为了不超过 150 行，docs/ 那一条压成了两行。
- 只读模式：`rl --read-only`。
  - Store 拒绝写入。记录格式比 CLI 旧时，说明要先升级，然后以 READ_ONLY 退出，不做迁移。锁文件以只读方式打开，不存在时不创建，也不建 state 目录。
  - 会写文件的命令（view 渲染、report、manuscript、写目录的 export、upgrade、init、hook 等）在开始前就被拒绝，判断按 cli.READ_ONLY 里的白名单。
  - launcher 在 `--read-only` 下不跑 uv add：项目环境的依赖过期时，照旧用现有的 `.venv`（rl 的代码总是插件里的那份），没有 `.venv` 时用插件自己的环境。`rl init --read-only` 和 `rl setup --read-only` 直接拒绝。
  - 只读下不写状态栏缓存（`rl usage statusline`）。
  - 测试里对一个项目跑一遍只读命令，项目里每个文件的字节都没变，包括数据库和本来就不存在的锁文件。
  - 设计里的“不写心跳”不用另外处理：watcher 的心跳（Meta 的 watch_beat）和会话记录只由 `rl agent watch`、`rl agent check` 这类写命令写，只读下这些命令都被拒绝。

**收尾**：

- SKILL.md 补上第 3 项留到最后的两条规则。
  - 编号：命令既接受层次编号，也接受内部编号。和用户说话、写说明和决定时用层次编号，路径、作业名和提交信息里用内部编号。
  - `--root`：派生的假设默认编在来源之下，开新研究线时加 `--root`。
- Finish a stage 的步骤依次是：`decide --stage`，`rl view --todo`，写或改说明，`rl view [--stamp <编号>]`，`rl report` 和 `rl manuscript`。`--stamp` 本身会渲染页面，所以不再单列 `rl view`；没有要 stamp 的说明时，直接跑 `rl view`。
- 为了不超过 150 行，`hypothesis revise` 那条压成了一行，`rl report` 后面删掉了“(a stage report in research/reports/)”，因为项目布局一节已经写了阶段报告放在 research/ 下。
- 第 12 条第 8 项漏改了一处：herdr 会话和 `rl delegate` 用的 brief（delegate.py）也要写上 `--verified`，第 8 项只改了 Claude 的 researcher 子代理说明。现在两处都写了，测试检查两种 brief 都带这个格式。
- agentlog.py 里第 1、2 项留下的一个未用的 import 已删掉。

## 附：核对来源

- 代码：smooth-research 73d0124，正文各处的 file:line。
- bypass-lora 记录：以只读方式打开 `research/state/research.lattice` 查询（没有经过 rl，避免触发升级）。用于第 5、8、9、14 条的决定、观测、任务和审核记录，层次编号的模拟，以及事件计数：session.reminded 只有 2 次，都是 reviewer-1，watch 发给协调者的 NOTE 共 4 条。
- codex 日志：`~/.codex/sessions/2026/11/02/rollout-2026-11-02T18-23-23-01a19b92-0eb8-7782-a636-d74a591227b1.jsonl` 第 979 至 982 行。
- 协调者会话：`~/.claude/projects/-work-xg24i002-x10041-bypass-lora/` 下全部会话记录里的 Monitor 调用，按 tool_use id 去重后，状态循环 82 次，等 working 的检查 13 次。命令原文见 `d2bf5e61-b388-4c0f-8994-99653f699b02.jsonl`。
- researcher-2 会话：`~/.claude/projects/-work-xg24i002-x10041-bypass-lora-worktrees-researcher-2/e66eff99-b433-44aa-951b-0e625f83f11e.jsonl` 第 836 至 912 行，第 8 条的经过。
- herdr：`herdr status`、`herdr integration status`、`herdr agent explain w1:p2T`、`~/.codex/hooks.json`、`~/.codex/herdr-agent-state.sh`（v10）。
- codex 的审核：`reports/DOING/design_review/codex_73d0124_bypass-lora-improvements.md`。本稿采用的关键事实都另行核对过，标“codex 核对”或“codex 统计”的几处除外。
