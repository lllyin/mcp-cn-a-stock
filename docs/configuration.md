# 配置参考

[返回 README](../README.md)

所有配置都通过 `.env` 提供，全部可省略，省略即使用下表的默认值；改完需要重启服务。
可以从 `.env.example` 复制一份来改。

- [出站 HTTP 通道](#出站-http-通道)
- [AkShare Proxy Patch（可选，付费）](#akshare-proxy-patch可选付费)
- [取数源与顺序](#取数源与顺序)
- [资金流页面兜底](#资金流页面兜底)
- [上游源熔断](#上游源熔断)
- [并发与线程池](#并发与线程池)
- [市场纪元边界](#市场纪元边界)
- [缓存](#缓存)
- [市场宽度（同花顺）](#市场宽度同花顺)
- [浏览器与虚拟显示](#浏览器与虚拟显示)

> `.env` 的取值优先于 shell 环境变量。`HTTP_CHANNEL=direct ./start.sh` 会被 `.env` 里的
> 同名项覆盖，临时改配置请直接改 `.env`。

要和别的程序共存时设 `ENV_PREFIX`，之后所有配置名都带上这个前缀（`ENV_PREFIX=CNSTOCK_`
时写 `CNSTOCK_HTTP_CHANNEL`）。`AKSHARE_PROXY_*` 属于第三方插件，不受影响。

## 出站 HTTP 通道

部分东方财富接口会直接断开普通 HTTP 客户端的连接，`HTTP_CHANNEL` 决定用哪种方式访问这些
主机。默认的 `auto` 在没有配置网关时使用 `impersonate`，无需任何额外账号。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `HTTP_CHANNEL` | 访问东财行情主机的方式：<br>`auto` 按 `AKSHARE_PROXY_ENABLED` 选择，并在运行中按请求回退<br>`proxy` 经授权网关和代理出口，按积分计费<br>`impersonate` 本机直连，伪装成浏览器<br>`direct` 本机直连，不做伪装，可写作 `off` | `auto`<br>`proxy`<br>`impersonate`<br>`direct`<br>（默认 `auto`） |
| `IMPERSONATE_RETRY` | 单个请求的伪装尝试次数，用尽后不带伪装再试一次 | 正整数（默认 `3`） |
| `IMPERSONATE_TIMEOUT_SECONDS` | 单次伪装请求的超时 | 秒（默认 `8`） |
| `EASTMONEY_FALLBACK_TIMEOUT_SECONDS` | 东财 API 原生重放和网关发送的连接/读取超时上限；保留调用方更短的 timeout，较长或无限 timeout 按此上限收敛 | 秒（默认 `8`） |
| `IMPERSONATE_BROWSER` | 伪装成哪个浏览器 | 浏览器名，如 `chrome`、`safari`（默认 `chrome`） |
| `IMPERSONATE_SUSPEND_AFTER_FAILURES` | 连续多少次请求打满重试仍失败后暂停伪装通道 | 正整数（默认 `4`） |
| `IMPERSONATE_SUSPEND_SECONDS` | 暂停时长。期间东财源直接跳过，改用备用源 | 秒（默认 `300`） |
| `EASTMONEY_AUTH_ENABLED` | 是否自动取得并复用东财访问凭据，提高行情列表、快照和资金流接口的成功率 | `0`<br>`1`<br>（默认 `1`） |
| `EASTMONEY_AUTH_TTL_SECONDS` | 多久主动刷新一次。到期后后台刷新，新值到手前继续使用旧值；连续被拒也会触发刷新 | 秒（默认 `21600`） |
| `EASTMONEY_AUTH_PAGE` | 采集凭据的页面，通常不需要修改 | URL（默认 `https://quote.eastmoney.com/center/gridlist.html`） |
| `EASTMONEY_AUTH_INVALIDATE_AFTER_FAILURES` | 连续多少次被拒后刷新凭据 | 正整数（默认 `3`） |
| `EASTMONEY_AUTH_HARVEST_TIMEOUT_SECONDS` | 一次采集的总预算，超时后继续走既有请求链路 | 秒（默认 `45`） |

只有少数东方财富行情主机会被接管，其余原样直连；详见[出站 HTTP 通道](technical-details.md#6-出站-http-通道)。

项目管理的东财 HTTP 链使用单调时钟计算总预算，覆盖伪装尝试、原生重放及网关的等待、
认证和重试；预算由上表的超时、重试次数及网关等待配置的最大值计算，嵌套请求只使用剩余
时间。原生重放和网关发送不再叠加第三方 Session 的隐式重试，调用方更短的超时仍保留。
客户端取消后停止排队任务、缓存等待、退避等待和后续取数；已经发出的同步 HTTP 由连接/
读取超时收尾，不能通过取消 asyncio 任务强杀线程。`HTTP budget`、`gateway_skip` 日志可
用于核对实际耗时和退出原因。

## AkShare Proxy Patch（可选，付费）

**默认关闭。** 这是一个按积分计费的授权网关，不配置也能正常使用全部工具；上游对本机出口 IP
限流严重时可以启用它来提高东财接口的成功率。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `AKSHARE_PROXY_ENABLED` | 网关使用方式；`auto` 仅在东财本地请求失败后按请求回退 | `0`<br>`1`<br>`auto`<br>（默认 `0`） |
| `AKSHARE_PROXY_GATEWAY` | 授权网关地址，不含协议和端口 | 主机名或 IP（默认空） |
| `AKSHARE_PROXY_TOKEN` | 网关访问令牌 | 字符串（默认空） |
| `GATEWAY_EXIT_RETRIES` | 一次网关请求最多尝试几次，每次换一个新出口；拿不到新出口也只算一次失败，隔一秒再要，N 次全用尽才进冷却（旧名 `AKSHARE_PROXY_RETRY` 仍然认） | 次数（默认 `3`） |
| `AUTO_PROXY_AFTER_FAILURES` | `AKSHARE_PROXY_ENABLED=auto` 时，触发网关回退前的连续本地失败次数 | 正整数（默认 `3`） |
| `AUTO_PROXY_COOLDOWN_SECONDS` | N 次尝试里一次都没从认证服务拿到新出口时的暂停时长；拿到过新出口就说明坏的是出口不是认证，改用下面的短冷却 | 秒（默认 `300`） |
| `AUTO_PROXY_DATA_COOLDOWN_SECONDS` | 网关数据失败（出口已拿到、请求没成）后的暂停秒数；失败同时作废缓存的认证，下一次尝试换新出口 | 秒（默认 `30`） |
| `AUTO_PROXY_RECOVERY_PROBES` | 网关回退激活期间，本地成功多少次就退出回退。默认 `1`：间歇性拒绝下"连续 N 次"几乎攒不够，网关会永久激活；误判恢复的代价只是几个请求走回退链 | 次数（默认 `1`） |
| `AUTO_PROXY_RECOVERY_INTERVAL_SECONDS` | 相邻两次恢复探测的最小间隔秒数；间隔内的本地成功不累计 | 秒（默认 `60`） |
| `GATEWAY_TRANSPORT` | 网关传输实现；接新的代理库时在 `gateway.py` 写一个 `GatewayTransport` 实现再加一个可选值 | 默认 `akshare_proxy_patch` |
| `GATEWAY_AUTH_REUSE_SECONDS` | 一份网关出口凭据的复用上限；出口死亡是静默的，由失败即作废兜住 | 秒（默认 `600`） |
| `GATEWAY_SINGLEFLIGHT_WAIT_SECONDS` | 同一主机同一接口族已有网关请求在飞时，其余请求等它出结果的上限。只管通道层自动回退（基本数据、K 线等，后面还有别的源可退）；资金流链尾的网关级后面没有回退，会等满领头请求的最坏耗时（换出口次数 ×（认证超时 + 请求超时）），不看这一项 | 秒（默认 `5`） |

从旧版本升级时注意：这个开关以前默认开启，现在需要显式写 `AKSHARE_PROXY_ENABLED=1`
才会继续走网关，否则自动降级到 `impersonate`。

## 取数源与顺序

每一维数据都可以配多个来源，逗号分隔按顺序尝试，前一个没取到就问下一个，`off` 关掉整层。
多个来源各给一部分字段时会合起来用。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `BASIC_INFO_PROVIDERS` | 基本数据（市值、市盈率、市净率）的尝试顺序，后面的源补前面缺的字段：<br>`eastmoney` 字段最全，需要网关或未被封的出口<br>`tencent` 无需鉴权，没有网关的部署靠它兜住这一组 | `eastmoney`<br>`tencent`<br>`off`<br>（默认 `eastmoney,tencent`） |
| `FINANCE_PROVIDERS` | 财务报表（净利润、营业总收入、每股收益、每股净资产、净资产收益率）的尝试顺序：**第一个给全这六列的源整份胜出**，不按字段跨源拼表——两家的报告期轴不同，拼一次错一位就是把上一年的净利润配到今年的净资产收益率上。`ths` 列最多、是主源；`sina` 是独立主机，一次调用带全部历史期。年度净资产收益率两家口径不同，走了回退时报告会在财务段写明这一节来自谁 | `ths`<br>`sina`<br>`off`<br>（默认 `ths,sina`） |
| `INTRADAY_QUOTE_PROVIDERS` | 盘中实时行情的尝试顺序，逗号分隔按序尝试，`off` 关闭整层。**当天那一根 K 线只认这一层**（历史 K 线给的当天数据不作准）：<br>`fund_flow_page` 复用已解析的资金流页面，不发请求但没有开高低<br>`tencent` 字段全<br>`tonghuashun` 字段全<br>`sina` 个股/ETF 的末级兜底，不提供指数报价和换手率 | `fund_flow_page`<br>`tencent`<br>`tonghuashun`<br>`sina`<br>`off`<br>（默认 `fund_flow_page,tencent,tonghuashun,sina`） |
| `INTRADAY_QUOTE_PROVIDERS_INDEX` | **指数**用的顺序，和上一项分开配：指数的成交量各源口径不一致，腾讯/新浪比东财/同花顺低约 3.5%（两家同源，互相校验不了）。东财是基准源，所以指数把同花顺排前面；个股各源逐位一致，不换 | `fund_flow_page`、`tonghuashun`、`tencent`、`off`（默认 `fund_flow_page,tonghuashun,tencent`） |
| `INTRADAY_QUOTE_CROSS_CHECK_PCT` | 报价字段相对偏差的告警阈值。排查时按当前环境设置；这一段挡在报价返回之前，开启会串行请求所有剩余来源，每个标的多付一份下面的预算 | 百分比，`0` 关闭（默认 `0`） |
| `INTRADAY_QUOTE_CROSS_CHECK_BUDGET_SECONDS` | 上面那轮校验的总预算。预算会穿给各来源压缩其超时，所以是真正的总上限而非单次超时。按最大值定不按分位数：各源单次最大耗时 × 源个数再留余量 | 秒，`0` 不限（默认 `2`） |
| `LOG_FILE` | 服务日志文件的路径，`health` 工具读它算可用率和耗时。`start.sh` 启动时会把实际路径传进来，正常不用配 | 路径（默认 `logs/cn-stock-mcp.log`） |
| `LOG_RETENTION_DAYS` | 归档日志保留几天。每次启动会把上一轮日志存成一份归档，超过这个天数的清掉。`health` 默认把归档一起统计，所以这个值决定它最多能回看多久 | 天数，`0` 表示不留归档（默认 `3`） |
| `TRADING_CALENDAR_PROVIDERS` | 判「今天开不开市」的日历来源：<br>`sina` 上交所公布的交易日名单，最权威<br>`holiday_cn` [NateScarlet/holiday-cn](https://github.com/NateScarlet/holiday-cn) 的国务院放假安排换算而来，与交易所名单的差异只在个别调休日<br>`weekday` 兜底，周一到周五算交易日——长假会被整段算成交易日，所以放最后 | `sina`<br>`holiday_cn`<br>`weekday`<br>`off`<br>（默认 `sina,holiday_cn,weekday`） |
| `FUND_FLOW_PROVIDERS` | 个股/指数资金流的来源顺序，前一级满足需求就停：<br>`eastmoney` 给全部历史<br>`eastmoney_delay` 只回当日一行，但主源拒绝当前出口时它还通<br>`fund_flow_page` 浏览器加载资金流向页面，不花积分，代价是一次页面加载；从链里去掉就不走页面<br>`eastmoney_gateway` 同一个接口走付费网关，放在页面之后才不花冤枉钱 | `eastmoney`<br>`eastmoney_delay`<br>`fund_flow_page`<br>`eastmoney_gateway`<br>`off`<br>（默认 `eastmoney,eastmoney_delay,fund_flow_page`） |
| `REALTIME_FUND_FLOW_PROVIDERS` | 没有资金流向页面的标的（科创 50 等）盘中实时资金流的来源，给当日累计的五档净流入；有页面的标的不走这里 | `eastmoney_delay`<br>`off`<br>（默认 `eastmoney_delay`） |
| `SECTOR_FUND_FLOW_PROVIDERS` | 板块资金流的取数顺序：<br>`eastmoney` 字段全<br>`eastmoney_dataapi` 只有主力净额，但主源连不上时它还通；报告备注里会标出是降级源 | `eastmoney`<br>`eastmoney_dataapi`<br>`off`<br>（默认 `eastmoney,eastmoney_dataapi`） |
| `MARKET_MAP_PROVIDERS` | 市场云图的来源顺序：<br>`eastmoney` 主集群<br>`eastmoney_delay` 同口径备用集群 | `eastmoney`<br>`eastmoney_delay`<br>`off`<br>（默认 `eastmoney,eastmoney_delay`） |
| `MARKET_MAP_BUDGET_SECONDS` | 一次市场云图取数的总预算；用尽时返回已取到的页并标注缺页 | 秒（默认 `30`） |
| `SECTOR_TAXONOMY_PROVIDERS` | 板块分级表的来源，用来只排同一层——东财的行业板块名单把各级混在一起，不分级会让父子板块同时上榜、同一笔钱数两遍。默认排申万二级，和东财官网那张榜一致：<br>`shenwan`、`swsresearch` 是同一套申万分类的两个来源，一个不通时另一个补上，缺的级也会互补<br>`off` 退回全部板块一起排，报告里会标出来 | `shenwan,swsresearch`<br>`shenwan`<br>`off`<br>（默认 `shenwan,swsresearch`） |
| `KLINE_PROVIDERS` | 东财那一级取不到时，**个股/ETF** 的兜底顺序，逗号分隔按序尝试，`off` 关闭整层：<br>`tonghuashun` 不覆盖北交所<br>`tencent` 个股/ETF/指数都覆盖，北交所大半不认<br>`sina` 覆盖腾讯不认的北交所代码，但不认 ETF 和创业板指<br>三家各补各的洞。新浪不认 ETF，所以同花顺排第三也不能省 | `tonghuashun`<br>`tencent`<br>`sina`<br>`off`<br>（默认 `tencent,sina,tonghuashun`） |
| `KLINE_PROVIDERS_INDEX` | **指数**用的兜底顺序，和 `KLINE_PROVIDERS` 分开配：腾讯/新浪的指数成交量比东财/同花顺低约 3.5%，这个量级不能忽略，所以指数按准确度排而不是按稳定性 | 同上（默认 `tonghuashun,tencent,sina`） |
| `KLINE_TONGHUASHUN_BUDGET_SECONDS` | 同花顺取一次 K 线的**总**预算，用尽即判该源失败、链路回退。它按年份取文件，跨 N 年就是 N 个请求，没有这一项时最坏耗时随窗口线性增长、没有上界。取值有两个下界，取大的那个：本环境**成功**取数的最大耗时，以及一次取数要发的请求数 × 单次超时。低于任何一个都会砍掉本来能拿到的结果、让指数成交量退到腾讯口径（低约 3.5%）；用 `probe_tuning.py tonghuashun` 量，它会把两个都算进去 | 秒，`0` 关闭（默认 `45`） |
| `KLINE_MAX_GAP_TRADING_DAYS` | 相邻两根 K 线之间允许缺多少个**交易日**，超过就判该源失败、让链路回退。防的是「序列断裂」——列是齐的、数值也在合理区间，源「成功」返回，但涨跌幅会跨缺口计算、均线全错。单位是交易日而非自然日，所以长假在结构上就是 0，阈值只用来容忍停牌（10 是 2018 年后重大资产重组停牌的上限）。是偏好不是硬条件：每个源都带同样缺口时（真实长期停牌）会宽松再问一轮并放行，不会让 K 线整段缺失 | 交易日，`0` 关闭（默认 `10`） |

## 资金流页面兜底

东财资金流接口不可用时，改用浏览器加载东财的资金流向页面取同一份数据，也就是
`FUND_FLOW_PROVIDERS` 里的 `fund_flow_page` 那一级，默认开启。这条路不花网关积分，
但每次要付一个页面加载，所以下面每一项都是在给它设上界——它不该在压力下变成常态。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `FUND_FLOW_PAGE_ENABLED` | 页面兜底的总开关：置 `0` 时，即使 `FUND_FLOW_PROVIDERS` 里有 `fund_flow_page` 也不走页面 | `0`<br>`1`<br>（默认 `1`） |
| `FUND_FLOW_PAGE_CONCURRENCY` | 同时进行的兜底页面加载数。要和 `BROWSER_MAX_PAGES` 一起调，只提一个另一个就成了新瓶颈 | 正整数（默认 `3`） |
| `FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS` | 单个标的等一个名额的上限，等不到就跳过兜底。必须大于一次页面加载的耗时，否则一批里的最后一个标的结构上永远排不到；用 `scripts/probe_tuning.py` 量当前环境的分布 | 秒（默认 `8`） |
| `FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS` | 一次请求里所有标的等名额的总时长上限，必须大于上一项 | 秒，`0` 关闭（默认 `15`） |
| `FUND_FLOW_PAGE_TABLE_WAIT_SECONDS` | 等历史表渲染完成的上限 | 秒（默认 `15`） |
| `FUND_FLOW_PAGE_REUSE_SECONDS` | 同一标的页面解析结果的复用窗口，避免一次请求内重复加载同一页面 | 秒，`0` 关闭复用（默认 `30`） |
| `FUND_FLOW_PAGE_MAX_LOADS` | 单次请求允许的页面加载次数，只在没拿到数据时才会用掉。被拒直接换 tab，不 reload；别调大，被拒后每多开一个 tab 都消耗同一出口的频率额度，会把偶发的拒绝放大成整批滑块，合适的值用 `scripts/probe_tuning.py` 量 | 正整数（默认 `2`） |
| `FUND_FLOW_PAGE_RETRY_DELAY_MS` | 重试刷新之前的随机等待区间，只作用在重试路径上 | `下界,上界` 毫秒<br>单个数字为固定值<br>`0` 关闭<br>（默认 `250,350`） |
| `FUND_FLOW_PAGE_OPEN_AFTER_FAILURES` | 实时页面加载多少次被拒后暂停实时路径（历史兜底不熔断，它是付费网关前的最后一级免费途径） | 正整数（默认 `4`） |
| `FUND_FLOW_PAGE_FAILURE_WINDOW_SECONDS` | 上一项按这个滑动窗口计数 | 秒，`0` 退回连续计数（默认 `60`） |
| `FUND_FLOW_PAGE_COOLDOWN_SECONDS` | 实时路径的暂停时长 | 秒（默认 `60`） |
| `FUND_FLOW_EMPTY_PROBE_SECONDS` | 日期对齐门的空探测有效期：探到目标日的行在上游不存在后，这段时间内同一（标的, 目标日）不再重复付页面/网关。按落地窗口的最大值量（probe_tuning 的 fund-flow-landing 项），量不到样本时宁小勿大 | 秒（默认 `3600`） |

## 上游源熔断

某个来源连续失败时直接跳过它，不必每次请求都把整条备用链走完。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `SOURCE_BREAKER_ENABLED` | 是否启用熔断 | `0`<br>`1`<br>（默认 `1`） |
| `SOURCE_BREAKER_OPEN_AFTER_FAILURES` | 连续失败多少次后跳过该源 | 正整数（默认 `3`） |
| `SOURCE_BREAKER_COOLDOWN_SECONDS` | 冷却时长，结束后放行一次探测请求 | 秒（默认 `120`） |

## 并发与线程池

取数用一个有界线程池执行。小内存机器建议保持默认值，调高会增加上游压力，并不保证降低延迟。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `FETCH_MAX_WORKERS` | 同时执行同步数据任务的线程数 | 正整数（默认 `8`） |
| `FETCH_MAX_IN_FLIGHT` | 已运行和已提交任务的总上限，超出后请求以协程等待 | 正整数（默认 `16`） |
| `BATCH_CONCURRENCY` | `brief/medium/full` 共享的活跃批次数上限 | 正整数（默认 `2`） |
| `FINANCE_CACHE_TTL_SECONDS` | 财务摘要的缓存时间。财务数据只在定期报告发布后变动 | 秒，`0` 关闭（默认 `21600`） |
| `FINANCE_CACHE_MAX_ENTRIES` | 财务缓存的最大标的数，超出后淘汰最早项 | 正整数（默认 `512`） |

## 市场纪元边界

交易所的时刻表是死的（09:30 开盘、15:00 收盘），但上游不在这些时刻定稿：盘前已经开始
更新当日数据，盘后还要整理一会儿。下面几项就是调这个提前量和延后量的，越界会夹回合法
区间并告警。它们同时决定报告缓存按什么时段划分、以及盘中资金流从哪里取。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `MARKET_EPOCH_WARMUP_TIME` | 从这一刻起按盘中对待。晚于开盘会把真在交易的一段判成闭市 | 四位 HHMM，夹在 `0700`–`0930`（默认 `0915`） |
| `MARKET_EPOCH_SETTLE_TIME` | 当日数据从这一刻起算定稿、报告可完全复用。早于收盘会把仍在变动的连续竞价折进来 | 四位 HHMM，不早于 `1500`、不晚于 `MARKET_EPOCH_FINAL_TIME`（默认 `1530`） |
| `MARKET_EPOCH_FINAL_TIME` | 资金流从抓页面切回读接口的时刻，同时是纪元边界 | 四位 HHMM，夹在 `1500`–`2300`（默认 `1600`） |
| `MARKET_EPOCH_BUFFER_MINUTES` | 午休、傍晚这些边界后留给上游整理的缓冲 | 分钟，`0`–`60`（默认 `5`） |

## 缓存

缓存唯一的正当理由是"这段时间里这份数据不会变"：非交易日数据冻结，一个纪元可以一直
命中；盘中数据在变，只用短 TTL 合并重复请求。命中与否不改变返回内容。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `CACHE_ENABLED` | 总开关。关掉后所有命名空间既不读也不写，可用于冷热对照压测 | `0`<br>`1`<br>（默认 `1`） |
| `CACHE_INTRADAY_TTL_SECONDS` | 盘中软过期秒数的默认值，`0` 表示盘中绝不复用。盘中数值持续变动，这个 TTL 只用于合并突发重复请求 | 秒（默认 `30`） |
| `CACHE_STALE_ON_ERROR` | 软过期后刷新失败，是否继续用旧值。用了一定会在输出里标注；跨纪元的旧值永远不给 | `0`<br>`1`<br>（默认 `1`） |
| `CACHE_DISK_ENABLED` | 跨重启保留闭市纪元的条目。傍晚纪元长达 16 小时，周末达 64 小时 | `0`<br>`1`<br>（默认 `1`） |
| `CACHE_DIR` | 缓存文件目录，相对项目根目录。每个命名空间一个子目录 | 路径（默认 `.runtime/cache`） |
| `CACHE_<命名空间>_MAX_ENTRIES`<br>`CACHE_<命名空间>_TTL_SECONDS` | 单个命名空间的覆盖，命名空间有 `report`、`market_events`、`sector_flow`、`market_breadth`、`finance`、`calendar`、`taxonomy`。例：`CACHE_REPORT_MAX_ENTRIES=512` | 正整数 / 秒 |
| `CACHE_<命名空间>_ENABLED` | 单独关掉某一层缓存，缺省跟随 `CACHE_ENABLED`。用于 A/B——只有总开关时无法把收益归因到某一层 | `0`/`1`（默认跟随总开关） |
| `CACHE_FUND_FLOW_MAX_ROWS` | 资金流历史一条最多缓存多少行。主源给全部历史（老标的数千行），截断可控内存；请求要的行数超过存下来的会判未命中、照常打上游，所以不会让数据变少 | 正整数（默认 `250`） |
| `CONF_DIR` | 参考数据目录（指数名单、代码表、板块表）。默认随包发布，正常不用配；指到别处可临时替换而不重装，只放要改的那个文件即可，其余仍从包内读 | 路径（默认包内 `finmcp/confs`） |

盘中命中返回的必然是一份稍旧的快照，TTL 决定这份快照能有多旧。对资金流精度要求高时设为 `0`。
纪元划分、TTL 取值依据和实测数据见[报告缓存](technical-details.md#10-报告缓存)。

## 市场宽度（同花顺）

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `MARKET_BREADTH_AUTH_FILE` | 同花顺认证缓存文件 | 路径（默认 `.runtime/tonghuashun-auth.json`） |
| `MARKET_BREADTH_COOLDOWN_SECONDS` | 同花顺认证失败后的冷却时间，冷却期内 `market_breadth` 直接用备用源 | 秒（默认 `300`） |

## 浏览器与虚拟显示

资金流兜底和 `market_breadth` 共用同一个浏览器实例，下面几项对两者同时生效。

| 配置名 | 作用 | 可选参数 |
| --- | --- | --- |
| `BROWSER_MAX_PAGES` | 整个浏览器同时开着的页面数上限。这同时就是同时有几个渲染进程，是峰值内存的直接决定项——每多一个并发页就多一个渲染进程 | 正整数（默认 `3`） |
| `BROWSER_IDLE_TIMEOUT_SECONDS` | **盘中**多久没人调用就关掉浏览器。默认 90 分钟盖住午休，关早了下一批调用要重新等冷启动 | 秒，`0` 整层关闭回收（总开关，不看时段）（默认 `5400`） |
| `BROWSER_IDLE_TIMEOUT_CLOSED_SECONDS` | **盘外**（收盘后、非交易日）的空闲回收超时。盘外没有午休要盖，而浏览器进程树是常驻内存的大头，拆掉能省下大部分 | 秒，`0` 盘外不回收、盘中照旧（默认 `300`） |
| `BROWSER_DISGUISE` | 把无头浏览器的自报特征改成普通浏览器的样子。哪种身份被拒得少取决于出口 IP，两个方向都出现过，所以默认关、由实测决定：用 `scripts/probe_tuning.py` 量当前环境，置 1 启用伪装 | `0`<br>`1`<br>（默认 `0`） |
| `BROWSER_CLAIM_PLATFORM` | 对外声明哪个平台。`auto` 下 Windows/macOS 照实报，Linux 报 macOS | `auto`<br>`real`<br>`macos`<br>`windows`<br>（默认 `auto`） |
| `BROWSER_NO_SANDBOX` | 为 Chromium 添加 `--no-sandbox`。会降低隔离，仅在 sandbox 确实不可用时启用 | `0`<br>`1`<br>（默认 `0`） |
| `XVFB_DISPLAY_NUMBER` | 无 `DISPLAY` 时 `start.sh` 使用的 Xvfb 起始显示号，被占用则依次往后试到 109 | 整数（默认 `99`） |
| `XVFB_SCREEN` | Xvfb 屏幕配置 | `宽x高x色深`（默认 `1920x1080x24`） |
| `BROWSER_HEADFUL` | 调试开关：用有头浏览器加载，便于人工观察 | `0`<br>`1`<br>（默认 `0`） |
| `BROWSER_KEEP_PAGES` | 调试开关：抓完不关页面。每个页面是一个独立渲染进程，会显著抬高内存 | `0`<br>`1`<br>（默认 `0`） |
