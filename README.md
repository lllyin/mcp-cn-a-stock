# A 股数据 MCP 服务（CnStock）

CnStock 是一个面向大模型和 MCP 客户端的 A 股数据服务。
提供股票、指数和场内 ETF 的行情、财务、资金流、技术指标、K 线与全市场涨跌分布数据。

## 项目亮点

- 覆盖沪深京股票、主要指数和场内 ETF。
- 支持 Markdown 报告和适合程序消费的严格 JSON 输出。
- `brief`、`medium`、`full`、`tech` 单次最多并行查询 4 个标的。
- 支持指定历史截止日期，非交易日自动使用最近可用行情。
- 自动纠正错误市场前缀，例如将 `SH000333` 规范为 `SZ000333`。
- 内置 KDJ、MACD、RSI、布林带等技术指标。
- 开箱即用，不需要付费网关；上游接口不可用时逐级回退到备用数据源。
- 使用有界并发控制同步数据请求，适合 Ubuntu 2 核 4G 等小型服务器。

## MCP 工具

| 工具 | 用途 | 返回格式 |
| --- | --- | --- |
| `brief` | 基本信息、行情和资金流 | JSON 外壳 + Markdown 报告 |
| `medium` | 在 `brief` 基础上增加财务摘要 | JSON 外壳 + Markdown 报告 |
| `full` | 完整财务、历史资金流和技术分析 | JSON 外壳 + Markdown 报告 |
| `tech` | OHLCV、KDJ、MACD、RSI、布林带 | 严格 JSON |
| `kline_daily` | 指定交易日的 K 线 | Markdown |
| `kline_range` | 指定日期区间的 K 线 | Markdown 表格 |
| `sector_fund_flow` | 行业/概念/地域板块的资金流排行，与东财官网同口径 | Markdown 表格 |
| `market_breadth` | 全市场涨跌家数、涨跌停和十档分布 | 严格 JSON |
| `market_events` | 指定日期的龙虎榜、涨停池、公告和业绩预告 | 严格 JSON |

完整报告示例：[兆易创新 SH603986](docs/SH603986-full.md)。
各工具的返回字段见[技术实现说明](docs/technical-details.md#9-输出与错误契约)。

## 环境要求

- Python 3.12 或更高版本。
- Linux、macOS；生产部署推荐 Ubuntu。
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖。
- 可访问 AkShare、efinance 使用的公开行情接口。
- Chromium：盘中实时资金流和 `market_breadth` 的首选数据源需要，`start.sh` 会在缺失时自动安装。

## 快速安装

```bash
git clone https://github.com/lllyin/mcp-cn-a-stock.git
cd mcp-cn-a-stock
./install.sh
```

`install.sh` 装 Python 依赖和 Chromium，只需执行一次。有 `uv` 就用 `uv`，没有就用
标准 venv + pip。

无桌面的 Ubuntu 可额外安装 `xvfb`，`start.sh` 会在没有 `DISPLAY` 时自动启动并管理它；
未安装也不影响其他工具。

## 启动和停止

零配置即可启动，不需要账号、密钥或网关：

```bash
./start.sh
```

默认 MCP 地址：

```text
http://127.0.0.1:8686/cnstock/mcp
```

写 `127.0.0.1` 而不是 `localhost`：服务只监听 IPv4 回环，而 macOS 上 `localhost`
会先解析到 IPv6 的 `::1`，有些客户端在那里被拒之后不会回退到 IPv4，表现是连接
一直挂着不报错。

查看日志（启动时会打印当前版本）：

```bash
tail -f logs/cn-stock-mcp.log
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

## MCP 客户端接入

支持 Streamable HTTP 的客户端填写：

```text
名称: cn-stock
类型: streamableHttp
地址: http://127.0.0.1:8686/cnstock/mcp
```

CherryStudio 中进入“设置 → MCP 设置 → 添加服务器”，选择
“可流式传输的 HTTP（streamableHttp）”并填写上述地址。

![CherryStudio MCP 配置](docs/cherrystudio.jpg)

其他客户端的操作示例见[让 DeepSeek 通过 MCP 分析股票](docs/let-your-deepseek-analyze-stock-by-mcp.md)。

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

查询单日或区间 K 线，`adjust` 可选 `qfq`（前复权）、`hfq`（后复权）和 `none`（不复权）：

```bash
mcporter call cn-stock kline_daily symbol=SH603986 date=2026-05-29 adjust=qfq
mcporter call cn-stock kline_range symbol=SH603986 start_date=2026-05-22 end_date=2026-05-29
```

查询全市场涨跌分布：

```bash
mcporter call cn-stock market_breadth
```

查询指定日期的公开事件池：

```bash
mcporter call cn-stock market_events \
  date=2026-08-20 \
  sources=lhb,limit_up,announcements \
  announcement_lookback_days=3 \
  keywords=中标,订单,涨价,投产,收购,重组 \
  symbols=SH600000,SZ000001 \
  max_rows_per_source=200
```

`sources` 可组合 `lhb`、`limit_up`、`strong`、`previous_limit_up`、`broken_board`、
`announcements` 和 `earnings_forecast`。`symbols` 可选，使用标准 `SH/SZ/BJ + 6 位代码`，
在 `max_rows_per_source` 截断前过滤；省略时保持全市场行为。

## 常见问题

**首次调用较慢**

首次请求可能包含模块初始化、浏览器启动、认证刷新或上游连接建立。请结合
`logs/cn-stock-mcp.log` 中的分段耗时判断，不要只比较单次冷启动。

**指定日期没有数据**

周末和节假日通常返回截止日期之前最近一个交易日的数据；代码错误或标的尚未上市时可能返回空结果。

**报告里出现“盘中实时数据暂时不可用”**

东财资金流接口和页面兜底都没取到数据，其余部分不受影响。这是瞬时状态，不会被写进缓存。
科创 50（`SH000688`）等没有资金流向页面的指数在盘中本来就没有这一段。

**`market_breadth` 出现 fallback warning**

首选数据源认证失败、处于冷却期或浏览器不可用时会自动回退。响应仍可使用，但应关注
`source`、`trade_date` 和 `warnings`。

**批量请求被截断**

每次 tool 调用最多处理 4 个标的。需要更多标的时由客户端拆分请求，并控制并发，避免集中冲击上游接口。

## 配置

所有配置都通过 `.env` 提供，全部可省略，省略即使用下表的默认值；改完需要重启服务。
可以从 `.env.example` 复制一份来改。

> `.env` 的取值优先于 shell 环境变量。`HTTP_CHANNEL=direct ./start.sh` 会被 `.env` 里的
> 同名项覆盖，临时改配置请直接改 `.env`。

要和别的程序共存时设 `ENV_PREFIX`，之后所有配置名都带上这个前缀（`ENV_PREFIX=CNSTOCK_`
时写 `CNSTOCK_HTTP_CHANNEL`）。`AKSHARE_PROXY_*` 属于第三方插件，不受影响。

### 出站 HTTP 通道

部分东方财富接口会直接断开普通 HTTP 客户端的连接，`HTTP_CHANNEL` 决定用哪种方式访问这些
主机。默认的 `auto` 在没有配置网关时使用 `impersonate`，无需任何额外账号。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `HTTP_CHANNEL` | 访问东财行情主机的方式：<br>`auto` 网关可用时走 `proxy`，否则降级 `impersonate`<br>`proxy` 经授权网关和代理出口，按积分计费<br>`impersonate` 本机直连 + 浏览器 TLS 指纹<br>`direct` 本机直连 + 原生 `requests`，可写作 `off` | `auto`<br>`proxy`<br>`impersonate`<br>`direct`<br>（默认 `auto`） |
| `IMPERSONATE_RETRY` | 单个请求的伪装尝试次数，用尽后改用原生 `requests` 重放一次 | 正整数（默认 `3`） |
| `IMPERSONATE_TIMEOUT_SECONDS` | 单次伪装请求的超时 | 秒（默认 `8`） |
| `IMPERSONATE_BROWSER` | 伪装的浏览器指纹 | curl_cffi 浏览器名（默认 `chrome`） |
| `IMPERSONATE_SUSPEND_AFTER_FAILURES` | 连续多少次请求打满重试仍失败后暂停伪装通道 | 正整数（默认 `4`） |
| `IMPERSONATE_SUSPEND_SECONDS` | 暂停时长。期间东财源直接跳过，改用备用源 | 秒（默认 `300`） |

只有 4 个东方财富主机会被接管，其余主机原样直连；详见[出站 HTTP 通道](docs/technical-details.md#6-出站-http-通道)。

### AkShare Proxy Patch（可选，付费）

**默认关闭。** 这是一个按积分计费的授权网关，不配置也能正常使用全部工具；上游对本机出口 IP
限流严重时可以启用它来提高东财接口的成功率。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `AKSHARE_PROXY_ENABLED` | 是否启用网关。只作为 `HTTP_CHANNEL=auto` 的判定输入 | `0`<br>`1`<br>（默认 `0`） |
| `AKSHARE_PROXY_GATEWAY` | 授权网关地址，不含协议和端口 | 主机名或 IP（默认空） |
| `AKSHARE_PROXY_TOKEN` | 网关访问令牌 | 字符串（默认空） |
| `AKSHARE_PROXY_RETRY` | 网关请求的失败重试次数（旧名 `AKSHARE_PROXY_PORT` 仍然认） | 正整数（默认 `30`） |

从旧版本升级时注意：这个开关以前默认开启，现在需要显式写 `AKSHARE_PROXY_ENABLED=1`
才会继续走网关，否则自动降级到 `impersonate`。

### 盘中行情与资金流

盘中的当日 K 线 bar 由一层可插拔的实时行情 provider 补齐，资金流在东财接口不可用时回退到
浏览器加载的资金流向页面。两者都可以整层关闭。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `BASIC_INFO_PROVIDERS` | 基本数据（市值、市盈率、市净率）的尝试顺序，后面的源补前面缺的字段：<br>`eastmoney` 字段最全，需要网关或未被封的出口<br>`tencent` 无需鉴权，没有网关的部署靠它兜住这一组 | `eastmoney`<br>`tencent`<br>`off`<br>（默认 `eastmoney,tencent`） |
| `INTRADAY_QUOTE_PROVIDERS` | 盘中实时行情的尝试顺序，逗号分隔按序尝试，`off` 关闭整层：<br>`fund_flow_page` 复用已解析的资金流页面，不发请求但没有开高低<br>`tencent` 六项俱全 | `fund_flow_page`<br>`tencent`<br>`off`<br>（默认 `fund_flow_page,tencent`） |
| `INTRADAY_QUOTE_CROSS_CHECK_PCT` | 拿到第一个可用报价后再问剩下的源一遍，字段相差超过这个值就打 WARNING。每个标的多一次上游请求，只在怀疑某个源口径不对时开 | 百分比，`0` 关闭（默认 `0`） |
| `TRADING_CALENDAR_PROVIDERS` | 判「今天开不开市」的日历来源，判断入口统一在 `finmcp.market_calendar`：<br>`sina` 上交所公布的交易日名单，最权威<br>`holiday_cn` [NateScarlet/holiday-cn](https://github.com/NateScarlet/holiday-cn) 的国务院放假安排换算而来；对 728 天实测与 `sina` 一致 99.863%，唯一差异是 2024-02-09 除夕交易所多休<br>`weekday` 兜底，周一到周五算交易日（同一区间错 56 天）| `sina`<br>`holiday_cn`<br>`weekday`<br>`off`<br>（默认 `sina,holiday_cn,weekday`） |
| `FUND_FLOW_PROVIDERS` | 个股/指数资金流 HTTP 层的取数顺序（浏览器页面兜底另算，挂在这一层之后）：<br>`eastmoney` 给全部历史，走伪装通道<br>`eastmoney_delay` 同一接口的 push2delay 主机，只回当日一行，但主源拒绝出口 IP 时它还通；补的是没有资金流向页面的标的（科创 50 这类指数） | `eastmoney`<br>`eastmoney_delay`<br>`off`<br>（默认 `eastmoney,eastmoney_delay`） |
| `REALTIME_FUND_FLOW_PROVIDERS` | 没有资金流向页面的标的（科创50 等）盘中实时资金流的来源。`eastmoney_delay` 取 push2delay 分钟线最后一行，即当日累计五档净流入，净占比按当日成交额折算；有页面的标的不走这里 | `eastmoney_delay`<br>`off`<br>（默认 `eastmoney_delay`） |
| `SECTOR_FUND_FLOW_PROVIDERS` | 板块资金流的取数顺序：<br>`eastmoney` 字段全<br>`eastmoney_dataapi` 只有主力净额，但主源连不上时它还通；报告备注里会标出是降级源 | `eastmoney`<br>`eastmoney_dataapi`<br>`off`<br>（默认 `eastmoney,eastmoney_dataapi`） |
| `SECTOR_TAXONOMY_PROVIDERS` | 板块分级表的来源，用来只排同一层——东财的行业板块名单是一棵树摊平的（496 个），不分级会让父子板块同时上榜、同一笔钱数两遍。默认排申万二级，和东财官网那张榜逐位一致：<br>`shenwan` 申万宏源的行业分类，经 AkShare 抓乐咕乐股页面<br>`swsresearch` 申万宏源研究所官网的接口，同一套分类的第二个源，乐咕乐股对机房 IP 回人机验证页时靠它补<br>同一套标准的源会合并：前一个缺的级由后一个补<br>`off` 退回全部板块一起排，报告里会标出来 | `shenwan,swsresearch`<br>`shenwan`<br>`off`<br>（默认 `shenwan,swsresearch`） |
| `KLINE_PROVIDERS_INDEX` | **指数**用的兜底顺序，和下一项分开配：指数的成交量各源口径差得多（创业板指相差 3.52%），同花顺与东财一致 | 同上（默认 `tonghuashun,tencent,sina`） |
| `KLINE_MAX_GAP_TRADING_DAYS` | 相邻两根 K 线之间允许缺多少个**交易日**，超过就判该源失败、让链路回退。防的是「序列断裂」——列是齐的、数值也在合理区间，源「成功」返回，但涨跌幅会跨缺口计算、均线全错。单位是交易日而非自然日，所以长假在结构上就是 0，阈值只用来容忍停牌（10 是 2018 年后重大资产重组停牌的上限）。是偏好不是硬条件：每个源都带同样缺口时（真实长期停牌）会宽松再问一轮并放行，不会让 K 线整段缺失 | 交易日，`0` 关闭（默认 `10`） |
| `KLINE_PROVIDERS` | 东财那一级取不到时，**个股/ETF** 的兜底顺序，逗号分隔按序尝试，`off` 关闭整层：<br>`tonghuashun` 不覆盖北交所<br>`tencent` 个股/ETF/指数都覆盖，北交所大半不认<br>`sina` 覆盖腾讯不认的北交所代码，但不认 ETF 和创业板指<br>三家各补各的洞 | `tonghuashun`<br>`tencent`<br>`sina`<br>`off`<br>（默认 `tencent,sina`） |
| `FUND_FLOW_PAGE_ENABLED` | 东财资金流接口不可用时，是否回退到资金流向页面 | `0`<br>`1`<br>（默认 `1`） |
| `FUND_FLOW_PAGE_CONCURRENCY` | 同时进行的兜底页面加载数。要和 `BROWSER_MAX_PAGES` 一起调，只提一个另一个就成了新瓶颈 | 正整数（默认 `3`） |
| `FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS` | 单个标的等一个名额的上限，等不到就跳过兜底。必须大于一次页面加载的耗时（实测 p90 7.5s） | 秒（默认 `8`） |
| `FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS` | 一次请求里所有标的等名额的总时长上限，必须大于上一项 | 秒，`0` 关闭（默认 `15`） |
| `FUND_FLOW_PAGE_TABLE_WAIT_SECONDS` | 等历史表渲染完成的上限 | 秒（默认 `15`） |
| `FUND_FLOW_PAGE_REUSE_SECONDS` | 同一标的页面解析结果的复用窗口，避免一次请求内重复加载同一页面 | 秒，`0` 关闭复用（默认 `30`） |
| `FUND_FLOW_PAGE_MAX_LOADS` | 单次请求允许的页面加载次数，只在没拿到数据时才会用掉。被拒直接换 tab，不 reload；别调大，被拒后每多开一个 tab 都消耗同一出口的频率额度，会把偶发的拒绝放大成整批滑块，合适的值用 `scripts/probe_tuning.py` 量 | 正整数（默认 `2`） |
| `FUND_FLOW_PAGE_RETRY_DELAY_MS` | 重试刷新之前的随机等待区间，只作用在重试路径上 | `下界,上界` 毫秒<br>单个数字为固定值<br>`0` 关闭<br>（默认 `250,350`） |
| `FUND_FLOW_PAGE_OPEN_AFTER_FAILURES` | 多少次徒劳加载后暂停整层兜底 | 正整数（默认 `4`） |
| `FUND_FLOW_PAGE_FAILURE_WINDOW_SECONDS` | 上一项按这个滑动窗口计数 | 秒，`0` 退回连续计数（默认 `60`） |
| `FUND_FLOW_PAGE_COOLDOWN_SECONDS` | 暂停时长 | 秒（默认 `60`） |

### 上游源熔断

某个上游源连续失败时直接跳过它，不必每次请求都把整条 provider 链走完。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `SOURCE_BREAKER_ENABLED` | 是否启用熔断 | `0`<br>`1`<br>（默认 `1`） |
| `SOURCE_BREAKER_OPEN_AFTER_FAILURES` | 连续失败多少次后跳过该源 | 正整数（默认 `3`） |
| `SOURCE_BREAKER_COOLDOWN_SECONDS` | 冷却时长，结束后放行一次探测请求 | 秒（默认 `120`） |

### 并发与线程池

AkShare 和 efinance 的接口是同步网络调用，由一个有界线程池执行。Ubuntu 2 核 4G 建议保持默认值，
调高会增加上游压力，并不保证降低延迟。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `FETCH_MAX_WORKERS` | 同时执行同步数据任务的线程数 | 正整数（默认 `8`） |
| `FETCH_MAX_IN_FLIGHT` | 已运行和已提交任务的总上限，超出后请求以协程等待 | 正整数（默认 `16`） |
| `BATCH_CONCURRENCY` | `brief/medium/full` 共享的活跃批次数上限 | 正整数（默认 `2`） |
| `FINANCE_CACHE_TTL_SECONDS` | 财务摘要的缓存时间。财务数据只在定期报告发布后变动 | 秒，`0` 关闭（默认 `21600`） |
| `FINANCE_CACHE_MAX_ENTRIES` | 财务缓存的最大标的数，超出后淘汰最早项 | 正整数（默认 `512`） |

### 市场纪元边界

交易所的时刻表是死的（09:30 开盘、15:00 收盘），但上游不在这些时刻定稿：盘前已经开始
更新当日数据，盘后还要整理一会儿。下面几项就是调这个提前量和延后量的，越界会夹回合法
区间并告警。它们同时决定报告缓存的纪元划分和盘中资金流走哪条分支。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `MARKET_EPOCH_WARMUP_TIME` | 从这一刻起按盘中对待。晚于开盘会把真在交易的一段判成闭市 | 四位 HHMM，夹在 `0700`–`0930`（默认 `0915`） |
| `MARKET_EPOCH_SETTLE_TIME` | 当日数据从这一刻起算定稿、报告可完全复用。早于收盘会把仍在变动的连续竞价折进来 | 四位 HHMM，不早于 `1500`、不晚于 `MARKET_EPOCH_FINAL_TIME`（默认 `1530`） |
| `MARKET_EPOCH_FINAL_TIME` | 资金流从抓页面切回读接口的时刻，同时是纪元边界 | 四位 HHMM，夹在 `1500`–`2300`（默认 `1600`） |
| `MARKET_EPOCH_BUFFER_MINUTES` | 午休、傍晚这些边界后留给上游整理的缓冲 | 分钟，`0`–`60`（默认 `5`） |

### 缓存

缓存唯一的正当理由是"这段时间里这份数据不会变"：非交易日数据冻结，一个纪元可以一直
命中；盘中数据在变，只用短 TTL 合并重复请求。命中与否不改变返回内容。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `CACHE_ENABLED` | 总开关。关掉后所有命名空间既不读也不写，可用于冷热对照压测 | `0`<br>`1`<br>（默认 `1`） |
| `CACHE_INTRADAY_TTL_SECONDS` | 盘中软过期秒数的默认值，`0` 表示盘中绝不复用。盘中数值持续变动，这个 TTL 只用于合并突发重复请求 | 秒（默认 `30`） |
| `CACHE_STALE_ON_ERROR` | 软过期后刷新失败，是否继续用旧值。用了一定会在输出里标注；跨纪元的旧值永远不给 | `0`<br>`1`<br>（默认 `1`） |
| `CACHE_DISK_ENABLED` | 跨重启保留闭市纪元的条目。傍晚纪元长达 16 小时，周末达 64 小时 | `0`<br>`1`<br>（默认 `1`） |
| `CACHE_DIR` | 磁盘层目录，相对项目根目录。每个命名空间一个子目录 | 路径（默认 `.runtime/cache`） |
| `CACHE_<命名空间>_MAX_ENTRIES`<br>`CACHE_<命名空间>_TTL_SECONDS` | 单个命名空间的覆盖，命名空间有 `report`、`market_events`、`sector_flow`、`market_breadth`、`finance`、`calendar`、`taxonomy`。例：`CACHE_REPORT_MAX_ENTRIES=512` | 正整数 / 秒 |

| `CACHE_<命名空间>_ENABLED` | 单独关掉某一层缓存，缺省跟随 `CACHE_ENABLED`。用于 A/B——只有总开关时无法把收益归因到某一层 | `0`/`1`（默认跟随总开关） |
| `CACHE_FUND_FLOW_MAX_ROWS` | 资金流历史一条最多缓存多少行。主源给全部历史（老标的数千行），截断可控内存；请求要的行数超过存下来的会判未命中、照常打上游，所以不会让数据变少 | 正整数（默认 `250`） |
| `CONF_DIR` | 参考数据目录（指数名单、代码表、板块表）。默认随包发布，正常不用配；指到别处可临时替换而不重装，只放要改的那个文件即可，其余仍从包内读 | 路径（默认包内 `finmcp/confs`） |

盘中命中返回的必然是一份稍旧的快照，TTL 决定这份快照能有多旧。对资金流精度要求高时设为 `0`。
纪元划分、TTL 取值依据和实测数据见[报告缓存](docs/technical-details.md#10-报告缓存)。

### 市场宽度（同花顺）

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `MARKET_BREADTH_AUTH_FILE` | 同花顺认证缓存文件 | 路径（默认 `.runtime/tonghuashun-auth.json`） |
| `MARKET_BREADTH_COOLDOWN_SECONDS` | 同花顺认证失败后的冷却时间，冷却期内 `market_breadth` 直接用 efinance | 秒（默认 `300`） |

### 浏览器与虚拟显示

资金流兜底和 `market_breadth` 共用同一个浏览器实例，下面几项对两者同时生效。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `BROWSER_MAX_PAGES` | 整个浏览器同时开着的页面数上限。这同时就是同时有几个渲染进程，是峰值内存的直接决定项：每多一个并发页约 +130 MiB | 正整数（默认 `3`） |
| `BROWSER_IDLE_TIMEOUT_SECONDS` | **盘中**多久没人调用就关掉浏览器。默认 90 分钟盖住午休，关早了下一批调用要重新等冷启动（+2.19s） | 秒，`0` 整层关闭回收（总开关，不看时段）（默认 `5400`） |
| `BROWSER_IDLE_TIMEOUT_CLOSED_SECONDS` | **盘外**（收盘后、非交易日）的空闲回收超时。盘外没有午休要盖，而浏览器进程树占 378 MiB（热空闲共 621 MiB，拆掉后 243 MiB） | 秒，`0` 盘外不回收、盘中照旧（默认 `300`） |
| `BROWSER_DISGUISE` | 把无头浏览器的自报特征改成普通浏览器的样子。默认关：机房出口的机器上实测原样身份全通、伪装后被拒更多，开发机上可能相反；这台机器该用哪种用 `scripts/probe_tuning.py` 量，置 1 启用伪装 | `0`<br>`1`<br>（默认 `0`） |
| `BROWSER_CLAIM_PLATFORM` | 对外声明哪个平台。`auto` 下 Windows/macOS 照实报，Linux 报 macOS | `auto`<br>`real`<br>`macos`<br>`windows`<br>（默认 `auto`） |
| `BROWSER_NO_SANDBOX` | 为 Chromium 添加 `--no-sandbox`。会降低隔离，仅在 sandbox 确实不可用时启用 | `0`<br>`1`<br>（默认 `0`） |
| `XVFB_DISPLAY_NUMBER` | 无 `DISPLAY` 时 `start.sh` 使用的 Xvfb 起始显示号，被占用则依次往后试到 109 | 整数（默认 `99`） |
| `XVFB_SCREEN` | Xvfb 屏幕配置 | `宽x高x色深`（默认 `1920x1080x24`） |
| `BROWSER_HEADFUL` | 调试开关：用有头浏览器加载，便于人工观察 | `0`<br>`1`<br>（默认 `0`） |
| `BROWSER_KEEP_PAGES` | 调试开关：抓完不关页面。每个页面是一个独立渲染进程，会显著抬高内存 | `0`<br>`1`<br>（默认 `0`） |

## 更多文档

- [2.0.0 变更说明与上线清单](docs/release-notes-2.0.0.md)：输出文本和配置名改了什么、下游怎么改、服务器上怎么验
- [开发与维护](docs/development.md)：跑测试、调试、发布前验证、重构时怎么证明行为没变
- [项目架构](docs/architecture.md)：分层、每层负责什么、加工具/加数据源该动哪里
- [技术实现说明](docs/technical-details.md)：数据链路与回退、输出契约、报告缓存、调优边界
- [完整报告示例](docs/SH603986-full.md)
- [DeepChat 使用示例](docs/let-your-deepseek-analyze-stock-by-mcp.md)

## 免责声明

本项目使用第三方公开数据接口，无法保证数据始终实时、完整或准确。项目输出不构成投资建议，
请勿将其作为交易决策的唯一依据。股市有风险，入市需谨慎。

## 许可证

基于原项目协议，本项目采用 MIT 许可证。问题和建议请通过 GitHub Issue 提交。
