# 协调者上下文负载：问题分析与新设计建议

日期 2026-11-09。插件版本 `a8a70a9`，文中行号都对应这个版本。第 5 章以后是建议，尚未实现，执行时按实际情况调整并更新本文。

## 1. 核心内容总结

Bypass-LoRA 的一个 Codex 协调会话在约 33 小时内发出 2,558 次请求，平均每次输入 16.3 万 token，占审计可见 12.3 亿 token 的 34%，Claude 会话未计入。两份报告对插件行为的描述与当前源码相符。

设计上的根因有五处。消息的记录和唤醒绑在一次调用上，WAIT 也会打断协调者。等待作业归协调者的规则定于 researcher 还是子代理的时期，改成常驻会话后没有调整，也和 miyabi-mcp 的"通知归提交者"相反。首审分配、批次放行和机械验收都要经过协调者的模型。CLI 输出和协调者上下文都没有上限。写命令同步渲染页面，造成锁等待。

建议把消息转发、调度、逐步审批和重复验收移出协调者：消息分唤醒和静默两类，作业事件归任务负责人，空闲 reviewer 自动接单，任务携带一次性授权，交付时生成确定性核验，协调者在阶段边界换新上下文。先补计量，再改路由。researcher 会话平均每次请求输入 16.9 万，所以节省主要来自去掉协调者这一跳，效果要用下一阶段的数据检验。

## 2. 证据范围

两份报告引用的插件行为和行号已逐条对照源码，都成立。各来源能支持的结论范围如下。

| 来源（前四项在 `bypass-lora/` 下） | 提供的事实 | 使用限制 |
|---|---|---|
| `docs/bug-report/codex-token-retrospective-2026-11-08.md` | 70 份 Codex 日志的逐请求 usage，共 7,638 次请求 | 不含 Claude 会话；不是账单或配额换算；流程原因没有逐 token 归因 |
| `docs/bug-report/2026-11-08-coordinator-context-load-from-routine-messages.md` | 协调者收件箱 856 条；窗口内 280 条、约 45.5 万字符的分类 | 内容分类靠关键词，是近似值；字符比例不等于 token 比例 |
| `research/notes/closeout-codex-token-audit-2026-11-08.json` | 每个会话的请求、turn、compaction、工具调用和字符数 | 角色只能从工作目录推断 |
| `docs/bug-report/2026-11-08-research-view-cpu-and-reader-lock.md`、`2026-11-07-stale-coordinator-target-and-rl-read-latency.md` | 页面渲染、锁等待和通知目标失效的观察 | 没有函数级 profile |
| smooth-research 源码和 git 历史，miyabi-mcp 的 `DESIGN.md`、`README.md` | 行为和设计决定的依据 | 只读核对，没有运行新流程 |

审计 JSON 按工作目录拆分后的结果如下，报告里没有这张表。

| 会话组 | 会话数 | 请求 | turn | compaction | 总 token | 占比 | 平均输入/请求 | 请求/turn | exec/turn |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 协调者会话 | 1 | 2,558 | 116 | 34 | 4.18 亿 | 34.0% | 16.3 万 | 22.1 | 18.7 |
| researcher worktree（含 16 个原生子代理） | 29 | 3,334 | 215 | 17 | 5.66 亿 | 46.0% | 16.9 万 | 15.5 | 12.4 |
| 项目根目录的 CLI 会话 | 38 | 1,661 | 147 | 11 | 2.36 亿 | 19.2% | 14.2 万 | 11.3 | 9.8 |
| 项目根目录的 VS Code 会话 | 2 | 85 | 3 | 0 | 0.09 亿 | 0.7% | 10.5 万 | 28.3 | 23.3 |

这张表不能当作同一时段的角色对比。worktree 会话全部出现在 11-01 至 11-03，协调者会话在 11-07 至 11-08；当前配置把 Claude 列为 researcher 首选，后期的 researcher 大概率不在这批日志里。表里能确定的是 researcher 会话的上下文同样可以长到 16 万以上，单个 researcher 会话最高用了 1.33 亿 token。下放工作时不能假设 researcher 的一次唤醒很便宜。

## 3. 成本结构

协调者会话的输入量可以写成三项的乘积：

```
116 turn × 22.05 请求/turn × 16.27 万输入/请求 ≈ 4.16 亿 token
```

turn 数取决于消息路由，每个 turn 的请求数取决于一次唤醒要核对多少东西，每次请求的输入取决于工具输出和会话寿命。协调者的特点在第二项：每个 turn 的请求数比 researcher 高四成，全部请求又集中在一个持续 33 小时、压缩过 34 次的会话里。

缓存命中率已经是 98.13%。缓存输入 4.08 亿是非缓存输入 778 万的 52 倍，粗略地说，进入上下文的内容平均要被重复读几十次，所以应该减少进入上下文的内容和请求次数。工具返回累计 1,424 万字符，占该会话文本字符的约 97%，平均每次 exec 约 6,600 字符；普通 assistant 文本只有 6.3 万字符。

一次例行唤醒的量级可以这样估计：报告观察到的典型动作是重读 SKILL.md 和 AGENTS.md，再运行 `rl show MSG-x`、`rl show DOC-x` 和 `git diff`。这些动作分开执行时，加上收尾回复约 6 次请求，按平均上下文算接近 100 万输入 token。这是示意值，实际取决于当时的上下文大小和并行程度。

路由改动能直接影响的消息字符，上限约 60%：作业流水 45%、`RV-x needs a reviewer` 10%、reviewer 消息 3%、researcher 转述 2%。其中一部分（比如终轮结论）仍要保留，这个比例也不能换算成 token 节省。

## 4. 当前设计的问题

### 4.1 消息的记录和唤醒绑在一起

`tasks.progress` 写入 RESULT、WAIT、BLOCKED 后统一调用 `messages.notice`（`src/smooth_research/tasks.py:164-174`）。`messages.deliver` 把通知敲进收件人的 pane（`messages.py:55-81`），协调者因此开始新回合。`Message` 没有表示紧急程度的字段（`schema.py:100-102`）。唯一不打断的情况是收件人停在对话框，而这个暂存逻辑只对 rl 打开的会话生效（`messages.py:77`，协调者没有 `herdr_agent`）。

窗口内有 72 条 WAIT，按代码每条都会向协调者发通知，很多和同一分钟的 NOTE 内容重复。T-0065 的 8 个训练臂有 7 次 guard 拒绝，协调者为此收到 MSG-1573 到 MSG-1621 共二十多条消息。同一时刻的多条消息各发一条通知（MSG-1593/1594），没有合并。

### 4.2 等待的归属停留在子代理时期

`c2686cf`（10-30）定下等待归协调者。当时 researcher 是原生子代理或 headless 会话，提交作业后进程就结束了，必须有别的角色接收通知。第二天 `eb83554` 把 researcher 改成常驻的 herdr 会话，这条规则保留了下来；11-06 的 `433a50b` 又把协调者的 herdr 目标写进了工作包。规则现在分布在 `plugin/skills/smooth-research/SKILL.md:77-79`、`references/workflow.md:201-209`、`delegate.py:106-110`、`context.py:156-167`、两个 `host.md` 和 `hosts/claude/agents/researcher.md:32-35`。

miyabi-mcp 在 11-05 定了相反的规则（`DESIGN.md` 9.7 节 D5，`README.md:313`）：通知归提交作业的 agent，"结果出来后要接着干活的是它"，协调者用 `notify_list`、`job_get` 看状态。两边现在互相矛盾。结果是一个作业结束要唤醒两个模型：协调者收到通知后转告 researcher，researcher 再去处理。协调者这一跳主要是转发和确认，却要带着十几万 token 的上下文。

执行器已经具备这个场景需要的能力：一个通知绑定一组作业，全部结束后发一条汇总，任一成员失败时提前单独通知（`DESIGN.md` 9.2 节）。`c2686cf` 的配方原本有批次语义（"the first message is either an early failure of one job or the batch summary"），`73d0124` 把配方改成执行器无关时删掉了，当前 `workflow.md` 的等待步骤不再提批次。

通知目标也有差别。协调者的目标是 pane id，11-07 曾失效（`w1:p2T`）；rl 打开的 researcher 会话用稳定的 agent 名称，重启后不变（`watch.py:590-640`）。watcher 的无消息提醒同样直接发给协调者（`watch.py:665-693`，报告问题 6）。

### 4.3 审核路由和 brief 的缺口

只有同一轮的复查会自动路由（`reviews.py:184-197`）。首次审核的 requester 是 researcher 时，一律发 NOTE 给协调者（`reviews.py:198-201`），消息里的 `brief()` 带工单问题全文（`research.py:655-658`）。窗口内 16 条，约 4.4 万字符，而协调者只需要决定派给谁。

报告把问题 3（reviewer 中间轮次汇报）和问题 4（researcher 重复发 NOTE）归为 agent 习惯。agent 确实超出了 brief，brief 本身也有缺口：

- reviewer brief 第 6 条写 "When the round is finished, tell the coordinator"（`agents.py:518`），没有定义轮次何时结束。第一次审核答完时，reviewer 无法知道 requester 是否还会复查，而 CLI 能判断这一点（`reviews.py:393`）。两份报告没有直接证据说明这是提前汇报的原因。
- researcher 工作包对 deliver 写了 "The coordinator gets a notice"（`delegate.py:81`），对 `task wait` 没有写（`delegate.py:106-110`），也没有说明不必再发 NOTE。
- `rl agent tell` 接受任意收件人（`agents.py:297-310`）。MSG-1176 发给了 `S-0034`，协调者的 inbox 里看不到。

### 4.4 批次放行没有结构

`Task` 有 `request_ref`、`next_action` 和合同绑定，没有字段说明 researcher 在什么范围内可以不问协调者（`schema.py:87-91`）。工作包的 WAITING 段把任务之外的步骤和追加预算的请求都指向协调者（`delegate.py:33-40`）。报告把 preflight GO 和 I1 替换单独出决定归为项目自定流程（问题 5），这个判断成立。任务请求文档（`--request`）可以写这些规则，但没有固定位置，工作包和 brief 也没有说明范围内不必询问。复盘把一次说清批次授权列为 P0。

任务粒度放大了这个问题。bypass-lora 收尾时编号到 T-0074，每个任务都有固定的协调者步骤：创建、开会话或分配、检查交付、验收、合并。

### 4.5 验收依赖模型手工核对

`SKILL.md:80-82` 要求协调者检查 RESULT、相关记录和输出后再 accept。没有命令汇总这些事实，`tasks.verification_lines` 只看测试记录（`tasks.py:222-237`）。`research-review` 的 skill 写着确定性检查应该放进脚本（`plugin/skills/research-review/SKILL.md:102`），插件没有提供这样的脚本，每次都由模型手写或逐条读取。

抽样的 16 次唤醒中有 11 次重新读取了 SKILL.md 和 AGENTS.md。SKILL.md 有 12.8 KB，`workflow.md`、`team.md`、`host.md` 合计约 50 KB。具体原因没有验证，可能和 34 次 compaction 有关。`rl inbox` 列出未读全文，`rl status` 列出全部 attention，没有一个有界的入口只显示现在要决定的事。

文档修订也走这条链路：T-0043 改到 r4，T-0069、T-0070、T-0071 改到 r3。`rl review request --files` 的指纹逻辑不限制文件类型（`scope.py` 的 `fingerprint`），现有审核轮次可以承接文档审稿，skill 里没有写这种用法。

### 4.6 工具输出没有预算

工作包、恢复包和咨询包都按 token 目标裁剪（`context.py:90-122`），其他输出没有这个机制：

- `rl inbox` 打印每条消息的全文（`messages.py:163-168`）。加 `--json` 时正文出现两次，`messages` 字段一次，`summary` 字段一次（`cli.py:1106-1107` 打印整个结果）。
- `--summary` 和 `tell` 的文本没有长度限制。窗口内作业流水类消息平均约 2,000 字符一条。
- 通知正文最多 600 字符（`messages.py:18`），结尾让收件人去读完整 inbox（`messages.py:70-72`）。
- `rl show` 打印记录的全部字段；`rl export --records-only` 在 bypass-lora 上输出 11.7 MB。

### 4.7 上下文没有生命周期

`host.md` 只有一句 "at a natural task boundary with a heavy context"（codex 版第 44 行），没有触发条件、格式和长度要求。项目自己的 `restart-coordinator-handoff.md` 不断前插最新状态，重启成本越来越高。`start_session` 命中同一 host session 时直接复用记录，不更新 pane（`tasks.py:32-35`），11-07 的目标失效就出在这里。

watcher 读 Codex 日志时已经经过 `token_count` 和 `token_usage_record`，只是当作非进度事件跳过（`agentlog.py:28-30`）。这些记录里有每次请求的输入量，可以近似上下文大小，rl 目前没有使用。researcher 和 reviewer 的会话也需要处理：`rl agent reset` 完全依靠协调者记得执行。

### 4.8 写命令同步渲染页面

`cli.py:1100` 在命令成功后同步调用 `_refresh_page`，影响 `hypothesis`、`experiment`、`evidence`、`decide`、`link` 和 `run import-result`（`cli.py:33-34`）。`explain.render` 在一个读事务里完成全部检查和页面生成（`explain.py:277-278`），期间持有共享锁，写命令最多等 60 秒后报 LOCKED（`store.py:27`）。

报告实测 `rl view` 约 2 分 43 秒，`decide` 写入成功后刷新又跑了 1 分钟。两次中断时的 traceback 都落在 `_words` 和 `labels.resolve`。`labels.resolve` 每个 key 执行一次 Cypher 查询（`labels.py:87`，调用点 `explain.py:131`），`EXTRA_INDEXES` 里没有 label（`store.py:29-31`）。这只是线索：`rl export` 的时间 85% 是 sys，读路径可能另有问题。

写入已成功而刷新被中断或超时时，调用方看到的是失败，重试有重复写入的风险。项目临时靠挪走 HTML 规避，并发会话下存在竞态。`explain.attention` 还让 `rl status`、`rl resume` 和会话启动 hook 都执行一次 `_check`（`context.py:230`）。

### 4.9 没有 LLM 用量计量

`usage.py` 只读取订阅窗口，用于切换模型。复盘的数字来自一次性审计，协调者的唤醒次数也没有记录。没有这些数据，新设计无法和现状对比。

## 5. 新设计建议

### 5.1 设计规则

1. 协调者只接收会改变研究下一步的事项：任务验收、路线决策、超范围请求、BLOCKED、审核升级和跨任务冲突。
2. 外部事件归发起者处理。作业归任务负责人，审核轮次归 requester 和 reviewer。
3. 机械事实由 CLI 算出，模型判断解释和路线。
4. 进入模型上下文的输出都有上限，超出部分留在文件里。
5. 没有 herdr 的运行方式和旧项目继续可用。

复盘第 3 节的约束全部保留：不降低模型质量，不取消证据链，不把测试搬到登录节点。rl 也继续不查询执行器。

### 5.2 消息分为唤醒和静默

`Message` 增加可选字段 `wake`。`true` 的消息按现在的方式通知收件人；`false` 的消息只写记录，在 `rl inbox` 里按任务汇总。路由规则集中在一个函数里，`tasks`、`reviews`、`agents`、`watch` 都调用它。

| 发给协调者的事件 | 现在 | 建议 |
|---|---|---|
| `task wait`，负责人是活动的 herdr 会话 | 唤醒 | 静默；任务状态和 attempts 在 `rl resume` 里可见 |
| `task wait`，负责人是子代理或 headless 进程 | 唤醒 | 唤醒，只有协调者能重启它 |
| `task deliver` | 唤醒 | 唤醒，消息带核验结论（5.6） |
| `task block`、`task handoff` | 唤醒 | 唤醒 |
| 首次代码审核 | 唤醒并附问题全文 | 有空闲 reviewer 时静默分配；否则唤醒，只写工单号、requester 和 subject |
| 结论审核 | 协调者发起 | 不变 |
| 轮次结束且通过 | reviewer 自己发 | CLI 写一条静默记录 |
| 轮次用尽仍有 medium 以上 | 唤醒 | 唤醒 |
| watcher：会话退出、对话框、卡住、模型错误 | 唤醒 | 唤醒 |
| watcher：等待的作业长时间没有消息 | 唤醒 | 先提醒任务负责人，再升级（5.3） |
| `agent tell coordinator` | 唤醒 | 默认静默，需要回答时加 `--ask` |

`tell` 默认静默时，CLI 的返回要写明已静默记录、需要回答请加 `--ask`，agent 用错时能马上看到。漏加 `--ask` 的问题可以自愈：researcher 空闲两个 beat 后，watcher 会提醒它，再报告给协调者。

有两处必须一起改。`watch.unread`（`watch.py:695`）要只统计 wake 消息，否则静默消息仍会触发提醒。`watch._awaited_reply`（`watch.py:232`）要只认 wake 消息，否则 researcher 恢复任务后，最后一条静默 WAIT 会让它看起来在等回复，watcher 就不再提醒了。

同一时刻的多条唤醒消息，以后可以沿用 `messages._hold` 和 `release` 的方式合并成一条通知。

### 5.3 作业事件归任务负责人

```
researcher  提交一批作业，执行器的组通知投递到自己的 herdr 名称
researcher  rl task wait --attempts <该批全部 attempt>     # 写记录，静默
researcher  结束回合
执行器      任一成员失败时单独通知 researcher；全部结束后发一条汇总
researcher  job_get，rl run record，rl run import-result，按授权重提或继续
researcher  rl task deliver                                # 唤醒协调者
```

`Task` 增加可选字段 `wait_owner`。`rl task wait` 在任务负责人是活动的 herdr 会话时设置它，否则留空，走现有路径。需要改的地方：

- `context.work_pack` 增加一行，写明会话自己的 herdr 目标；`herdr_target` 已经可以用于任何会话（`context.py:136-153`）。`coordinator_line` 保留给没有 herdr 的路径和升级路径。
- `delegate.prompt_for` 第 4 步在 herdr 会话里改成通知发给自己，说明 WAIT 已有记录、不用另发 NOTE，并提示优先使用执行器的批次或组通知。
- `workflow.md` 第 6 步、`SKILL.md` loop 第 3 步和两个 `host.md` 按此改写，同时保留没有 herdr 时由协调者等待的写法。
- `watch.waits` 对有 `wait_owner` 的任务先提醒负责人，沿用 1 倍、2 倍、4 倍的退避；负责人不在，或到第二档仍没有新消息时，再写给协调者。
- `rl agent assign` 拒绝把持有等待任务的会话改派其他任务（`agents.py:255` 现在没有这项检查），除非显式放弃（`--release-wait`）。否则作业通知会送到已经在做别的事的会话。

资源分配失败的重提留在 researcher 内部，失败记录照常写入账本。协调者最后从 RESULT 和核验结果里看到 attempt 总数和失败数，复盘要求的原始失败和唯一 usage 不受影响。

researcher 被唤醒的次数不会增加，节省来自协调者不再为每个作业事件开一个回合。researcher 的上下文并不小（第 2 章），这部分成本由 5.8 处理。

### 5.4 审核路由

`_route` 改成依次尝试：同一轮的原 reviewer（已有）；同一任务或实验上一次的 reviewer，如果空闲；其他空闲 reviewer，优先选和作者不同模型族的。空闲指会话活动、herdr 状态为 idle 或 done、当前 assignment 已答完。上一工单和新工单无关时，先执行 `rl agent reset <reviewer> --clear`。都不满足时才请协调者分配。复查路径已经在 researcher 的进程里调用 `assign`，不需要新的权限模型。

reviewer brief 删除第 6 条，写明 requester 不是协调者时不给协调者发消息。`_store_answer` 在轮次结束时写一条静默记录。轮次用尽的升级保持不变（`reviews.py:422-431`）。

措辞、内部一致性和口径问题走同样的轮次，由 reviewer 和 researcher 解决，协调者只看最后一版。`research-review` skill 要增加这种用法。

### 5.5 任务授权

`rl task create` 增加可选的 `--mandate <file>`，文件随任务封存，显示在工作包的固定位置。格式示例：

```yaml
scope: "<合同和修订号，允许的臂和 seed>"
budget: "<上限，用合同的单位>"
retries: "<哪些失败可以重提，每臂最多几次>"
replacements: "<合同已规定的替换规则，按规则执行并记录>"
report_when: "<必须回到协调者的条件>"
```

CLI 不解析这些语义，它们和约束文档一样靠 agent 遵守。这个字段让已批准的批次、预算、替换规则和停止条件有固定位置一次写清，工作包同时说明范围内不必询问；researcher 超出范围时用 BLOCKED 或 `--ask` 上报。

`SKILL.md` 的 loop 第 2 步增加一条：含多个 run 的任务要写授权，同规格的批次尽量合成一个任务。已封存合同内的运行是否取消 preflight GO 属于项目流程，重启 bypass-lora 时由用户决定。

### 5.6 交付时的确定性核验

新增只读命令 `rl task check T-x`。`rl task deliver` 也自动执行一次，把一行结论写进 RESULT。有问题时 researcher 在交付时就能看到，交付不被拒绝。设想的输出：

```
T-x delivered by researcher-1 · branch sr/researcher-1/T-x, <n> commits ahead
attempts   <n> ended: <n> success, 0 needs-check
results    <n> complete, usable for the planned comparison: <n>
review     covered <n>, stale 1 (RV-y: src/train.py changed)
tests      head <commit> passed
usage      <used> of <budget> node-h
notes      1 stale review
```

检查项包括 attempts 的状态和 needs_check、结果是否导入和完整、代码审核覆盖（复用 `runs.code_review_summary`）、分支 head 的测试（复用 `tasks.verification_lines`）、未决的审核轮次和用量对照（不同单位不相加）。JUnit、哈希表、ledger 这类项目特定的校验继续由作业内脚本完成，写进 manifest 的 `validator` 字段，这个命令只读取结果。

`SKILL.md` 第 4 步改成先读核验结果，只对它标记的项和科学判断继续看。科学解释、合同变更和关键结论仍由模型审查。

### 5.7 输出预算

`rl inbox` 默认显示待处理的 wake 消息和例行消息汇总，例行动作的命令直接写在输出里，协调者处理这类消息不需要重读 skill。`--full` 显示全文。

```
2 to act on
MSG-a RESULT T-x from researcher-1: <one line>; check: 1 note. rl task check T-x
MSG-b NOTE from researcher-2 (asks): <the question>
7 routine, unread: T-x WAIT 1 · T-y WAIT 3, RV-y round closed · watch 1
```

其他输出的调整：

- `--summary` 和 `tell` 的文本超过上限（建议 WAIT 和 NOTE 500 字符，RESULT 1,500 字符）时，CLI 把全文封存成文档，消息里保留开头和文档引用，不拒绝写入。
- `--json` 去掉重复的 `summary`，增加 `--fields`。
- 所有命令有默认输出上限，超出时写入 `research/state/` 下的文件并给出路径。
- 通知正文缩到约 200 字符。

### 5.8 上下文生命周期

新增 `rl session rotate --note <file>`，由协调者在阶段边界运行。CLI 封存这份短状态（建议 1,500 token 以内，超出时提示），结束当前 Session，请 watcher 在协调者空闲时输入 `/new` 或 `/clear`。新对话从 `rl resume --role coordinator` 开始，只显示最新一份状态，旧状态留链接。`start_session` 命中同一 host session 时要更新 pane。

watcher 读取 Codex 日志的 compaction 次数和上下文大小，超过阈值时写一条静默提示，建议在下一个阶段边界轮换。轮换会清掉用户在同一 pane 里的对话，所以只在协调者或用户明确运行命令时发生。Claude Code 的状态行目前只读 `rate_limits` 和 `model`（`usage.py:105-122`），需要确认能否取得上下文用量。

`rl agent assign` 给无关任务时默认先 `--clear`，相关任务保留上下文，`--keep-context` 可以关闭。判断依据是新旧任务有没有共同的目标实验或依赖。

### 5.9 页面刷新移出写路径

- 写命令只标记页面过期。`rl view` 显式渲染；`view.auto_refresh` 可选 `off` 或 `background`，`compute.coordinator: login` 时默认 `off`。
- 后台渲染由 watcher 在 beat 中触发，合并多次写入。
- 先做一次 profile，再决定怎么改读路径。两项小改动可以先做：`_words` 用一次 `labels.names` 建表，给 label 加索引。
- 把读取和渲染分开：短读事务里取出需要的记录，在锁外生成 HTML。

### 5.10 用量计量

新增 `rl usage report [--since <time>] [--by role|session|task]`：

- 读 Codex 日志的 `token_usage_record`，按 `response_id` 去重，不累加 `token_count` 快照，沿用复盘第 1 节的口径。
- 用 `Session.host_session_id` 映射到角色，用 assignment 的起止时间映射到任务。
- 附上 wake 消息数和 compaction 次数。
- Claude Code 的会话日志需要单独的解析规则。

## 6. 实施顺序

| 阶段 | 内容 | 对应报告条目 | 观察指标 |
|---|---|---|---|
| 0 | `rl usage report` 的 Codex 版本 | 复盘 P1 预算告警 | 能和 11-08 审计 JSON 对账 |
| 1 | `Message.wake`；WAIT 和普通 NOTE 静默；`--ask`；inbox 汇总视图；`unread` 和 `_awaited_reply` 联动；分配请求去掉正文；reviewer 和 researcher brief 修订 | 问题 2 部分、3、4；复盘 P0 唤醒和交付格式 | 无需决策的唤醒比例；重复消息数；消息长度 |
| 2 | `Task.wait_owner`；通知改发 researcher；组通知写回配方；watcher 先找负责人；空闲 reviewer 自动接单；轮次结束由 CLI 记录 | 问题 1、2、6；复盘 P0 单一运行负责人 | 每 attempt 的协调者唤醒数；异常漏报数 |
| 3 | `--mandate`；`rl task check` 和交付时自动核验；文档修订走 reviewer | 问题 5；复盘 P0 批次授权，P1 确定性核验、审查长尾 | 每批次人工 GO 数；手写临时检查数；每交付修订轮数 |
| 4 | `rl session rotate`；上下文提示；assign 清理无关上下文；输出上限和 `--fields` | 复盘 P0 阶段交接，P1 字段裁剪 | 平均输入/请求；compaction 次数；交接后返工 |
| 独立 | 页面刷新移出写路径；label 查询和索引；读路径 profile | 复盘 P1；reader lock 报告 | 单命令延迟；LOCKED 次数；重复写入数 |

阶段 1、2 改变 `tests/test_agents.py:300,378` 和 `tests/test_watch.py:382,391` 固定的行为，这些测试需要复核。配置和行为变化同步到 `docs/configuration.md` 和 `docs/smooth-research-design.html`。

评估时看所有角色的总量和每个被接受交付的用量，科学成功和 token 效率分开评价。

## 7. 兼容性

- `SCHEMA_VERSION` 从 4 升到 5，新增字段都可选：`Message.wake`、`Task.wait_owner`、`Task.mandate_ref`。增加迁移，冻结 `tests/fixtures/projects/schema-5`，保留 schema-2 到 4 的夹具。
- 旧消息没有 `wake`，按 `true` 读取。
- 升级时已在途的作业，执行器通知仍发给协调者；`wait_owner` 为空的任务走原路径。
- 原生子代理和 `rl delegate` 维持协调者等待。
- 建议保留一个版本的 `agents.routing: legacy` 开关，便于研究中途回退和对比。

## 8. 风险和待决定事项

| 风险 | 处理 |
|---|---|
| 静默消息里混入需要处理的问题 | 失败和阻塞类事件保持唤醒；用注入失败的测试确认；观察异常漏报数 |
| researcher 会话在等待期间丢失 | watcher 重启同名会话，herdr 目标不变；执行器对 `agent_not_found` 稍后重试；`waits` 第二档升级给协调者 |
| token 只是从协调者移到 researcher | 按角色和按交付看总量；5.8 的会话清理同时覆盖 researcher |
| 自动分配削弱审核独立性 | 优先不同模型族；无关工单先清上下文；relation 继续记录 |
| researcher 在授权内判断失误 | 授权写明上报条件；协调者验收和结论审查保留 |
| 轮换丢失尚未记录的判断 | 轮换前要写短状态；观察交接后返工 |

需要用户决定的事项，每项附建议：

1. `agent tell coordinator` 是否默认静默。建议默认静默，用 `--ask` 表示需要回答。
2. 新路由先在哪个项目试。建议先在 `sr-e2e` 下的联合测试项目（例如 `miyabi-joint-a`）跑通失败注入，再用于 bypass-lora 的下一阶段。
3. 轮换是否允许 watcher 自动触发。建议只给提示，由协调者或用户运行命令。
4. 是否保留 `agents.routing: legacy` 开关。建议保留一个版本。
5. bypass-lora 重启时是否取消已封存合同内的 preflight GO。这项改变项目流程，需要用户明确决定。

## 9. 未验证事项

- 新设计没有实现或试运行，也没有对照数据，所以不给节省百分比。
- Claude 会话的用量未知，同期的协调者和 researcher 无法比较。
- 重读 skill 的原因和 herdr 在 agent 忙时的通知行为都没有验证。
- 执行器按 agent 名称投递到 researcher 的路径没有实测。bypass-lora 的 PBS 通知实测到达过协调者的 pane，miyabi-mcp `DESIGN.md` 9.4 节仍把 herdr 投递标为待验证。
- 页面和读路径的性能问题没有函数级 profile。
