# Mark-L Agent

A lightweight AI agent implementation based on Claude Code. (参考Claude Code实现的轻量级AI agent)
基于飞书的多 Agent 协作框架，使用 Claude Agent SDK 驱动。通过飞书 WebSocket 长连接接收消息，支持多 Agent 编排、MCP 工具集成、业务知识库、权限管理。

## 架构

```
飞书 WebSocket（长连接）
    ↓
event_handler.py（消息解析、命令路由、卡片回调）
    ↓
Role Agent（意图识别 → 自动分发）
    ├── Ask Agent — 只读问答（代码 + 文档 + 知识库）
    ├── Dev Agent — 研发全流程（分析 → 修改 → commit → MR）
    └── Ops Agent — 监控查询、日志分析
    ↓
工具层
    ├── SDK 内置 — Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch
    ├── MCP — 飞书文档、多维表格、GitLab 代码、Prometheus 监控
    └── Safety Hooks — Bash Guard, Git Guard, Destructive Guard
    ↓
数据层
    ├── SQLite WAL — 会话持久化、使用量追踪
    ├── Knowledge — 业务知识库（biz/<domain>/knowledge/）
    └── Skills — 可复用工作流（skills/）
```

## 能力

| 能力 | 说明 |
|------|------|
| 知识问答 | 基于沉淀的业务知识库回答问题 |
| 代码分析 | 通过 GitLab MCP 远程读取项目代码 |
| 飞书协作 | 搜索/创建/编辑文档、查询多维表格、搜索用户 |
| 研发辅助 | 需求调研 → 方案设计 → 编码 → commit → MR |
| 运维分析 | Prometheus 监控、日志查询 |
| 上下文压缩 | 长对话自动压缩，控制 token 成本 |
| 权限管理 | 多级权限组，按用户/群聊控制 Agent 和工具访问 |
| 多业务域 | 支持多个独立业务域，各自拥有知识库和代码仓库 |

## 快速启动

```bash
# 环境初始化（一键）
./scripts/setup.sh

# 或手动：
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pyyaml setproctitle

# 配置
cp .env.example .env        # 填入飞书凭证 + Claude API Key
cp mcp.json.example mcp.json # MCP server 配置

# 启动
.venv/bin/python main.py
```

## 飞书命令

```
@bot <消息>              # 对话（群聊需 @）
/ask <项目> <问题>        # 知识问答（只读）
/dev <项目> <需求>        # 开发模式（可写）
/ops <问题>              # 运维分析
/clear                   # 清除会话历史
/stop                    # 停止当前请求
/help                    # 查看帮助
/model                   # 查看/切换模型（opus/sonnet/haiku）
/session                 # 查看当前会话状态
/admin stats             # 使用统计
/admin log               # 查看审计日志
```

## 项目结构

```
mark-l-agent/
├── main.py              # 入口：WebSocket 长连接、启动预热、信号处理
├── agent.py             # 核心引擎：Chat / Agent / Orchestrator 模式
├── event_handler.py     # 消息路由、命令解析、卡片回调、流式渲染
├── config.yaml          # Agent 列表、权限组、预算、日志级别
├── identity.md          # Bot 人设（注入 system prompt）
│
├── agents/              # Agent 定义（声明式）
│   ├── dev.py           #   Dev Agent：三阶段工作流 + Git 操作
│   ├── ask.py           #   Ask Agent：只读问答 + 知识路由
│   ├── ops.py           #   Ops Agent：监控 + 日志分析
│   └── role.py          #   Role Agent：意图识别 + 子 Agent 编排
│
├── core/                # 基础设施
│   ├── runner.py        #   Agent 执行引擎（Claude SDK 封装）
│   ├── card.py          #   飞书卡片流式渲染
│   ├── session.py       #   会话管理
│   ├── permissions.py   #   权限系统
│   ├── biz.py           #   业务域发现与上下文加载
│   ├── mcp.py           #   MCP 配置管理
│   └── ...
│
├── tools/               # 工具定义
│   ├── base.py          #   基础工具（Read/Glob/Grep/WebSearch）
│   ├── dev.py           #   开发工具（Bash Guard/Git Guard）
│   ├── feishu.py        #   飞书工具
│   └── ...
│
├── biz/                 # 业务域（按需创建）
│   └── <domain>/
│       ├── knowledge/   #   知识库
│       ├── context/     #   业务上下文
│       └── repos/       #   代码仓库
│
├── skills/              # Agent 技能
├── mcp_servers/         # 自建 MCP server
└── scripts/             # 部署脚本
```
