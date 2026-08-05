# A 股数据 MCP 服务（CnStock）

CnStock 是一个面向大模型和 MCP 客户端的 A 股数据服务。
提供股票、指数和场内 ETF 的行情、财务、资金流、技术指标、K 线与全市场涨跌分布数据。

项目基于 [elsejj/mcp-cn-a-stock](https://github.com/elsejj/mcp-cn-a-stock) 改造，使用 [AkShare](https://github.com/akfamily/akshare) 和 [efinance](https://github.com/nelsonie/efinance) 作为公开数据源，不依赖原项目的私有 API。

## 项目亮点

- 覆盖沪深京股票、主要指数和场内 ETF。
- 支持 Markdown 报告和适合程序消费的严格 JSON 输出。
- `brief`、`medium`、`full`、`tech` 单次最多并行查询 4 个标的。
- 支持指定历史截止日期，非交易日自动使用最近可用行情。
- 自动纠正错误市场前缀，例如将 `SH000333` 规范为 `SZ000333`。
- 内置 KDJ、MACD、RSI、布林带等技术指标。
- 使用有界并发控制同步数据请求，适合 Ubuntu 2 核 4G 等小型服务器。
- 全市场涨跌分布支持数据源回退、短时缓存和同请求合并。

## MCP 工具

| 工具 | 返回格式 | 用途 |
| --- | --- | --- |
| `brief` | JSON 外壳 + Markdown 报告 | 基本信息、行情和资金流 |
| `medium` | JSON 外壳 + Markdown 报告 | 在 `brief` 基础上增加财务摘要 |
| `full` | JSON 外壳 + Markdown 报告 | 完整财务、历史资金流和技术分析 |
| `tech` | 严格 JSON | OHLCV、KDJ、MACD、RSI、布林带 |
| `kline_daily` | Markdown | 指定交易日的 K 线 |
| `kline_range` | Markdown 表格 | 指定日期区间的 K 线 |
| `market_breadth` | 严格 JSON | 全市场涨跌家数、涨跌停和十档分布 |

完整报告示例：[兆易创新 SH603986](docs/SH603986-full.md)。

## 环境要求

- Python 3.12 或更高版本。
- Linux、macOS；生产部署推荐 Ubuntu。
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖。
- 可访问 AkShare、efinance 使用的公开行情接口。
- AkShare Proxy Patch 账号可选，但推荐用于提高东财接口稳定性。
- `market_breadth` 首选数据源需要 Chromium；不可用时会回退到 efinance。

## 快速安装

### 1. 获取代码

```bash
git clone https://github.com/lllyin/mcp-cn-a-stock.git
cd mcp-cn-a-stock
```

### 2. 创建环境并安装依赖

使用 uv：

```bash
uv sync
source .venv/bin/activate
```

或使用标准 venv 和 pip：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

### 3. 安装可选浏览器依赖

需要同花顺全市场涨跌分布时安装 Chromium：

```bash
playwright install chromium
```

Ubuntu 可使用 Playwright 安装浏览器所需的系统依赖：

```bash
playwright install --with-deps chromium
```

无桌面的 Ubuntu 可额外安装 `xvfb`。`start.sh` 会在没有 `DISPLAY` 时自动启动并管理
一个项目专用的 Xvfb；未安装也不影响其他工具，`market_breadth` 会尝试备用数据源。

## 配置

在项目根目录创建 `.env`：

```env
# AkShare Proxy Patch，可选但推荐
AKSHARE_PROXY_GATEWAY=你的代理网关
AKSHARE_PROXY_TOKEN=你的访问令牌
AKSHARE_PROXY_RETRY=30

# 同步行情 I/O 并发，以下是默认值
CN_STOCK_DATA_FETCH_MAX_WORKERS=8
CN_STOCK_DATA_FETCH_MAX_IN_FLIGHT=16
CN_STOCK_BATCH_QUERY_CONCURRENCY=2
CN_STOCK_FINANCE_CACHE_TTL_SECONDS=21600
CN_STOCK_FINANCE_CACHE_MAX_ENTRIES=512
```

兼容旧变量名 `AKSHARE_PROXY_IP`、`AKSHARE_PROXY_PASSWORD` 和
`AKSHARE_PROXY_PORT`。其中 `PORT` 历史上表示重试次数，不是网络端口；新部署建议使用
含义明确的 `GATEWAY`、`TOKEN`、`RETRY`。

Ubuntu 2 核 4G 建议先保持默认的 `8/16`。提高数值会增加上游压力，并不保证降低延迟。
交易时段的 `brief/medium/full` 都以 Playwright 为实时资金流来源；仅同时进行中的
同标的 Playwright 请求会合并，完成后的新请求仍会重新获取实时数据。
成功且非空的财务摘要默认缓存 6 小时；缓存命中不会提交线程池任务。
`brief/medium/full` 共用最多 2 个活跃批次的准入限制。财务缓存每次访问清理过期项，
超过 512 个标的时淘汰最早缓存，避免进程长期运行时无限增长。
参数含义和调优方法见[技术实现说明](docs/technical-details.md)。

## 启动和停止

推荐通过脚本后台运行：

```bash
./start.sh
```

默认 MCP 地址：

```text
http://localhost:8686/cnstock/mcp
```

查看日志：

```bash
tail -f logs/cn-stock-mcp.log
```

日志启动时会输出当前版本，例如：

```text
cn-stock-mcp version=1.1.0
```

停止服务：

```bash
./stop.sh
```

也可以前台运行并选择 transport：

```bash
cn-stock-mcp --transport http --port 8686
cn-stock-mcp --transport stdio
cn-stock-mcp --transport sse --port 8686
```

## 使用 mcporter 调用

以下示例假设 `mcporter` 已配置名为 `cn-stock` 的服务：

```bash
export MCPORTER_CONFIG=~/.openclaw/workspace/config/mcporter.json
```

查询简要、财务和完整报告：

```bash
mcporter call cn-stock brief symbol=SH600000
mcporter call cn-stock medium symbol=SZ000333
mcporter call cn-stock full symbol=SH603986 fund_flow_limit=30
```

单次批量查询，标的之间使用半角逗号：

```bash
mcporter call cn-stock brief symbol=SH600000,SZ000333,SZ300750,SH688981
```

超过 4 个标的时只处理前 4 个，其余代码会写入响应的 `warnings`。

查询机器可读技术指标：

```bash
mcporter call cn-stock tech symbol=SZ002463 days=30
mcporter call cn-stock tech symbol=SZ002463,SH688981 days=10
mcporter call cn-stock tech symbol=SZ002463 fields=macd,kdj include_derived=true
```

查询指定历史截止日期：

```bash
mcporter call cn-stock brief symbol=SZ002463 date=2026-06-05
mcporter call cn-stock tech symbol=SZ002463 days=30 date=2026-06-05
```

查询单日或区间 K 线：

```bash
mcporter call cn-stock kline_daily symbol=SH603986 date=2026-05-29 adjust=qfq
mcporter call cn-stock kline_range symbol=SH603986 start_date=2026-05-22 end_date=2026-05-29
```

`adjust` 可选 `qfq`（前复权）、`hfq`（后复权）和 `none`（不复权）。

查询全市场涨跌分布：

```bash
mcporter call cn-stock market_breadth
```

## 返回结构

`brief`、`medium`、`full` 的顶层响应包含：

- `reports`：以规范化证券代码为键的 Markdown 报告。
- `errors`：以证券代码为键的错误信息。
- `warnings`：批量截断、数据回退等非致命提醒。
- `symbols_count`：应用批量上限后的标的数量。
- `timestamp`：报告生成时间。

`tech` 使用相同的批量外壳，但 `reports` 的值是结构化对象，包含 `symbol`、`name`、
`quote_date` 和按日期排列的 `indicators`。不可计算或缺失的指标使用 JSON `null`。

`market_breadth` 返回 `source`、抓取时间、涨跌和平盘家数、涨跌停家数、十档涨跌幅分布
及回退警告。调用方应读取 `source` 和 `warnings`，不要假设每次都来自同一提供方。

## MCP 客户端接入

支持 Streamable HTTP 的客户端填写：

```text
名称: cn-stock
类型: streamableHttp
地址: http://localhost:8686/cnstock/mcp
```

CherryStudio 中进入“设置 → MCP 设置 → 添加服务器”，选择
“可流式传输的 HTTP（streamableHttp）”并填写上述地址。

![CherryStudio MCP 配置](docs/cherrystudio.jpg)

其他客户端的操作示例见[让 DeepSeek 通过 MCP 分析股票](docs/let-your-deepseek-analyze-stock-by-mcp.md)。

## 调试与测试

使用 MCP Inspector：

```bash
npx @modelcontextprotocol/inspector --url http://localhost:8686/cnstock/mcp
```

运行单元测试：

```bash
uv sync --extra dev
pytest tests --ignore=tests/test_akshare_source.py
```

查看版本：

```bash
python -c "from qtf_mcp import __version__; print(__version__)"
```

## 常见问题

**首次调用较慢**

首次请求可能包含模块初始化、浏览器启动、认证刷新或上游连接建立。请结合
`logs/cn-stock-mcp.log` 中的分段耗时判断，不要只比较单次冷启动。

**指定日期没有数据**

周末和节假日通常返回截止日期之前最近一个交易日的数据；代码错误或标的尚未上市时可能返回空结果。

**`market_breadth` 出现 fallback warning**

首选数据源认证失败、处于冷却期或浏览器不可用时会自动回退。响应仍可使用，但应关注
`source`、`trade_date` 和 `warnings`。

**批量请求被截断**

每次 tool 调用最多处理 4 个标的。需要更多标的时由客户端拆分请求，并控制并发，避免集中冲击上游接口。

## 更多文档

- [技术实现说明](docs/technical-details.md)
- [完整报告示例](docs/SH603986-full.md)
- [DeepChat 使用示例](docs/let-your-deepseek-analyze-stock-by-mcp.md)

## 免责声明

本项目使用第三方公开数据接口，无法保证数据始终实时、完整或准确。项目输出不构成投资建议，
请勿将其作为交易决策的唯一依据。股市有风险，入市需谨慎。

## 许可证

基于原项目协议，本项目采用 MIT 许可证。问题和建议请通过 GitHub Issue 提交。
