# Futu Paper AI 项目上下文

最后更新：2026-06-11  
本地目录：`/Users/liurunsheng/Documents/futu-paper-ai`  
GitHub：`laurunshen/futu_ai.git`  
服务器公网 IP：`54.161.53.16`  
服务器项目目录：`/home/ec2-user/futu_ai`

> 这个文件用于切换对话时快速恢复上下文。不要在这里写 Gemini API Key、GitHub Token、Basic Auth 密码、Futu 密码或验证码。
>
> 交易策略、AB Test 设计和复盘原则单独记录在 `STRATEGY.md`。

## 项目目标

这是一个 **Futu OpenD + Gemini + autoNews 的本地组合/模拟盘决策复盘系统**。

核心目标不是直接让 AI 替用户赚钱，而是：

- 用本地组合记录真实仓位镜像、模拟实验盘和 AB Test 分支。
- 让 Gemini 每次买入/卖出/观望都必须给理由。
- 接入 autoNews 新闻源，让 AI 根据行情、持仓和新闻做推理。
- 支持用户手动维护多个“本地组合”，即使富途模拟账户本身是空仓，也能让 AI 围绕真实持仓成本和仓位讨论。
- 后续可以扩展成多策略、多组合、多新闻源的自动复盘和模拟决策系统。

## 当前阶段结论

2026-06-11 当前结论：

- **阶段 1：自研 TradingAgents-lite MVP 已完成。**
- 当前系统已经具备“多模拟盘上下文 -> 行情/新闻/持仓输入 -> Gemini 多角色研究简报 -> AI 决策日志 -> observe/manual/auto 应用 -> 本地流水/富途成交回写”的第一版闭环。
- 这个阶段不是照搬 `tauricresearch/tradingagents`，而是在现有 Futu OpenD、autoNews、本地组合账本和 Web 工作区上自研轻量版研究小组决策结构。
- 下一阶段应进入 **阶段 2：复盘与策略评估层**，重点验证 AI 决策有没有用，而不是继续堆 agent 角色。

阶段 2 建议优先做：

1. 为每条 AI 决策记录决策时的行情快照、新闻信号和组合净值基线。
2. 增加决策后 1 天 / 3 天 / 7 天表现追踪。
3. 增加每个模拟盘的收益曲线、最大回撤、胜率、盈亏比、交易频率。
4. 对比 `l r s` 手动/实际仓位镜像、`l r s - Auto` 小资金自动盘、`富途大资金操作盘` 的差异。
5. 统计富途同步盘的成交差异、滑点和失败原因。

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
- Gemini 可以给交易建议；`auto` 模式默认只自动应用到本地组合账本。只有某个本地组合显式开启“同步富途模拟盘”时，应用订单才会提交到富途模拟环境，并按富途实际成交反写本地。
- 组合可以标记为 `模拟实验盘` 或 `实际仓位镜像`。实际仓位镜像代表用户真实券商仓位的本地镜像，AI prompt 必须避免“教学模拟盘/模拟盘稳健原则”这类误导措辞。
- 当前用户没有 A 股权限，因此 A 股暂时只保留代码能力，不作为主流程。

## 已实现功能

### Web 工作区

页面 tab：

- `模拟盘`：本地多组合管理。
- `AI 决策`：查看自动周期和手动扫描的决策记录。
- `行情`：临时行情和自选行情。
- `新闻`：查看 autoNews 结构化新闻信号。
- `AI 对话`：和 Gemini 围绕某个股票/行业/模拟盘对话。
- `设置`：富途账户状态、手动扫描、风控设置、Gemini 用量和系统输出。底层手动订单表单已隐藏，日常买卖统一从本地组合页发生。

### 本地多组合/模拟盘

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

本地组合支持：

- 新增模拟盘。
- 删除模拟盘。
- 设置当前模拟盘。
- 标记组合口径：`模拟实验盘` 或 `实际仓位镜像`。
- 修改当前模拟盘现金，支持按 `HKD` / `USD` / `CNY` 分币种维护现金桶；买入其他币种资产时可从基础币种现金自动换汇扣款。
- 添加/编辑/删除持仓。
- 组合页买入/卖出是日常交易入口：未开启富途同步时只写本地账本；开启富途同步时提交富途模拟单，并按实际成交反写本地。
- 设置 AI 应用模式：`observe` 仅观察、`manual` 手动应用、`auto` 自动应用到本地模拟盘。
- 克隆当前模拟盘为 Manual/Auto 分支，用于 AB Test。
- AI 决策生成本地订单后，可以手动应用到模拟盘；自动盘会在本地账本里自动应用。
- 本地应用会写入 `trades` 流水，并用 `decision_id` 防止同一条决策重复应用。
- `operations` 操作流水会记录创建组合、组合设置变化、现金修改、持仓快照编辑、本人交易记录、AI 手动应用、AI 自动应用和富途成交回写，便于后续复盘。
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
- 决策记录里包含 `portfolio` 字段，前端 AI 决策页会显示属于哪个模拟盘，并支持按模拟盘筛选历史记录。
- 决策上下文会带上 `portfolio_kind` 和最近 `operations`；如果组合是 `实际仓位镜像`，Gemini 会按真实仓位复盘/风控口径表达，而不是模拟盘教学口径。
- 当前模式是 `portfolio_decision` / `portfolio_suggestion`。
- `observe` 不改本地持仓；`manual` 等用户在前端确认；`auto` 会自动应用。
- 默认只改本地账本；开启富途同步的模拟盘会先提交富途模拟单，再按富途成交反写本地。

### TradingAgents-lite 决策增强

2026-06-11 已在现有 Gemini 决策引擎里加入第一版 TradingAgents-lite。  
当前可视为 **阶段 1 MVP 完成**。

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

阶段 1 完成范围：

- 单次 Gemini structured output 调用里模拟轻量研究小组，而不是引入完整多 agent 编排框架。
- AI 决策上下文包含本地组合、持仓、现金、多币种现金桶、FX 来源、最近操作、autoNews 信号和美股扩展时段。
- 前端 AI 决策页能展示研究小组、五档评级、组合动作，并支持按模拟盘筛选。
- `observe` / `manual` / `auto` 应用模式已经接入本地组合。
- 开启富途同步的组合会先提交富途模拟单，再按实际成交反写本地账本。
- 实际仓位镜像口径已经写进 prompt，避免 Gemini 把真实仓位说成教学模拟盘。

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

## 当前体验问题和阶段 2

### 1. AI 价格准确性

已经收紧规则：当前价只能来自 OpenD。  
后续可以在前端 AI 回复旁边显示“本次使用行情快照”，让用户肉眼确认 Gemini 用的是哪组价格。

### 2. 本地模拟盘 AB Test

2026-06-11 已加入本地 AB Test 闭环：

- 模拟盘支持 `observe` / `manual` / `auto` 三种 AI 应用模式。
- AI 决策日志新增 `decision_id` 和 `application` 状态。
- `manual` 模式：AI 只生成建议，前端 AI 决策详情页可以点击“应用到模拟盘”。
- `auto` 模式：AI 生成可执行本地订单且未被风控阻止时，自动应用到本地模拟盘。
- `observe` 模式：只记录建议，不应用。
- 每个模拟盘可以单独开启 `futu_sync_enabled`。关闭时，本地应用只修改 `data/state/portfolios.json`；开启时，应用订单先提交富途模拟单，随后使用富途返回的 `dealt_qty` / `dealt_avg_price` 反写本地持仓、现金和流水。
- 富途 OpenAPI 当前覆盖资金查询、持仓查询、下单、改/撤单、订单和成交查询；没有找到直接修改模拟盘现金或持仓成本的开放接口。因此初始现金/成本仍需本地维护或在富途端手动对齐，系统只能同步后续由本系统发出的交易成交。
- 当前买卖维护多币种现金桶 `cash_by_currency`；买入时优先扣交易币种现金，不足部分可按 `fx_to_hkd` 从基础币种现金自动换汇扣款。
- 2026-06-11 15:07 已加入 FX 来源追踪：系统优先尝试富途 OpenD FX 快照；服务器实测 `FX.USDHKD` / `FX.USDCNH` 返回“不支持的行情市场”，底层 `SecurityType_Forex` 静态列表为空，因此当前会回退到本地默认 HKD 汇率表，并在前端、AI 决策上下文和成交流水里标记 `local_default_fx_to_hkd`。
- 2026-06-11 15:20 已接入美股扩展时段字段：富途快照里的 `pre_*` / `after_*` / `overnight_*` 会整理成 `extended_session`，进入 AI 候选池、持仓上下文、AI 对话上下文和前端展示；它只作为盘前/盘后/夜盘情绪信号，不替代常规价或下单价。
- 2026-06-11 已创建 `富途大资金操作盘`：本地现金 HKD 1,000,000 + USD 1,000,000，空仓，`manual`，`futu_sync_enabled=true`，用于和小资金实际仓位镜像/小资金 Auto 盘比较大资金购买力下的策略差异。当前服务器 active 组合是这个富途操作盘。

后续仍需增加：

- 每个模拟盘收益曲线。
- 决策后 1/3/7 天表现追踪。
- 富途同步盘的成交差异、滑点和失败原因统计。

这些就是阶段 2 的主线，优先级高于继续扩充 TradingAgents-lite 角色。

### 2.5 阶段 2：复盘与策略评估层

阶段 2 的目标是回答：AI 决策到底有没有帮用户变得更稳、更赚钱、更少犯错。

建议实现顺序：

1. 先在决策日志里固化复盘基线：决策时间、模拟盘 ID、组合净值、现金、持仓市值、行情快照、新闻信号、AI action/rating/position_action、application 状态。
2. 新增后台或 API 计算每条决策后的 1 天 / 3 天 / 7 天收益表现，先允许用当前可得快照近似，后续再补完整历史 K 线。
3. 新增模拟盘绩效 API：总资产、收益率曲线、最大回撤、胜率、盈亏比、交易次数、换手率。
4. 前端 `模拟盘` 或 `AI 决策` 增加复盘视图，能按模拟盘和时间范围查看表现。
5. 把 AI 自动应用、AI 手动应用、本人交易、富途成交回写分开统计，避免归因混乱。
6. 最后再做策略标签和不同 prompt 纪律，例如短线波段、长期持有、激进成长、防守现金流。

### 3. 多模拟盘策略

后续可以让每个模拟盘带策略标签，例如：

- `短线波段`
- `长期持有`
- `激进成长`
- `防守现金流`

然后每个模拟盘用不同 prompt、不同仓位规则、不同止损纪律。

### 4. 真实持仓和富途模拟账户的关系

当前“本地模拟盘”和“富途模拟账户”默认分离，但可以对单个本地模拟盘开启富途同步：

- 本地模拟盘用于 AI 分析和复盘。
- 富途模拟账户用于真实 OpenD paper order。
- 同步开启后，本地应用动作会先提交富途模拟单，实际成交数量和均价再反写本地。
- 资金和持仓成本不能通过 Futu OpenAPI 任意改写，因此同步盘启用前需要尽量让本地初始状态和富途模拟账户对齐。

这仍是一种更安全的设计，因为用户可能想录入真实持仓，但不希望所有本地策略都直接改同一个富途模拟账户。

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
- 新对话如果继续开发，先读本文件和 `STRATEGY.md`，然后从“阶段 2：复盘与策略评估层”开始。
