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
- 开箱即用，不需要付费网关；上游接口不可用时逐级回退到备用数据源。
- 使用有界并发控制同步数据请求，适合 Ubuntu 2 核 4G 等小型服务器。

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
| `market_events` | 严格 JSON | 指定日期的龙虎榜、涨停池、公告和业绩预告 |

完整报告示例：[兆易创新 SH603986](docs/SH603986-full.md)。
各工具的返回字段见[技术实现说明](docs/technical-details.md#9-输出与错误契约)。

## 环境要求

- Python 3.12 或更高版本。
- Linux、macOS；生产部署推荐 Ubuntu。
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖。
- 可访问 AkShare、efinance 使用的公开行情接口。
- Chromium：盘中实时资金流和 `market_breadth` 的首选数据源需要，缺失时会回退到备用源。

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

### 3. 安装浏览器

```bash
playwright install chromium
```

Ubuntu 用下面这条命令，会一并安装浏览器所需的系统依赖：

```bash
playwright install --with-deps chromium
```

无桌面的 Ubuntu 可额外安装 `xvfb`。`start.sh` 会在没有 `DISPLAY` 时自动启动并管理一个项目专用的
Xvfb；未安装也不影响其他工具。

## 启动和停止

复制一份配置，然后用脚本后台运行：

```bash
cp .env.example .env
./start.sh
```

默认 MCP 地址：

```text
http://localhost:8686/cnstock/mcp
```

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

## 配置

所有配置都通过 `.env` 提供，全部可省略，省略即使用下表的默认值。改完需要重启服务。

> 入口执行的是 `load_dotenv(override=True)`，**`.env` 的取值优先于 shell 环境变量**。
> `HTTP_CHANNEL=direct ./start.sh` 这种写法会被 `.env` 里的同名项覆盖掉，
> 临时改配置请直接改 `.env` 或注释掉其中对应的行。

配置名一律不带前缀，`AKSHARE_PROXY_*` 那一组除外——那是第三方插件 akshare-proxy-patch 的
名字。要和别的程序共存、担心重名时设 `ENV_PREFIX`，之后所有配置都读带前缀的名字——
`ENV_PREFIX=CNSTOCK_` 时读的就是 `CNSTOCK_HTTP_CHANNEL`。`start.sh` 读的 `XVFB_*`
也遵守同一规则。

### 出站 HTTP 通道

部分东方财富接口会直接断开普通 HTTP 客户端的连接，`HTTP_CHANNEL` 决定用哪种方式访问这些
主机。默认的 `auto` 在没有配置网关时使用 `impersonate`，无需任何额外账号。

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `HTTP_CHANNEL` | `auto`<br>`proxy`<br>`impersonate`<br>`direct`<br>（默认 `auto`） | 访问东财行情主机的方式：<br>`auto` 网关可用时走 `proxy`，否则降级 `impersonate`<br>`proxy` 经授权网关和代理出口，按积分计费<br>`impersonate` 本机直连 + 浏览器 TLS 指纹<br>`direct` 本机直连 + 原生 `requests`，可写作 `off` |
| `IMPERSONATE_RETRY` | 正整数（默认 `3`） | 单个请求的伪装尝试次数，用尽后改用原生 `requests` 重放一次 |
| `IMPERSONATE_TIMEOUT_SECONDS` | 秒（默认 `8`） | 单次伪装请求的超时 |
| `IMPERSONATE_BROWSER` | curl_cffi 浏览器名（默认 `chrome`） | 伪装的浏览器指纹；固定取值才能复用 TLS 连接 |
| `IMPERSONATE_SUSPEND_AFTER_FAILURES` | 正整数（默认 `4`） | 连续多少次请求打满重试仍失败后暂停伪装通道 |
| `IMPERSONATE_SUSPEND_SECONDS` | 秒（默认 `300`） | 暂停时长。期间这四个主机退回原生 `requests`，而它们被接管的理由正是拒绝原生 `requests`，所以东财源会在这段时间直接跳过，不再逐个源重新发现一遍 |

只有 `push2`、`push2his`、`fund`、`emweb.securities` 四个东方财富主机会被接管，其余主机原样直连。
四种模式互斥，同一进程只安装一个；详见[出站 HTTP 通道](docs/technical-details.md#6-出站-http-通道)。

### AkShare Proxy Patch（可选，付费）

**默认关闭。** 这是一个按积分计费的授权网关，不配置也能正常使用全部工具；上游对本机出口 IP
限流严重时可以启用它来提高东财接口的成功率。

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `AKSHARE_PROXY_ENABLED` | `0`<br>`1`<br>（默认 `0`） | 是否启用网关。只作为 `HTTP_CHANNEL=auto` 的判定输入 |
| `AKSHARE_PROXY_GATEWAY` | 主机名或 IP（默认空） | 授权网关地址，不含协议和端口 |
| `AKSHARE_PROXY_TOKEN` | 字符串（默认空） | 网关访问令牌 |
| `AKSHARE_PROXY_RETRY` | 正整数（默认 `30`） | 网关请求的失败重试次数。插件的第三个参数是重试次数不是端口，早年误名为 `AKSHARE_PROXY_PORT`，那个名字仍然认 |

从旧版本升级时注意：这个开关以前默认开启。如果原来只配了 `GATEWAY` 和 `TOKEN`、没有写
`AKSHARE_PROXY_ENABLED`，现在需要显式写 `AKSHARE_PROXY_ENABLED=1` 才会继续走网关，否则会
自动降级到 `impersonate`，启动日志里的 `reason` 会是 `auto:proxy_disabled`。

### 盘中行情与资金流

盘中的当日 K 线 bar 由一层可插拔的实时行情 provider 补齐，资金流在东财接口不可用时回退到
浏览器加载的资金流向页面。两者都可以整层关闭。

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `BASIC_INFO_PROVIDERS` | `eastmoney`<br>`tencent`<br>`off`<br>（默认 `eastmoney,tencent`） | 基本数据（总市值、流通市值、市盈率、市净率）的尝试顺序，后面的源补前面缺的字段：<br>`eastmoney` 字段最全，需要网关或未被封的出口<br>`tencent` 走 `qt.gtimg.cn`，无需鉴权，与东财逐项比对过市值与市盈率 0.000% 一致<br>没有网关的部署靠它兜住这一组，否则会连带丢掉市盈率(静) 和换手率 |
| `INTRADAY_QUOTE_PROVIDERS` | `fund_flow_page`<br>`tencent`<br>`off`<br>（默认 `fund_flow_page,tencent`） | 盘中实时行情的尝试顺序，逗号分隔按序尝试，`off` 关闭整层：<br>`fund_flow_page` 复用已解析的资金流页面，不发请求但没有开高低<br>`tencent` 走 `qt.gtimg.cn`，六项俱全 |
| `INTRADAY_QUOTE_CROSS_CHECK_PCT` | 百分比，`0` 关闭（默认 `0`） | 拿到第一个可用报价后再问剩下的源一遍，字段相差超过这个值就打 WARNING。开着每个标的多一次上游请求，只在怀疑某个源口径不对时开——创业板指成交量差 3.5% 那件事，开着的话日志里当场就有一行 |
| `TRADING_CALENDAR_PROVIDERS` | `sina`<br>`weekday`<br>`off`<br>（默认 `sina,weekday`） | 判"今天开不开市"的日历来源：<br>`sina` 上交所公布的交易日名单（经 AkShare），8797 行 / 0.18s<br>`weekday` 兜底，周一到周五算交易日，即接入日历之前的行为<br>降级路径做成平台而不是 if/else，好处是看得见、能单独关掉 |
| `TRADING_CALENDAR_TTL_SECONDS` | 秒（默认 `86400`） | 日历的进程内缓存时长。交易日历提前一年公布，一天刷一次够了 |
| `KLINE_PROVIDERS` | `tencent`<br>`sina`<br>`off`<br>（默认 `tencent,sina`） | 东财那一级取不到时，历史 K 线的兜底顺序，逗号分隔按序尝试，`off` 关闭整层：<br>`tencent` 走 `stock_zh_a_hist_tx`，个股/ETF/指数都覆盖，北交所大半不认<br>`sina` 走 `stock_zh_a_daily`，覆盖腾讯不认的北交所代码，但 ETF 和创业板指是 JSONDecodeError<br>接新源只需写一个 provider 再注册，然后把名字加进来 |
| `FUND_FLOW_PAGE_ENABLED` | `0`<br>`1`<br>（默认 `1`） | 东财资金流接口不可用时，是否回退到资金流向页面 |
| `FUND_FLOW_PAGE_CONCURRENCY` | 正整数（默认 `3`） | 同时进行的兜底页面加载数。要和 `BROWSER_MAX_PAGES` 一起调，两者是串联的闸门，只提其中一个另一个立刻变成新瓶颈 |
| `FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS` | 秒（默认 `8`） | 单个标的等一个名额的上限，等不到就跳过兜底、改渲染“盘中实时数据暂时不可用”。必须大于一次页面加载的耗时（部署机实测 p50 3.4s / p90 7.5s），否则一批 4 个标的里的最后一个结构上永远排不到 |
| `FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS` | 秒，`0` 关闭（默认 `15`） | 一次请求里所有标的加起来最多为等名额花掉多少秒。是截止时间不是配额，必须大于上一项，否则上一项提了也会被它削回来 |
| `FUND_FLOW_PAGE_TABLE_WAIT_SECONDS` | 秒（默认 `15`） | 等历史表渲染完成的上限。请求被拒时会提前结束，不会白等满 |
| `FUND_FLOW_PAGE_REUSE_SECONDS` | 秒，`0` 关闭复用（默认 `30`） | 同一标的页面解析结果的复用窗口，避免一次请求内重复加载同一页面 |
| `FUND_FLOW_PAGE_MAX_LOADS` | 正整数（默认 `2`） | 单次请求允许的页面加载次数：第 2 次是同一个 tab 上 reload，第 3 次起才换 tab。只在没拿到想要的数据时才会用掉，顺利路径一次都不多花 |
| `FUND_FLOW_PAGE_RETRY_DELAY_MS` | `下界,上界` 毫秒<br>单个数字为固定值<br>`0` 关闭<br>（默认 `250,350`） | reload 之前的随机等待区间。只作用在重试路径上，顺利路径不受影响；没拿到数据后 0 毫秒就刷新同一个页面是个机器节奏 |
| `FUND_FLOW_PAGE_OPEN_AFTER_FAILURES` | 正整数（默认 `4`） | 多少次徒劳加载后暂停整层兜底 |
| `FUND_FLOW_PAGE_FAILURE_WINDOW_SECONDS` | 秒，`0` 退回连续计数（默认 `60`） | 上一项按这个滑动窗口计数，不是连续计数——被拒是逐次随机的，连续计数两头都不准 |
| `FUND_FLOW_PAGE_COOLDOWN_SECONDS` | 秒（默认 `60`） | 暂停时长 |

### 上游源熔断

某个上游源连续失败时直接跳过它，不必每次请求都把整条 provider 链走完。

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `SOURCE_BREAKER_ENABLED` | `0`<br>`1`<br>（默认 `1`） | 是否启用熔断 |
| `SOURCE_BREAKER_OPEN_AFTER_FAILURES` | 正整数（默认 `3`） | 连续失败多少次后跳过该源 |
| `SOURCE_BREAKER_COOLDOWN_SECONDS` | 秒（默认 `120`） | 冷却时长，结束后放行一次探测请求 |

### 并发与线程池

AkShare 和 efinance 的接口是同步网络调用，由一个有界线程池执行。Ubuntu 2 核 4G 建议保持默认值，
调高会增加上游压力，并不保证降低延迟。

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `FETCH_MAX_WORKERS` | 正整数（默认 `8`） | 同时执行同步数据任务的线程数 |
| `FETCH_MAX_IN_FLIGHT` | 正整数（默认 `16`） | 已运行和已提交任务的总上限，超出后请求以协程等待 |
| `BATCH_CONCURRENCY` | 正整数（默认 `2`） | `brief/medium/full` 共享的活跃批次数上限 |
| `FINANCE_CACHE_TTL_SECONDS` | 秒，`0` 关闭（默认 `21600`） | 成功且非空的财务摘要缓存时间。财务数据只在定期报告发布后变动 |
| `FINANCE_CACHE_MAX_ENTRIES` | 正整数（默认 `512`） | 财务缓存的最大标的数，超出后淘汰最早项 |

### 报告缓存

按标的缓存已渲染的报告。缓存条目绑定“市场纪元”，只在重新生成会得到同样字节的窗口内复用，
因此命中与否不改变返回内容。

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `REPORT_CACHE_ENABLED` | `0`<br>`1`<br>（默认 `1`） | 关闭后缓存完全不参与调用链，可用于冷热对照压测 |
| `REPORT_CACHE_INTRADAY_TTL_SECONDS` | 秒，`0` 表示盘中绝不复用（默认 `30`） | 盘中数值持续变动，这个 TTL 只用于合并突发重复请求 |
| `REPORT_CACHE_SETTLE_TIME` | 四位 HHMM，夹在 `1500`–`1700`（默认 `1530`） | 收盘后进入完全复用纪元的时间。默认留 30 分钟缓冲等东财资金流页面定稿 |
| `REPORT_CACHE_MAX_ENTRIES` | 正整数（默认 `512`） | 内存缓存的最大条目数 |
| `REPORT_CACHE_DISK_ENABLED` | `0`<br>`1`<br>（默认 `1`） | 跨重启保留闭市纪元的条目。傍晚纪元长达 16 小时，周末达 64 小时 |
| `REPORT_CACHE_DIR` | 路径，相对项目根目录（默认 `.runtime/report-cache`） | 磁盘缓存目录 |

盘中命中返回的必然是一份稍旧的快照，TTL 决定这份快照能有多旧。对资金流精度要求高时设为 `0`。
纪元划分、TTL 取值依据和实测数据见[报告缓存](docs/technical-details.md#10-报告缓存)。

### 市场宽度（同花顺）

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `MARKET_BREADTH_AUTH_FILE` | 路径（默认 `.runtime/tonghuashun-auth.json`） | 同花顺认证缓存文件 |
| `MARKET_BREADTH_COOLDOWN_SECONDS` | 秒（默认 `300`） | 同花顺认证失败后的冷却时间，冷却期内 `market_breadth` 直接用 efinance |

### 浏览器与虚拟显示

资金流兜底和 `market_breadth` 共用同一个浏览器实例，下面几项对两者同时生效。

| 配置名 | 可选参数 | 作用 |
| --- | --- | --- |
| `BROWSER_MAX_PAGES` | 正整数（默认 `3`） | 整个浏览器同时开着的页面数上限。这同时就是同时有几个渲染进程，是峰值内存的直接决定项：每多一个并发页约 +130 MiB |
| `BROWSER_IDLE_TIMEOUT_SECONDS` | 秒，`0` 关闭空闲回收（默认 `5400`） | 多久没人调用就把浏览器整个拆掉。90 分钟是为了盖住午休，拆早了下一批调用要重新付一次冷启动 |
| `BROWSER_DISGUISE` | `0`<br>`1`<br>（默认 `1`） | 把无头浏览器的自报特征改成普通浏览器的样子。不改的话 `sec-ch-ua` 请求头里写着 `HeadlessChrome`，容易被上游风控挑出来 |
| `BROWSER_CLAIM_PLATFORM` | `auto`<br>`real`<br>`macos`<br>`windows`<br>（默认 `auto`） | 对外声明哪个平台。`auto` 下 Windows/macOS 照实报，其余（服务器上的 Linux）统一报 macOS —— Linux 桌面在真实访客里占比极低。`real` 用于在部署机上做对照 |
| `BROWSER_NO_SANDBOX` | `0`<br>`1`<br>（默认 `0`） | 为 Chromium 添加 `--no-sandbox`。会降低浏览器隔离，仅在受控容器且 sandbox 确实不可用时启用 |
| `XVFB_DISPLAY_NUMBER` | 整数（默认 `99`） | 无 `DISPLAY` 时 `start.sh` 使用的 Xvfb 起始显示号，被占用则依次往后试到 109 |
| `XVFB_SCREEN` | `宽x高x色深`（默认 `1920x1080x24`） | Xvfb 屏幕配置 |
| `BROWSER_HEADFUL` | `0`<br>`1`<br>（默认 `0`） | 调试开关：资金流页面用有头浏览器加载，便于人工观察渲染结果 |
| `BROWSER_KEEP_PAGES` | `0`<br>`1`<br>（默认 `0`） | 调试开关：抓完不关页面。每个页面是一个独立渲染进程，会显著抬高内存 |

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

**报告里出现“盘中实时数据暂时不可用”**

东财资金流接口和页面兜底都没取到数据，其余部分不受影响。这是瞬时状态，不会被写进缓存。
科创 50（`SH000688`）等没有资金流向页面的指数在盘中本来就没有这一段。

**`market_breadth` 出现 fallback warning**

首选数据源认证失败、处于冷却期或浏览器不可用时会自动回退。响应仍可使用，但应关注
`source`、`trade_date` 和 `warnings`。

**批量请求被截断**

每次 tool 调用最多处理 4 个标的。需要更多标的时由客户端拆分请求，并控制并发，避免集中冲击上游接口。

## 更多文档

- [技术实现说明](docs/technical-details.md)：架构、数据链路与回退、输出契约、报告缓存、调优边界
- [完整报告示例](docs/SH603986-full.md)
- [DeepChat 使用示例](docs/let-your-deepseek-analyze-stock-by-mcp.md)

## 免责声明

本项目使用第三方公开数据接口，无法保证数据始终实时、完整或准确。项目输出不构成投资建议，
请勿将其作为交易决策的唯一依据。股市有风险，入市需谨慎。

## 许可证

基于原项目协议，本项目采用 MIT 许可证。问题和建议请通过 GitHub Issue 提交。
