# CnStock 技术实现说明

本文面向维护者和部署人员，说明 CnStock 的内部架构、数据获取策略、并发模型、输出契约
和性能调优边界。安装及日常使用请先阅读[项目 README](../README.md)。

## 1. 运行结构

服务基于 FastMCP，默认以无状态 Streamable HTTP 运行：

```text
MCP client
    |
    v
FastMCP tools (finmcp/mcp_app.py)
    |
    +-- report/indicator assembly (finmcp/research.py)
    |
    +-- compatibility data layer (finmcp/datafeed.py)
            |
            v
       DataSource contract
            |
            v
       CNStockDataSource
            |
            +-- AkShare / AkShare Proxy Patch
            +-- efinance
            +-- Playwright (selected real-time paths)
```

主要模块：

| 文件 | 职责 |
| --- | --- |
| `main.py` | 加载环境、记录版本、启动 MCP transport |
| `finmcp/mcp_app.py` | MCP tool、参数和 Pydantic 输出模型 |
| `finmcp/research.py` | 报告组装及技术指标计算 |
| `finmcp/datafeed.py` | 将统一数据对象转换为旧研究层字典格式 |
| `finmcp/datasource/base.py` | `DataSource`、`StockData`、`FetchRequirements` |
| `finmcp/datasource/cn_stock_source.py` | AkShare/efinance 数据源实现和执行器 |
| `finmcp/datasource/realtime_ff.py` | 交易时段实时资金流浏览器路径 |
| `finmcp/datasource/fund_flow_page.py` | 资金流向页面解析，纯标准库、不联网 |
| `finmcp/datasource/intraday_quote.py` | 盘中行情 provider 注册表与多级回退 |
| `finmcp/datasource/http_channel.py` | 出站 HTTP 通道的解析、安装与卸载 |
| `finmcp/datasource/market_breadth.py` | 全市场涨跌分布、缓存和回退 |
| `finmcp/cache.py` | 报告缓存与市场纪元判定 |

服务路径为：

- Streamable HTTP：`/cnstock/mcp`
- SSE：`/cnstock/sse`
- SSE message：`/cnstock/messages/`

## 2. 数据源抽象与兼容

`DataSource.fetch_stock_data(symbol, start_date, end_date)` 是稳定的完整抓取接口。已有或第三方
数据源只实现这个方法即可继续工作。

`fetch_stock_data_with_requirements(..., requirements=None)` 是非抽象扩展接口。基类实现会回退到
`fetch_stock_data`，因此旧数据源不会因选择性抓取能力而失效。`CNStockDataSource` 覆盖该方法，
可以按 tool 的实际依赖跳过不需要的上游请求。

`StockData` 是数据源与研究层之间的统一对象，包含：

- K 线：日期、开高低收、成交量、成交额。
- 复权数据：不复权收盘价、派息、送转。
- 财务数据：营收、利润、EPS、净资产、ROE、股本和市值。
- 估值数据：PE、PB。
- 资金流：主力及大中小单净流入与历史序列。
- 元数据：规范化代码、名称、板块和市场类型。

`StockData.to_dict()` 保留旧研究代码使用的字段名，例如 `CLOSE`、`CLOSE2`、`TCAP`、
`FCAP`、`_DS_FINANCE` 和 `_DS_FUND_FLOW`。修改字段或数组含义属于输出兼容性变更，不能作为
单纯性能优化合入。

## 3. 不同工具的数据需求

完整报告路径保持完整抓取，只有确认不影响输出的工具使用裁剪：

| Tool | 复权 K 线 | 不复权 K 线 | 财务 | 历史资金流 | 实时信息 |
| --- | --- | --- | --- | --- | --- |
| `brief` | 是 | 是 | 是 | 是 | 是 |
| `medium` | 是 | 是 | 是 | 是 | 是 |
| `full` | 是 | 是 | 是 | 是 | 是 |
| `tech` | 是 | 否 | 否 | 否 | 是，用于名称 |
| `kline_daily` | 是 | 否 | 否 | 否 | 否 |
| `kline_range` | 是 | 否 | 否 | 否 | 否 |

`brief` 看似简单，但基本信息、估值、换手和资金流仍依赖完整数据，不能按报告篇幅直接裁剪。
`tech` 的数值只依赖复权 OHLCV，不过返回对象还包含名称，因此保留实时信息请求。

## 4. 同步 I/O 与有界并发

AkShare 和 efinance 的主要接口是同步网络调用。服务通过进程级 `ThreadPoolExecutor` 执行这些
调用，避免阻塞 MCP 事件循环。

两个环境变量控制容量：

| 变量 | 默认值 | 含义 |
| --- | ---: | --- |
| `FETCH_MAX_WORKERS` | 8 | 同时执行同步数据任务的线程数 |
| `FETCH_MAX_IN_FLIGHT` | 16 | 已运行和已提交任务的总上限 |
| `BATCH_CONCURRENCY` | 2 | `brief/medium/full` 共享的活跃批次数上限 |
| `FINANCE_CACHE_TTL_SECONDS` | 21600 | 成功且非空的财务摘要缓存时间 |
| `FINANCE_CACHE_MAX_ENTRIES` | 512 | 财务缓存最大标的数，超过后淘汰最早项 |

Python 的 `ThreadPoolExecutor` 内部队列没有业务级上限，因此服务在提交前使用事件循环所属的
`asyncio.Semaphore` 做 admission control。达到上限后的请求以轻量协程等待，不会继续向线程池
堆积任务和参数对象。

客户端取消或超时时，已经运行的同步网络调用无法被 Python 安全中断。此时 permit 会一直保留到
底层 future 真正结束，防止调用方通过反复超时绕过并发上限。

每个同步任务在 DEBUG 日志中记录 `request_id`、`tool`、`symbol` 和：

- `admission`：等待进入有界执行器的时间。
- `queue`：提交后在线程池队列等待的时间。
- `service`：同步函数实际执行时间。

这些指标用于区分服务端排队与上游接口耗时，不会进入 MCP 响应。

财务摘要缓存会在线程池提交前检查。命中时直接返回 DataFrame 深拷贝，并记录
`Finance cache ... cache=hit age=...`；并发冷请求通过 singleflight 合并。失败或空结果不会缓存。
每次访问都会清理超过 TTL 的条目，达到容量上限时淘汰最早缓存；设置 TTL 为 `0` 可禁用。

`brief/medium/full` 在数据任务展开前共用批量准入控制。日志分别记录 `queued`、
`admitted`、`released` 以及 `queue/service/total`，用于判断入口排队和实际执行耗时。

Playwright 实时资金流额外记录 `semaphore_wait`、`service` 和 `singleflight_role`。
HTTP 层记录响应字节数、是否完成发送及 `client_disconnected`，
用来区分工具计算慢与调用端先关闭连接。

### Ubuntu 2 核 4G 建议

默认 `workers=8`、`in-flight=16` 面向网络 I/O，不表示需要 8 个 CPU core。单进程部署时建议
先保持默认值，并监控 MCP 进程、Chromium 和 Xvfb 的总内存。

不建议在 2 核 4G 上启动多个 Web worker，因为线程池、缓存、symbol 数据和浏览器资源会按进程
复制。提高线程数前应同时观察 P90/P95、错误率、上游限流和峰值 RSS；吞吐不再增长时应回退。

## 5. 批量请求

`brief`、`medium`、`full` 和 `tech` 在 tool 层将逗号分隔的输入截断为最多 4 个标的，并使用
`asyncio.gather` 并行处理。所有请求共享同一个有界同步执行器，因此多个 MCP 调用同时到达时仍受
全局容量保护。

`tech` 在并行任务完成后按输入顺序写入 `reports` 和 `errors`，避免线程完成顺序改变 JSON key
顺序。批量上限之外的代码不会静默丢弃，而是写入 `warnings`。

## 6. 出站 HTTP 通道

部分东方财富接口会直接断开普通 HTTP 客户端的连接，表现为空响应体和
`Expecting value: line 1 column 1 (char 0)`。`HTTP_CHANNEL` 决定用哪种方式访问这些主机。
四种模式互斥，同一进程只安装一个：两个非 `direct` 的实现改写的是同一组 requests 模块属性，
同时安装只会剩下最后一个生效。

| 模式 | 行为 |
| --- | --- |
| `auto` | 网关可用时选 `proxy`，否则降级 `impersonate`（默认） |
| `proxy` | 经 AkShare Proxy Patch 的授权网关和代理出口，附带 Cookie 与积分计费 |
| `impersonate` | 本机直连，用 curl_cffi 伪装浏览器 TLS 指纹，无需网关 |
| `direct` | 本机直连 + 原生 `requests`，即引入该开关之前的行为（可写作 `off`） |

通道在 `CNStockDataSource` 模块加载时安装，且必须早于 `efinance` 的导入。只有 `push2`、
`push2his`、`fund`、`emweb.securities` 这四个东方财富主机会被接管；同花顺以及
`market_events` 用到的 `datacenter-web`、`push2ex` 均原样直连。

`impersonate` 只更换 TLS 指纹，不涉及浏览器；抓实时资金流的 Playwright 是另一条独立链路。
连续 `IMPERSONATE_SUSPEND_AFTER_FAILURES` 次请求打满重试仍失败后，该通道进入
`IMPERSONATE_SUSPEND_SECONDS` 冷却，期间直接走原生 `requests`——否则一个
根本无法完成伪装的环境会在每次调用上白付一遍重试预算，外加一次原生重放。

冷却期内这四个主机注定失败（它们被接管的理由正是拒绝原生 `requests`），所以数据源层
通过 `impersonated_hosts_degraded()` 直接跳过它们，不再逐个源用自己的连续失败计数
重新发现一遍。2026-09-05 部署机日志里，少了这一步的代价是 26 秒内 18 次必败的 K 线
请求，每次都付满 `IMPERSONATE_RETRY × IMPERSONATE_TIMEOUT_SECONDS`。

`auto` 永不因配置缺失而启动失败，只会降级并打印 WARNING；只有显式写
`HTTP_CHANNEL=proxy` 却没有配 `AKSHARE_PROXY_GATEWAY` 时才会启动即报错。

启动日志会在版本信息之后打印实际生效的通道：

```text
cn-stock-mcp version=2.0.0
Market data library versions: akshare=... efinance=...
HTTP channel mode=impersonate reason=auto:proxy_disabled profile=chrome retry=3 timeout=8.0s hooked_hosts=4
HTTP channel mode=proxy reason=auto:proxy_configured gateway=... token=configured retry=30 patch_version=0.5.0 hooked_hosts=4
HTTP channel mode=direct reason=requested hooked_hosts=4
```

`reason` 会写明是显式指定（`requested`）、`auto` 的判定结果，还是降级
（`curl_cffi_unavailable`、`requests_already_patched`），降级同时记 WARNING。

注意入口执行 `load_dotenv(override=True)`，**`.env` 的取值优先于 shell 环境变量**。临时切换
通道要改 `.env` 或注释掉其中的 `HTTP_CHANNEL`，`HTTP_CHANNEL=direct ./start.sh`
这种写法会被 `.env` 覆盖掉。

### AkShare Proxy Patch

`AKSHARE_PROXY_ENABLED` **默认关闭**：网关是付费的，每次认证都计积分，而 `impersonate` 在
同样这四个主机上已经能独立取到数据。关闭时不导入、不安装 `akshare_proxy_patch`，AkShare
走原生请求链路。该开关只作为 `auto` 的判定输入，不再单独触发安装；修改后需要重启服务。

相关变量为：

```env
AKSHARE_PROXY_ENABLED=1
AKSHARE_PROXY_GATEWAY=...
AKSHARE_PROXY_TOKEN=...
AKSHARE_PROXY_RETRY=30
```

一次 MCP 调用可能产生多个命中代理的 HTTP 请求，失败重试还会放大请求数。因此真实并发
benchmark 应以代理尝试数设置预算，不能用 MCP 调用次数估算积分消耗。高样本并发测试应优先使用
冻结 fixture 或延迟回放，只用少量真实调用校准网络方向。

降低重试次数可能改变最终成功率和错误内容，属于可靠性策略变更，不应混入纯性能优化。

## 7. 盘中数据链路与回退

### 实时资金流与页面兜底

交易时段 `brief/medium/full` 的今日资金流来自 `data.eastmoney.com/zjlx/<code>.html` 的浏览器
加载（`realtime_ff.py`），进程内共用一个 Chromium 实例和一个 context，并发由信号量限到 2。
解析逻辑单独放在 `fund_flow_page.py`：纯标准库、不联网，因此可以被单元测试完全覆盖。

这个页面同时带今日和历史两块。东财的 `fflow` 接口拒绝出口 IP 时（2026-09-03 push2his 连续
拒绝超过半小时），一次页面加载就能把两块都补回来，且不消耗网关积分。页面上的数值是预格式化的
（`1.59亿` / `-4725.87万`），但报告本身按同样的两位小数渲染，所以往返无损。占位符解析为
`None` 而不是 `0`：停牌和开盘前页面就是占位符，当成 0 会让报告声称"主力净流入 0 元"。

页面路径成本高于一次 HTTP 请求，所以有四道约束，避免它在上游全面失败时变成稳态：

- `FALLBACK_CONCURRENCY` 低于浏览器信号量，兜底不会饿死没有替代来源的实时路径。
- `FALLBACK_WAIT_SECONDS` 内拿不到槽位就直接渲染"盘中实时数据暂时不可用"，不排队。
- 连续 `FAILURE_THRESHOLD` 次徒劳加载后整层进入 `COOLDOWN_SECONDS` 冷却。
- `REUSE_SECONDS` 复用已解析结果。页面级单飞只能合并并发加载，而实时预取与资金流兜底在
  一次请求里相隔约 4 秒先后发生，没有复用窗口时同一页面会被加载两次。

无头浏览器会被这个页面识别。2026-09-04 实测同一组 16 个沪深标的各加载一次：加上
`--disable-blink-features=AutomationControlled` 之前，今日块 2/16 成功、历史表 0/16；加上之后
分别是 15/16 和 14/16。这个启动参数是必需项，不是可选优化。另有 8 轮全新浏览器、每轮最多三次
的实测显示成功全部发生在前两次，所以 `FUND_FLOW_PAGE_MAX_LOADS` 定为 2。

### 空闲回收：超时按市场时段分档

浏览器不用时整个拆掉，由 `BROWSER_IDLE_TIMEOUT_SECONDS` 控制。2026-09-04 实测一次页面加载
之后常驻的浏览器进程树是 257.6 MiB，`close_browser()` 用 0.03s 回收到 0 MiB / 0 进程，不留残余；
代价是空闲后第一个请求多付 2.19s（冷 2.88s vs 热 0.69s）。附带收益是清掉每轮约 26 MiB 的慢渗漏。

**现在的单一取值解决不了两个方向相反的需求。** 默认 5400 秒（90 分钟）的依据是"盖住午休
11:30–13:00"，那是个**只在盘中成立**的理由：一整个午休零调用时，浏览器会恰好在 13:00 前后
被拆掉，开盘第一个请求付那 2.2s。可是收盘后到次日开盘有 18 小时，周末更长，同样要空转
90 分钟才回收——而那段时间根本没有午休要盖。

2026-09-06 部署机实测，服务进程树在"热空闲"（最后一次调用 90 分钟内）是 621 MiB：

| 进程 | RSS | 占比 |
|---|---:|---:|
| python 服务本体 | 243 MiB | 39% |
| playwright driver（node） | 111 MiB | 18% |
| chrome `--type=gpu-process` | 102 MiB | 16% |
| chrome 主进程 | 93 MiB | 15% |
| chrome NetworkService | 72 MiB | 12% |

浏览器那四个进程合计 **378 MiB，占 61%**。拆掉之后只剩 243 MiB。

所以超时值按 `market_session` 的时段分档：

| 时段 | 超时 | 理由 |
|---|---:|---|
| `LIVE` / `LUNCH` | 5400s | 盖住午休，保住开盘后第一个请求的 0.69s |
| `POSTCLOSE` / `CLOSED` | 300s | 没有午休要盖；这段时间对延迟不敏感，报告缓存在同一纪元内还会挡掉重复查询 |

**收益**：盘外（约 18 小时/天，加整个周末）常驻从 621 MiB 降到 243 MiB，且慢渗漏清得更勤。
**代价**：盘外空闲后第一个请求多付 2.19s。
**为什么收益大于代价**：省的是 18 小时 × 378 MiB 的常驻，付的是盘外偶发请求的 2.2 秒——
两者都有量级，且不在同一个数量级上。

取值时机是**排定时器的那一刻**（`_arm_idle_timer`），不是到期时。这意味着 15:59 排下的
定时器会按盘中口径留到 17:29——每天最多多留一个窗口，可接受；换成到期时再判就要引入
周期性唤醒，而现在这个定时器刻意做成"没有借用者时才存在"。

与 P0 的联动不变：`close_browser()` 必须连带复位 `_session_warm`，否则标志还是 True 而预算
塌到 1，只 goto 不 reload，反而降低获取率。分档只改超时值，不碰这条。

### 被拒的机制与无头特征

2026-09-04 抓到了被拒时的完整时序：`/fflow/` 在页面加载后约 1 秒以
`net::ERR_EMPTY_RESPONSE` 失败，**600 毫秒之后**页面才去问
`websitecaptcha/api/checkuser`，拿到 `{"block":true}`，随后挂上
`<iframe class="popwscps_d_iframe">` 的滑块模态框。所以空响应就是风控本身，滑块是它给的
解法，而判定发生在请求那一刻——那时页面刚加载完，没有任何行为可供观察。这也说明"打开页面后
滚动一下"之类的行为伪装对当次取数不可能有作用。

判定依据只能是请求时就存在的东西，而我们原先在每个请求头里写着：

```text
sec-ch-ua: "Not:A-Brand";v="99", "HeadlessChrome";v="145", "Chromium";v="145"
user-agent: Mozilla/5.0 (Macintosh; ...) Chrome/120.0.0.0 Safari/537.36
```

`HeadlessChrome` 是自报身份，不是细微指纹；而且硬编码的 UA 说 Chrome/120、client hints 说 145，
自相矛盾——在 Linux 服务器上还会变成 UA 说 Macintosh、`sec-ch-ua-platform` 说 Linux 的第二重矛盾。
`BROWSER_DISGUISE` 用 CDP `Network.setUserAgentOverride` 把 UA 与 client hints 一起改成
自洽的非 Headless（版本号和平台都从真实构建现算），配合 `locale=zh-CN`、1920x1080 视口，以及一段
只补 `window.chrome` / `plugins` / `pdfViewerEnabled` 的 init script。

注意 `setUserAgentOverride` 是 **per-target** 的：在 context 上装一次不会被后建页面继承，
实测第一个页面装完之后，新建页面的 `sec-ch-ua` 依旧是 `HeadlessChrome`。所以
`load_fund_flow_page` 在每次 `new_page()` 之后、`goto()` 之前都调一次。

换完整 Chromium 的新无头模式能得到同样的指纹，但实测浏览器进程树 footprint 从 63.7/95 MiB
涨到 323/401 MiB（+260），会把服务总量推到 710-810 MiB，按第四条内存约束不可采纳；上面这套
CDP 方案只 +2/+16 MiB。

被拒的**时长有两种**，不要混为一谈：瞬时的立刻重试就过（2026-09-04 15:51 的 SH512480 第一次
`blocked_captcha`、593 毫秒后第二次拿到 `today=True history=121`），这是 `FUND_FLOW_PAGE_MAX_LOADS=2`
存在的理由；持续的可达分钟级（15:35-15:50 实测背靠背 10 次全拒、静默 75 秒仍拒、16 分钟后仍拒），
重试无用只能等。单次日志区分不了两者，所以策略是最多试两次然后放弃。

### 盘中行情多级回退

兜底 K 线源（腾讯日 K）在盘中不包含当天，直接用会让报告在 09-04 盘中返回 09-03 的收盘数据。
`intraday_quote.py` 是一层可插拔的实时行情解析：provider 各自注册，
`INTRADAY_QUOTE_PROVIDERS` 按逗号顺序决定尝试次序，`off` 关闭整层。

| provider | 网络成本 | 字段 |
| --- | --- | --- |
| `fund_flow_page` | 零请求，复用已解析的资金流页面页头 | 最新价、涨跌、涨跌幅、换手率、总手、成交额；**没有开高低** |
| `tencent` | 一次 `qt.gtimg.cn` 请求 | 开高低收、成交量、成交额、换手率、行情时间 |

只有拿到完整 OHLC 且行情时间晚于序列最后一行时才会追加当日 bar，后复权序列一律跳过。
`collect()` 可以一次拿到所有 provider 的结果，`compare()` 按容差交叉校验，用于排查两个来源
是否在同一时刻给出不一致的价格。

### 上游源熔断

东方财富按端点限流：某个端点拒绝出口 IP 时，同一主机的其他路径仍然正常。没有熔断时每个请求
都要把整条 provider 链走完——efinance 重试、三次 impersonate 尝试、AkShare 重试——才轮到
0.2 秒就能返回的腾讯兜底。跳过一个明确在拒绝的源，每次请求省 1.5–3.7 秒。

`SOURCE_BREAKER_OPEN_AFTER_FAILURES` 默认 3：历史基线是约 1.5% 的散点失败率，连续三次偶然发生的概率是
0.003%，而一次真实封禁产生了 111 次连续失败。冷却结束后半开放行一次探测，因此该值只决定
恢复延迟，不决定熔断打开期间的成本。

## 8. 市场宽度数据

`market_breadth` 与股票报告使用独立的数据获取链路：

1. 优先使用同花顺数据。
2. 复用持久化认证信息，必要时通过 Playwright 刷新。
3. 认证失败或处于冷却期时回退到 efinance。
4. 响应通过 `source` 和 `warnings` 暴露实际数据源及降级情况。

相关配置：

| 变量 | 用途 |
| --- | --- |
| `MARKET_BREADTH_AUTH_FILE` | 覆盖认证缓存文件路径 |
| `MARKET_BREADTH_COOLDOWN_SECONDS` | 认证失败后的冷却时间，默认 300 秒 |
| `BROWSER_NO_SANDBOX` | 为 Chromium 增加 no-sandbox 参数 |
| `XVFB_DISPLAY_NUMBER` | `start.sh` 使用的 Xvfb 显示号，默认 99 |
| `XVFB_SCREEN` | Xvfb 屏幕配置，默认 `1920x1080x24` |

`BROWSER_NO_SANDBOX` 会降低浏览器隔离，仅应在受控容器且 Chromium sandbox 确实不可用
时启用。

市场宽度结果带有短 TTL 缓存，并通过锁合并并发 cache miss，避免多个请求同时刷新同一份全市场
数据。调用方仍应检查 `trade_date` 和 `market_time`，尤其是在收盘后、周末和回退场景。

## 9. 输出与错误契约

`brief`、`medium`、`full` 和 `tech` 共用同一个批量外壳，返回 Pydantic 模型：

- `reports` 保存成功结果，键是规范化后的证券代码。`brief/medium/full` 的值是 Markdown 文本，
  `tech` 的值是结构化对象，含 `symbol`、`name`、`quote_date` 和按日期排列的 `indicators`。
- `errors` 保存单标的失败，不因一个标的失败而丢弃整个批次。
- `warnings` 保存截断、回退或部分数据提示。
- `symbols_count` 表示应用批量上限后的请求数量，不等同于成功数量。
- `timestamp` 是报告生成时间，因此不适合作为跨时间逐字节比较字段。

`tech` 的数值字段只允许 JSON number 或 `null`，不会输出 `NaN`/`Infinity`；不可计算或缺失的
指标一律是 `null`。指标日期按最近交易日优先排列。

`market_breadth` 返回 `source`、抓取时间、涨跌和平盘家数、涨跌停家数、十档涨跌幅分布及回退
警告。调用方应读取 `source` 和 `warnings`，不要假设每次都来自同一提供方。

`market_events` 按指定日期返回严格 as-of 的结构化公开事件池。响应会主动删除龙虎榜
“上榜后 N 日”等未来字段；历史池为空时通过 `warnings` 提示供应商保留窗口，**不能把空表解释
为当日无事件**。`earnings_forecast` 按查询日自动选择最近已结束报告期（1-3 月取上年年报、
4-6 月取一季报、7-9 月取中报、10-12 月取三季报），再按 `announcement_lookback_days` 过滤公告
日期。生产回测仍应优先读取每日归档，因为供应商的报告期快照可能覆盖历史修订版本；该来源会
返回 `revision_safe=false` 和明确 warning。

`kline_daily` / `kline_range` 保留原 efinance 和 AkShare 主路径，主路径失败时自动回退腾讯历史
行情，返回格式不变。

## 10. 报告缓存

`finmcp/cache.py` 按标的缓存已渲染的输出，目的是降低 AkShare Proxy Patch 的积分消耗。
它与数据源层解耦，`REPORT_CACHE_ENABLED=0` 时完全不参与调用链。

### 纪元模型

条目绑定"市场纪元"——只在重新生成会得到同样字节的窗口内复用，因此命中与否不改变返回内容。

| 时段 | 阶段 | 复用行为 |
|------|------|----------|
| 09:15–11:30、13:00–SETTLE | live | 仅 `LIVE_TTL_SECONDS` 内复用 |
| 11:30–11:35 | live | 午休缓冲，同上 |
| 11:35–13:00 | lunch | 纪元内完全复用 |
| SETTLE–17:00 | postclose | 纪元内完全复用 |
| 17:00–17:05 | live | 傍晚缓冲，同上 |
| 17:05–次日 09:15、周末 | closed | 纪元内完全复用 |

`SETTLE` 由 `REPORT_CACHE_SETTLE_TIME` 配置，默认 15:30，取值夹在 `[1500, 1700]`。
闭市纪元锚定在刚结束的交易日上，因此周五傍晚到周一开盘是一个连续纪元。

### 缓冲窗口的必要性

上游不在时段边界的瞬间定稿：东财资金流页面在早盘结束后仍要几分钟才稳定，AkShare 当日
资金流行也在 17:00 后才落地。而 `research.build_fund_flow` 打印最新一行时不做日期校验，
一律标注"今日"。若纪元在边界瞬间开启，一份尚未定稿的渲染会被钉住整个窗口。因此每个
边界之后都有一段 TTL 受限的缓冲窗口，并使用独立的纪元 token——复用 `live-` token 会让
盘中条目与傍晚条目混淆渲染分支。

### 键的构成

`渲染指纹 | 工具 | 标的 | 参数 | 纪元 | 取数窗口日期`

- **渲染指纹**是版本号与 `research.py`/`mcp_app.py`/`cache.py`/`config.py` 以及
  `finmcp/confs/indices.json` 内容的哈希。闭市纪元最长 64 小时且磁盘层跨重启存活，没有它则
  傍晚上线的渲染修复要到次日开盘才可见。`finmcp/confs/indices.json` 之所以计入，是因为
  `ALL_INDICES` 决定标的走指数分支还是个股分支（`research.get_realtime_fund_flow_target`），
  改它是一次渲染变更，尽管没有任何 `.py` 文件变动。
- **取数窗口日期**覆盖 `research.load_raw_data` 用 `now() + 1 天` 推导取数区间的行为，
  使缓存在零点自动分裂。

### 没有"已结算"快速通道

指定历史日期**不**使结果稳定，两个原因：报告类工具仍打印实时总市值、流通市值和动态
市盈率（`_fetch_realtime_sync` 不看 `date` 参数）；前复权序列是在除权日**当天**被重算的，
不是隔夜，所以"当日内可自由复用"会在除权日返回重算前的价格。因此所有工具一律走市场纪元。

### 不进入缓存的内容

空结果、异常结果，以及含 `[实时抓取失败]`、`[实时调用异常]`、`盘中实时数据暂时不可用`
标记的报告——这些是瞬时状态，缓存会把它们固化一个纪元。`market_breadth` 不接入缓存：
它以同花顺为主源，不消耗代理积分。

### 磁盘层

写在 `REPORT_CACHE_DIR`，用于跨重启保留闭市纪元的条目。目录名带 `epoch-` 前缀，
清理只删自己创建的目录且需超过保留期，因此把该变量指向已有目录不会破坏其内容。

### 实测

基于下游 57 天真实调用日志回放：

| 盘中 TTL | 标的复用率 | 代理积分降幅 | 整批全命中 |
|---------|-----------|------------|-----------|
| 0（盘中不复用） | 12.4% | 11.8% | 21.2% |
| **30 秒（默认）** | **19.6%** | **19.3%** | **34.2%** |
| 60 秒 | 22.3% | 22.1% | 38.0% |

收益上限由需求分布决定而非策略：58.5% 的请求发生在盘中。盘中重复查询的间隔呈双峰分布，
60 秒内占 17.3%，其余集中在 600 秒以上（中位 1777 秒），因此 TTL 超过 60 秒几乎没有额外收益。

### 盘中 TTL 的取值依据

盘中"命中等于新调用"本就不成立——行情在动，TTL 是在明确接受一段陈旧度。把下游捕获里
盘中同标的、间隔在窗口内的相邻样本逐对比对（1724 个捕获文件，60 秒窗口 113 对、30 秒窗口
47 对），实测结果：

| 字段 | 60 秒 P90 漂移 | 30 秒 P90 漂移 |
|------|--------------|--------------|
| 当日价 | 0.23% | 0.15% |
| 当日成交量（实时） | 15.92% | 0.55% |
| 今日主力净流入 | 21.07% | 7.74% |
| 今日超大单净流入 | 34.77% | 19.30% |

两个窗口下逐字节一致的比例都是 0%——浏览器抓取的资金流每秒都在跳，命中必然返回稍旧的
快照。方向翻转（净流入读成净流出）的比例较低：60 秒窗口下主力 0.9%、超大单 3.5%。

默认取 30 秒，是用约 2.8 个百分点的积分降幅换取主力净流入 P90 漂移从 21% 降到 7.7%。
对资金流精度要求更高的部署可设为 0，此时仍保留 11.8% 的积分降幅（全部来自闭市窗口）。

注意净流入是有符号量、会在零附近震荡，因此相对变化的**最大值**会被严重放大，判断时应看
中位、P90 和翻转率。分析脚本见 `benchmarks/intraday_staleness.py`。

`tech` 三档压测（2026-08-20 postclose 纪元，同一组 4 标的）：

| 档位 | baseline 380dd66 p95 | 关缓存 p95 | 开缓存 p95 |
|------|---------------------|-----------|-----------|
| 1×4  | 18.29s | 4.02s | 7.95s（冷启动） |
| 5×4  | 21.24s | 3.30s | 0.77s |
| 10×4 | 39.36s | 5.75s | 1.30s |

16 批共 64 个标的级请求中只有 4 次真实回源，积分 192 → 12。关缓存一轮同时确认冷路径无回归；
该轮相对 baseline 的优势主要来自 `90888e6` 与 `886bc24` 的既有优化，baseline 停在 `380dd66`。
开缓存档位的 0.77–1.30 秒是 mcporter 进程启动与 MCP 建连的地板，服务端命中实测为 0.000 秒。

## 11. 性能验证原则

性能修改需要同时验证：

1. 固定 fixture 下的输出语义一致。
2. 单标的与单批 4 标的延迟。
3. 同时 5 批和 10 批、每批 4 标的的吞吐与长尾。
4. 成功率、空数据率和上游错误率不变差。
5. Ubuntu 2 核 4G 的 MCP + Chromium 峰值内存可接受。
6. 交易时段与收盘后的路径分别测试。

真实端到端计时可使用：

```bash
mcporter call cn-stock tech \
  symbol=SH600000,SZ000333,SZ300750,SH688981 \
  --config ~/.openclaw/workspace/config/mcporter.json
```

客户端 wall time 包含 mcporter 进程启动、配置加载、MCP 建连、响应解析和输出；服务端
`admission/queue/service` 日志只反映内部数据任务。两者应分别报告，不应相互替代。

收盘后的测试不会执行所有交易时段 Playwright 路径，因此不能据此推断交易时段最大延迟。

## 12. 测试边界

建议修改后至少运行：

```bash
pytest tests --ignore=tests/test_akshare_source.py
git diff --check
```

`tests/test_akshare_source.py` 仍引用已删除的旧 `AkShareDataSource`，不属于当前实现的有效测试集。

重点测试包括：

- `DataSource` 旧实现的兼容回退。
- `tech` 是否跳过无关数据源且保持输出结构。
- 完整抓取是否仍请求全部数据。
- 执行器在并发和取消场景下是否遵守上限。
- 多事件循环是否使用各自的 limiter。
- 报告缓存的纪元边界、缓冲窗口、渲染指纹和磁盘清理边界。
- 开关缓存前后输出是否逐字节一致（`tests/test_report_cache_consistency.py`）。

`tests/conftest.py` 默认为每个用例装入一个关闭状态的缓存并隔离生产目录。断言"是否回源"
的测试必须在关闭状态下运行；需要缓存的测试自行调用 `set_report_cache` 覆盖。
- 批量技术报告是否保持输入顺序。
- 运行时版本是否与项目元数据一致并写入启动日志。

涉及真实数据源的测试会受网络、交易时段和代理积分影响，不应作为高频单元测试。单元测试应在
同步数据函数边界使用 fixture 或 monkeypatch，真实 MCP 调用用于发布前的小样本集成验证。
