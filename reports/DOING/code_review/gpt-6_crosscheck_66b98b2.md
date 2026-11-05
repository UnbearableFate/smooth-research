smooth-research 交叉复核报告

复核模型：GPT-6（Codex）。时间：2026-11-05 16:20 UTC。对象：用户指定的 `review-fixes-66b98b2` 工作树当前实现，以及两份以 `66b98b2` 为基线的审查报告。

结论：同意 Claude 的 #2、#5、#6、#7 是缺陷，具体严重度和修复意见如下。我的 F1、F2、F5、F6 的针对性修复正确；F3 已修复普通路径下搬走数据库及直接写入保护目录的问题，但遗漏目标子目录符号链接；F4 已修复 review 的答案覆盖竞争，但同一答案写入机制中，advice 的回答者所有权检查仍有可复现的竞争。因此不能给出“所有相关边界均已充分修复”的结论。没有足够证据把这两项残留问题定性为本次修复新引入的回归。

本次未执行任何 Git 命令，包括 `git diff`；按要求，通过 CodeGraph、当前源码、上一轮已有证据、现有测试和临时项目验证进行对照。以下位置均为复核时当前工作树的行号，不是基线提交行号。复核期间观察到 Herdr、launch、watch 及部分测试继续变化，已复读相关调用路径，并在这些变化后补跑本次的 16 项定向回归；额外启动行为调整不等于已经过全量审查。

对 Claude 四项补充发现的判断：

| Claude 编号 | 是否同意、严重度 | 证据与对修复方式的意见 |
| --- | --- | --- |
| #2：`hypothesis set` 绕过结论审查 | 同意，P2／中 | `research.key_hypotheses()`（`:434–439`）和 attention 的检查（`:738–747`）从 Decision 追踪关键结论，直接修改 Hypothesis 状态不会产生对应 Decision。当前 `research.set_hypothesis()`（`:180–195`）拒绝 `KEY_STATUSES`，引导使用 `decide`，修复方向正确。需要修正报告措辞：`decide()`（`:596–605`）仍允许记录缺少审查的结论，但返回 review gap，并持续进入 attention；这不是“所有未经审查的结论一律不能写入”的硬门禁。此次修复补的是绕过追踪和提醒的入口。拒绝 supported/challenged、保留其他状态记账，范围合理；不自动改写历史记录也无异议，但不能宣称旧记录已被回溯补齐。 |
| #5：records-only 输出被 warning 污染 | 同意，P2／中 | 配置警告足以使重定向的 stdout 不再是单个 JSON；这会直接阻断程序化读取。`cli.cmd_export()`（`:165–167`）区分原始快照与显示包装；`cli.main()`（`:1078–1089`）现在将该模式的警告写到 stderr，并跳过给快照加 `warnings` 字段。现有回归验证普通输出和 `--json` 两条路径，均通过。修复方式无异议；我将严重度列为中，而非仅风格或低优先级显示问题。 |
| #6：watcher 退出竞争 | 同意，P2／中 | `agents.open_session()`（`:125–138`）先登记 active session 的 `herdr_agent`，再调用 `watch.start()`；旧的 beat 结果可能已过时。当前 `watch.run()`（`:854–862`）在清除 watcher 登记的同一写事务中重查 active herdr sessions，修复了这个丢失唤醒的窗口：会话先登记则 watcher 继续；watcher 先清除则后续 start 能重新启动。定向交错测试及原有退出／替换测试均通过。修复方式无异议。静默失去后续监视、恢复和通知，比普通显示瑕疵更重，因此列为中。 |
| #7：`CODEX_HOME` 两套解析 | 同意，P3／低 | 空字符串和 `~` 的解释不同，会让配置、模型日志与额度读取指向不同目录。当前统一到 `adapters.codex_home()`（`:150–152`）；sandbox 与 rollout 读取直接使用它，`usage.codex_sessions()`（`:51–52`）使用它，`agentlog.locate()`（`:35–40`）间接使用它。已验证未设置、空字符串、绝对路径、`~` 规范化的一致性，并用临时配置和 rollout 验证 sandbox／model／log 读取。helper 放在 adapters 而不是 Claude 建议的 usage，没有实质异议；不必为了这次修复强行把 rollout 扫描改成调用 `agentlog.locate()`。这里验证的是本项目读取逻辑，没有验证真实 Codex CLI 对所有环境值的解释。 |

对 F1–F6 的修复核对：

| 编号 | 当前实现与结论 | 验证及边界 |
| --- | --- | --- |
| F1 | `util.py:88–98` 的 `BOOT`／`rl_process()` 保留当前解释器，通过 `-I -c` 和独立参数传递源码目录；`reviews.py:207–214`、`delegate.py:174–182` 均复用。对原导入失败的修复正确。 | 在新建的依赖专用虚拟环境中，先确认 `find_spec('smooth_research') is None`，然后从与当前源码一致的临时副本进入 `_spawn()`。路径含空格，真实子进程退出码为 0，AD 票据为 pending／waiting for a manual answer。验证了源路径、解释器与参数传递组合；现有后台 entry 参数回归也通过。未调用真实模型，未把这次验证描述成真实 delegate 任务已完成。 |
| F2 | `view.condition()`（`:106–114`）加入 environment，`:158–159` 同时加入差异列；分组键与展示保持一致，修复正确。 | 新增的环境测试实际得到 CPU 一行 `0.7 (n=2)`、CUDA 一行 `0.2 (n=1)`；原有条件区分、重复尝试只计一次等回归也通过。没有引入把每个 seed 强制拆开的变化。 |
| F3 | `upgrade.move()`（`:215–231`）拒绝包含保护目录的源路径，并检查展开后的目标文件。原始 `research -> archive` 和普通 `stray/state -> research/state` 已被拒绝，但修复尚不充分。 | 保护路径、冲突及回滚测试通过；另复现了下文 C1：目标中间目录是指向保护区的符号链接时，逐文件检查仍可被绕过。 |
| F4 | `reviews._store_answer()`（`:384–401`）在写事务内重读并检查 review 终态，然后计算文件名、写答案、封存并登记；失败路径（`:309–316`）及 `_answered_meanwhile()`（`:328–338`）保留已接受答案，只追加 worker 调用记录。原 review 问题已修复。 | 首个答案胜出、拒绝者不覆盖文件、迟到失败、迟到成功、调用日志保留及 PID 占用回归均通过。迟到结果只追加 calls，不覆盖 verdict／answer_ref，方向正确。advice 的修订编号也在锁内计算，但“只有原 advisor 能修订”的检查仍在锁外，见 C2。 |
| F5 | `usage.hosts()`（`:205–209`）改成 `min(..., key=...)`，同值时选择第一个窗口，不再比较 dict。修复正确且与最小剩余额度的原语义一致。 | 双窗口同时为零、会话列表、阈值区分和 stale 读取测试通过；没有发现该修改引入新问题。 |
| F6 | `load_config(..., remember=False)`（`project.py:521–546`）只跳过缓存写入；`find_project()`（`:592–616`）的各返回分支传递只读意图，`cli._store()`（`:57–60`）在项目发现前传入参数。修复正确。 | 已有只读命令回归通过；补充检查配置变化且缓存存在、缓存完全缺失两种情况，命令成功、项目文件字节不变，仍读取当前 YAML 中的新值。正常 remember=True 路径与解析失败的回退逻辑被保留。 |

C1（P2）：逐文件保护检查没有解析目标路径的中间符号链接

位置：`src/smooth_research/upgrade.py:225–231`，实际移动在 `:259–261`。

`dst.resolve()` 只解析用户传入的目标根目录。由 `dst / f.relative_to(src)` 构造出的每个目标路径，没有再次解析其中可能存在的符号链接；`d.is_relative_to(root / x)` 只是路径组成上的比较。因此“文件会落入保护目录”的检查与实际 rename 的落点不一致。

在临时项目中执行以下确定性布局和调用：

```text
incoming/alias/probe.txt         普通文件，内容 crosscheck
research/alias                  符号链接 -> research/state
upgrade.move(store, 'incoming', 'research')
```

实际结果：

```text
moved incoming -> research: 1 file(s); recorded paths rewritten: none. Commit the move.
research/state/probe.txt 的内容：crosscheck
incoming 已被删除：True
```

这证明当前实现仍允许把迁移文件写入明文规定的保护区。此复现没有证明数据库被损坏，也没有把符号链接条件下的残留问题等同于原 F3 中直接搬走数据库的高影响结果。

建议在修改任何文件前，检查每个展开后的目标的实际解析位置，确认其仍在项目内且不在任一保护目录中；或者明确拒绝目标路径中会改变落点的目录符号链接。新增包含目标中间符号链接的拒绝测试，并断言源文件未移动、保护目录无新增文件。普通目录迁移仍应保留。

C2（P2）：两个 advisor 交错回答时，后到者仍能修订另一人的答案

位置：`src/smooth_research/reviews.py:437–460`，尤其 `:449–450`；最终写事务为 `:384–401`，其中 `_refuse_answered()`（`:348–352`）仅拒绝已经回答的 review。

`answer_live()` 在读取票据后，先检查 advice 是否已由其他 session 回答，再获取模型信息并调用 `_store_answer()`。最终写事务没有重新检查 `answered_by`。因此两个 advisor 都在 pending 状态下通过检查时，第二个可以把第一个刚刚提交的答案当作可修订答案。

本次使用两个真实存储中的 active advisor session，以及假的模型身份读取，在 B 的模型身份读取阶段先执行 A 的回答，再恢复 B。这个注入点位于 B 已完成前置检查之后、最终写事务之前；没有模型请求或 Herdr 通知。实际结果：

```text
A 的 session：S-0001
B 的 session：S-0002
最终 answered_by：S-0002
最终文件：AD-0001.revision-2.md
earlier_answers：['DOC-0002@1']
此后 A 再提交：AD-0001 was answered by another session; only that advisor revises it
```

后一句也证明“只允许原回答者修订”是当前显式实现的规则，不是审查者附加的偏好。交错时它被越过，原回答者还会失去后续修订资格。该问题不否定 review 终态修复；它是共用答案机制中仍未覆盖的 advice 所有权边界。

建议针对 live advice，在最终写事务内用最新的 `status`／`answered_by` 重新核验回答者，并在任何答案文件写入前拒绝不合法修订。保留同一 advisor 的正常修订，以及人工 import 另有定义的语义。测试应包含两个 advisor 交错、同一 advisor 修订和普通顺序拒绝三种情况，不能通过禁止全部 advice 修订来消除竞争。

本次验证范围与结果：

| 检查 | 结果 |
| --- | --- |
| 16 项现有定向 pytest 回归 | 最后一次为 **16 passed in 6.47s**。覆盖 records 2 项、explain 2 项、upgrade 2 项、reviews 5 项、usage 3 项、watch 2 项。首次运行同样通过，发现并发源码变化后才补跑。 |
| Ruff | `uvx --offline ruff check --isolated --no-cache --select E9,F src scripts` 通过；不是全规则 lint 通过的声明。 |
| 真实后台子进程 | 未安装 smooth_research 包的依赖专用环境中，manual advice worker 正常进入业务逻辑并退出。环境构建最初受离线缓存缺少 latticedb 影响，随后使用本机已安装的完整运行依赖复制到临时环境，最终验证通过；该准备失败不是产品回归。 |
| CODEX_HOME 与只读补充检查 | 路径默认／空值／绝对路径／波浪号规范化，以及配置变化／缓存缺失均通过。 |
| 两项残留问题 | C1 的实际保护目录落点和 C2 的 advisor 所有权竞争均已在临时项目中复现。 |

为满足“不要 Git 操作”，定向测试在进程内将文档的 Git 元数据查询替换为返回 None，并安装进程审计钩子阻止 Git 子进程；后台验证环境的 PATH 不提供 Git。替换仅发生在测试进程内，没有编辑源码。现有测试中的外部模型和 Herdr 使用替身，没有真实调用、付费工作、通知或远端执行。

没有重跑包含 Git 初始化／提交／工作树操作的完整测试集，也没有执行真实 Herdr、多代理或远端端到端测试。因此本报告确认的是上面列出的修复路径与定向行为，不是对整个当前分支作全量通过认证。建议补齐 C1、C2 后再给出“相关修复充分”的结论。本次仅写此报告，没有修改代码、测试或文档，也没有进行 Git 操作。
