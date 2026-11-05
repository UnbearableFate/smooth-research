# My Rules

## 通用规则

1. 优先使用codegraph获取代码库知识.
2. 自行使用uv管理所需依赖, 自行使用git管理代码库状态.
3. 避免在生成代码中使用SHA-256
4. 设计文档(位于design路径下)为事前制作的参考文档,并非权威, 实际执行中应当根据实际情况调整计划并对文档进行更新

## 参考代码库

- /home/yu/workspace/research-tree
- /home/yu/workspace/LoopLora

## 关于miyabi

- 位于miyabi之外的环境时, 使用pi-env-mcp 访问miyabi-g login node, 使用miyabi-mcp访问计算资源, 工作目录应该放置于`/work/xg24i002/x10041/`之下,为临时任务创建临时文件夹
- 位于miyabi环境时, 使用miyabi-mcp访问计算资源, 工作目录应该放置于`/work/xg24i002/x10041/`之下,为临时任务创建临时文件夹
- 不要直接使用miyabi-g* 系列login node进行测试.

