# Futu Paper AI 项目上下文

最后更新：2026-06-11  
本地目录：`/Users/liurunsheng/Documents/futu-paper-ai`  
GitHub：`laurunshen/futu_ai.git`  
服务器公网 IP：`54.161.53.16`  
服务器项目目录：`/home/ec2-user/futu_ai`

> 这个文件用于切换对话时快速恢复上下文。不要在这里写 Gemini API Key、GitHub Token、Basic Auth 密码、Futu 密码或验证码。

## 项目目标

这是一个 **Futu OpenD + Gemini + autoNews 的模拟盘交易学习系统**。

核心目标不是直接赚钱，而是：

- 用模拟盘学习交易判断。
- 让 Gemini 每次买入/卖出/观望都必须给理由。
- 接入 autoNews 新闻源，让 AI 根据行情、持仓和新闻做推理。
- 支持用户手动维护多个“本地模拟盘/组合”，即使富途模拟账户本身是空仓，也能让 AI 围绕真实持仓成本和仓位讨论。
- 后续可以扩展成多策略、多组合、多新闻源的自动复盘和模拟决策系统。

## 当前部署状态

服务器上使用 systemd 运行：

- `futu-opend`：Futu OpenD，监听 `127.0.0.1:11111`
- `futu-paper-ai-web`：Web 控制台，监听 `127.0.0.1:8787`
- `futu-paper-ai-loop`：Gemini 自动周期
- `autonews`：新闻采集服务
- `nginx`：公网入口，带 Basic Auth

公网入口：

```text
http://54.161.53.16/
```

本地 SSH key 路径：

```text
/Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem
```

常用部署命令：

```bash
git push origin main
ssh -o ConnectTimeout=10 -i /Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem ec2-user@54.161.53.16 git -C /home/ec2-user/futu_ai pull --ff-only
ssh -o ConnectTimeout=10 -i /Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem ec2-user@54.161.53.16 sudo systemctl restart futu-paper-ai-web
ssh -o ConnectTimeout=10 -i /Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem ec2-user@54.161.53.16 sudo systemctl restart futu-paper-ai-loop
```

状态检查：

```bash
ssh -o ConnectTimeout=10 -i /Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem ec2-user@54.161.53.16 systemctl is-active futu-opend futu-paper-ai-web futu-paper-ai-loop autonews
ssh -o ConnectTimeout=10 -i /Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem ec2-user@54.161.53.16 curl -s http://127.0.0.1:8787/api/portfolios
```

## 重要安全约束

- 项目当前只做模拟盘，不做真实交易。
- Futu OpenD 不能暴露公网，只允许 `127.0.0.1:11111`。
- Web 入口必须有认证。
- 不要提交 `.env`、`secrets/`、`*.pem`、`data/state/`。
- Gemini 可以给交易建议，但当前多模拟盘自动周期只记录“组合建议”，不会自动修改本地持仓，也不会向富途提交订单。
- 当前用户没有 A 股权限，因此 A 股暂时只保留代码能力，不作为主流程。

## 已实现功能

### Web 工作区

页面 tab：

- `总览`：账户、订单、Gemini 手动扫描、风控。
- `行情`：临时行情和自选行情。
- `模拟盘`：本地多组合管理。
- `AI 对话`：和 Gemini 围绕某个股票/行业/模拟盘对话。
- `AI 决策`：查看自动周期和手动扫描的决策记录。
- `新闻`：查看 autoNews 结构化新闻信号。
- `调试`：Gemini 用量和原始输出。

### 本地多模拟盘

新增模块：

```text
futu_paper_ai/portfolios.py
```

状态文件：

```text
data/state/portfolios.json
```

这个文件被 `.gitignore` 忽略，不提交。  
默认会创建一个：

```text
我的模拟盘
```

模拟盘支持：

- 新增模拟盘。
- 删除模拟盘。
- 设置当前模拟盘。
- 修改当前模拟盘现金。
- 添加/编辑/删除持仓。
- 持仓字段：代码、名称、数量、成本价、币种、备注。
- 前端会显示 OpenD 快照价、成本、市值、浮盈浮亏。

用户目前想录入的真实持仓示例：

- 腾讯控股 `HK.00700`：持有 1 手，成本 429 HKD。
- 阿里巴巴-W `HK.09988`：持有 3 手，成本 125 HKD。
- 拼多多 `US.PDD`：持有 20 股，成本 89 USD。

### AI 对话

新增模块：

```text
futu_paper_ai/chat_engine.py
```

关键设计：

- 对话页可选择“参考模拟盘”。
- Gemini 会收到选中模拟盘的持仓上下文。
- 如果用户提到股票代码或中文别名，会尝试识别：
  - 阿里/阿里巴巴 -> `HK.09988`
  - 腾讯 -> `HK.00700`
  - 英伟达 -> `US.NVDA`
  - 苹果 -> `US.AAPL`
  - 特斯拉 -> `US.TSLA`
- 当前价只能来自 Futu OpenD 快照：
  - `last_price`
  - `bid_price`
  - `ask_price`
  - `update_time`
- 联网检索和新闻中的价格只能当背景，不能当“当前价”。
- 如果 OpenD 没有给到某个标的的当前价，Gemini 必须明确说“当前价缺失”，不能编造或用网页价格替代。
- 输出上限可配置：

```bash
GEMINI_CHAT_MAX_OUTPUT_TOKENS=8000
```

代码保护范围是 `1024` 到 `20000`。

### 自动 AI 周期

入口：

```text
futu_paper_ai/auto_trader.py
```

当前自动周期行为：

- `ai-loop` 每 30 分钟运行一次。
- 会遍历所有本地模拟盘。
- 每个模拟盘分别调用 Gemini 决策。
- 决策写入 `data/decisions/YYYY-MM-DD.jsonl`。
- 决策记录里包含 `portfolio` 字段，前端决策页会显示属于哪个模拟盘。
- 当前模式是 `portfolio_decision` / `portfolio_suggestion`。
- 不会自动改本地模拟盘持仓。
- 不会向富途提交订单。

### TradingAgents-lite 决策增强

2026-06-11 已在现有 Gemini 决策引擎里加入第一版 TradingAgents-lite。

设计原则：

- 不把 `tauricresearch/tradingagents` 整体作为项目底座。
- 不引入 LangGraph / LangChain 作为主流程依赖。
- 保留当前 Futu OpenD、autoNews、本地模拟盘、风控和 Web 工作区作为主系统。
- 借鉴 TradingAgents 的多角色研究结构，让单次 Gemini 决策输出更像“研究小组审议”。

配置：

```bash
GEMINI_AGENT_MODE=multi_lite
```

当前实现仍是单次 Gemini structured output 调用，但 schema 增加：

- `rating`：`BUY` / `OVERWEIGHT` / `HOLD` / `UNDERWEIGHT` / `SELL`
- `position_action`：`ENTER` / `ADD` / `HOLD` / `TRIM` / `EXIT` / `WATCH`
- `research.market_analyst`
- `research.news_analyst`
- `research.portfolio_analyst`
- `research.bull_case`
- `research.bear_case`
- `research.risk_review`
- `research.manager_summary`
- `research.missing_data`

旧字段仍保持兼容：

- `action`
- `code`
- `confidence`
- `reason`
- `evidence`
- `risk`
- `invalidation`
- `max_notional`
- `time_horizon`
- `learning_note`

执行层仍只读取 `BUY` / `SELL` / `HOLD` 等旧字段，并继续通过现有风控和模拟盘安全边界。
前端 AI 决策详情页会显示“研究小组”、五档评级和组合动作。

手动扫描按钮现在也调用多模拟盘版本：

```text
POST /api/ai/once
```

CLI 也改成多模拟盘：

```bash
python -m futu_paper_ai ai-once --dry-run
python -m futu_paper_ai ai-loop --execute
```

注意：`--execute` 目前对本地多模拟盘只表示“请求过执行”，实际仍只生成组合建议，不提交订单。

### autoNews 集成

服务器推荐布局：

```text
futu app      /home/ec2-user/futu_ai
autoNews app  /home/ec2-user/autoNews
signal db     /home/ec2-user/news-data/news.db
```

Futu 项目 `.env` 中：

```bash
AUTONEWS_DB_PATH=/home/ec2-user/news-data/news.db
AUTONEWS_LOOKBACK_HOURS=24
AUTONEWS_MIN_IMPACT=60
AUTONEWS_MAX_SIGNALS=8
```

说明：

- 新闻页可以展示更多新闻。
- AI 决策每轮默认只拿有限条高相关新闻，避免 prompt 噪声太大。
- 新闻筛选逻辑优先匹配候选标的，其次自选/观察池，再是高影响宏观。

## 最近关键提交

```text
b50d776 Add multi-portfolio paper tracking
5968839 Make chat output length configurable
d18af44 Improve AI chat understanding
56a7cac Add AI chat workspace
f8ff9c6 Add decision news cards
7e9b210 Refine overview risk layout
88bc6d2 Make overview actionable
e3df8af Expand news signal view
```

## 主要文件地图

```text
futu_paper_ai/config.py          环境变量和运行配置
futu_paper_ai/futu_client.py     Futu OpenD 行情、账户、持仓、下单封装
futu_paper_ai/portfolios.py      本地多模拟盘状态管理
futu_paper_ai/chat_engine.py     AI 对话逻辑
futu_paper_ai/gemini_engine.py   自动决策 Gemini prompt 和 JSON schema
futu_paper_ai/auto_trader.py     自动周期、多模拟盘决策、决策日志
futu_paper_ai/news_signals.py    autoNews SQLite 信号读取和筛选
futu_paper_ai/web_server.py      HTTP API 和静态页面服务
web/index.html                   前端结构
web/app.js                       前端交互逻辑
web/app.css                      前端样式
data/watchlist.default.json      默认 100 支观察池
data/state/                      本地运行状态，忽略提交
data/decisions/                  决策日志，忽略提交
```

## 当前体验问题和后续想法

### 1. AI 价格准确性

已经收紧规则：当前价只能来自 OpenD。  
后续可以在前端 AI 回复旁边显示“本次使用行情快照”，让用户肉眼确认 Gemini 用的是哪组价格。

### 2. 本地模拟盘还不会自动更新持仓

现在自动周期只生成建议，不会真的把 BUY/SELL 应用到本地模拟盘。

后续可以增加：

- `应用建议到模拟盘` 按钮。
- 决策里的建议转成本地模拟成交。
- 本地模拟盘交易流水。
- 每个模拟盘收益曲线。

### 3. 多模拟盘策略

后续可以让每个模拟盘带策略标签，例如：

- `短线波段`
- `长期持有`
- `激进成长`
- `防守现金流`

然后每个模拟盘用不同 prompt、不同仓位规则、不同止损纪律。

### 4. 真实持仓和富途模拟账户的关系

当前“本地模拟盘”和“富途模拟账户”是分离的：

- 本地模拟盘用于 AI 分析和复盘。
- 富途模拟账户用于真实 OpenD paper order。

这是一种更安全的设计，因为用户可能想录入真实持仓，但不希望系统直接改富途模拟账户。

### 5. AI 周期费用

当前自动周期是 30 分钟一次。  
如果有多个模拟盘，每个周期会对每个模拟盘调用 Gemini，成本按模拟盘数量线性增加。

未来可以加：

- 只在交易时段运行。
- 只在新闻命中/价格波动达到阈值时运行。
- 每个模拟盘独立开关。
- 每个模拟盘独立周期。

### 6. 用户当前交易需求

用户当前偏向：

- 1-3 个月短期波段。
- 当前三只中概互联网/电商仓位是全部仓位。
- 关心是否应该继续持有、减仓、止损、补仓。
- 对价格不准非常敏感，希望 AI 只基于可靠行情说话。

## 新对话恢复建议

如果要继续开发，先做：

```bash
cd /Users/liurunsheng/Documents/futu-paper-ai
git status --short
git log --oneline -5
```

如果要查看服务器状态：

```bash
ssh -o ConnectTimeout=10 -i /Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem ec2-user@54.161.53.16 systemctl is-active futu-opend futu-paper-ai-web futu-paper-ai-loop autonews
```

如果要验证页面：

```bash
curl -s http://127.0.0.1:8787/api/portfolios
```

如果在服务器上验证：

```bash
ssh -o ConnectTimeout=10 -i /Users/liurunsheng/Documents/futu-paper-ai/secrets/futu.pem ec2-user@54.161.53.16 curl -s http://127.0.0.1:8787/api/portfolios
```

## 不要忘记

- 不要泄露或写入任何密钥。
- 不要把 `data/state/portfolios.json` 提交到 Git。
- 不要把 OpenD 暴露到公网。
- 涉及自动下单前必须再次确认安全边界。
- 用户现在更需要“交易学习 + 复盘 + 纪律”，不是黑盒自动交易。
