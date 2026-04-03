# Knowledge Base Specification

面向 AI Agent 消费的代码知识库结构规范。

本规范定义了如何为代码仓库生成结构化知识文件，使 AI Agent 能够高效理解系统设计、定位代码、回答业务问题。

## 核心理念

knowledge 文件的价值是**代码里看不出来的东西**——设计意图、跨模块关系、隐含约定、业务规则。

依据：
- 单个知识文件控制在 200 行以内，信息利用效率最高（LLM 对长上下文中间段信息利用率显著下降）
- 稳定内容（overview、index）与按需内容（topic）分离，前者适合注入 system prompt 并命中 prompt cache
- Agent 主要通过目录结构和文件名导航，好的命名比复杂的索引系统更有效
- 只写 AI 从代码推断不出的信息，避免冗余

## 目录结构

```
knowledge/
├── overview.md              # 系统全貌（注入 prompt，始终可见）
├── index.md                 # 路由表（注入 prompt，始终可见）
├── <name>.module.md         # 模块/组件主题（按需读取）
├── <name>.flow.md           # 业务流程主题（按需读取）
└── ...
```

**扁平结构**，不设子目录。文件直接平铺在 `knowledge/` 下。理由：
- 减少 Agent 目录探索层级
- 文件名本身就是导航
- 总文件数通常 5-10 个，不值得分子目录

## 文件类型

### overview.md — 系统地图

**定位**：Agent 对系统的第一印象。适合注入 system prompt，每次会话都可见。

**长度**：< 150 行 / ~2000 token

**结构模板**：

```markdown
# <系统名> 概览

## 定位
一句话：系统做什么，在技术架构中的位置。

## 技术栈
语言、框架、中间件、数据库。列表形式，不展开。

## 架构
架构风格，核心组件关系。
典型请求流转路径（1-2 行概括）。

## 核心模块
每个模块一行：名称 — 职责 — 关键入口类。
不展开实现，给 Agent 足够线索知道"去哪找"。

## 外部依赖
依赖的外部服务/RPC/API，每个一行。

## 关键约定
代码中不显而易见的约定、设计决策、历史包袱。
这是 overview 中最有价值的部分。
```

**不写**：完整 API 列表、DB 表结构、配置项详解（Agent 可以直接读代码获取）。

### index.md — 路由表

**定位**：指导 Agent 该读哪个文件、该搜索什么关键词。适合注入 system prompt。

**长度**：< 100 行

**结构模板**：

```markdown
# Knowledge Index

<!-- 生成元数据，供维护者判断时效性，Agent 无需校验 -->
<!-- 生成时间：YYYY-MM-DD | 源仓库：repo-a (a1b2c3f), repo-b (d4e5f6g) -->

## 问题路由

| 问题类型 | 推荐阅读 | 仓库 | 搜索关键词 |
|----------|----------|------|------------|
| 系统架构 | overview.md | — | — |
| XX 的执行流程 | xx.flow.md | repo-a | XxService, XxExecutor, processXx |
| XX 模块的设计 | xx.module.md | repo-b | XxManager, XxHandler |
| YY 跨仓库流程 | yy.flow.md | repo-a, repo-b | YyCoordinator, YyHandler |
| ... | ... | ... | ... |
| 其他 | 直接搜索代码 | — | — |

## 文件清单

| 文件 | 主题 | 行数 |
|------|------|------|
| overview.md | 系统全貌 | ~120 |
| xx.module.md | XX 模块 | ~150 |
| yy.flow.md | YY 流程 | ~100 |
```

**要点**：
- "搜索关键词"列是核心价值——告诉 Agent 该用什么关键词定位代码
- 元数据（生成时间、源 commit）集中在 index，不分散到每个 topic 文件
- "问题路由"面向 Agent 的决策，不是面向人类的目录

### topic 文件 — 按问题域聚合

**定位**：Agent 按需读取的深入文档，不注入 prompt。

**长度**：每个 80-200 行 / ~1000-3000 token

**命名规则**：`<主题名>.<type>.md`，小写连字符

| 后缀 | 含义 | 示例 |
|------|------|------|
| `.module.md` | 模块/组件 | `data-sync.module.md`、`election.module.md` |
| `.flow.md` | 业务流程 | `dividend-execution.flow.md`、`split-coordination.flow.md` |

命名面向问题域，不面向代码类名：
- 好：`dividend-execution.flow.md`（回答"分红怎么执行"）
- 不好：`stock-dividend-executor.md`（只是一个类的文档）

**内容结构**：

```markdown
# <主题>

> 本文档回答：关于 XX 的设计和实现问题。涉及模块：A, B, C。

## 核心流程
调用链 + 数据流转，标注具体类名和方法名。
例：OrderController.create() → OrderService.process() → SettlementEngine.settle()

## 设计决策
为什么这样设计，有哪些权衡。

## 隐含约定
代码里看不出来但必须知道的规则。

## 已知问题
当前局限和技术债。
```

**不写**：方法签名列表、配置项说明、逐步代码解读。

## 多仓库处理

当知识库覆盖多个代码仓库时：

- `overview.md` 覆盖所有仓库的整体架构和仓库间关系
- topic 文件名加仓库前缀，双横线分隔：`{repo}--{name}.{type}.md`
  - 例：`backend--data-sync.module.md`
  - 单仓库时省略前缀
- topic 可以跨仓库（一个业务流程涉及多个仓库很正常），此时不加前缀，在文件内标注仓库
- index.md 的搜索关键词标注所属仓库

## 文件数量控制

每个知识库控制在 **5-10 个** topic 文件：
- 合并关联度高的主题（不要一个类一个文件）
- 不到 80 行的 topic 考虑合并到相近主题
- 超过 200 行的 topic 考虑拆分

## 质量标准

| 维度 | 标准 |
|------|------|
| 信息密度 | 每行都有信息价值，无填充文字 |
| 可操作性 | 包含具体类名/方法名/文件路径，Agent 读完就能定位代码 |
| 非冗余 | 不复述代码能直接告诉 Agent 的信息 |
| 独立性 | 每个 topic 独立可读，不依赖其他 topic |
| 稳定性 | 不包含频繁变化的信息（版本号、精确行数等） |
