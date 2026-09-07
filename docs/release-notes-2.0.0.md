# 2.0.0 变更说明与上线清单

给两类人看：**接报告的下游**看第一、二节，改完正则和配置就够了；**负责部署的人**看第三、四节。
数字全部来自 2026-09-06 部署机（Ubuntu 2 核 4G）和本机的验证记录，口径写在每张表下面。

## 一、下游必须改的

### 1. 报告文本：两族标签变了，共 10 行

只有这两处，都出自同一次修正（`今日` 配着前一交易日的数据，读的人无从知道是哪天）。

| 位置 | 改前 | 现在 |
| --- | --- | --- |
| 资金流向五行（主力 / 超大单 / 大单 / 中单 / 小单） | `- 今日主力净流入: 1.22亿  主力净占比: 7.79%` | `- 当日主力净流入: 1.22亿  主力净占比: 7.79%` |
| 换手率五行（5 / 20 / 60 / 120 / 240 日） | `- 5日总换手 (含今日): 1.31%` | `- 5日总换手 (含当日): 1.31%` |

盘中和盘后两条渲染路径都改了。指数那一行 `- 沪深两市主力净流入:` **没有变**，所以形如
`^- (今日|沪深两市)(主力|超大单|大单|中单|小单)净流入:` 的正则会退化成只匹配指数、匹配不上个股，
这种部分失败比整段失败更难发现。把正则里的 `今日` 改成 `当日` 即可，两处都是。

`## 资金流向` 标题中途加过日期又去掉了，现在和改前逐字相同。资金流的真实日期改由 JSON 外壳的
`warnings` 给出，不再进标题。

### 2. 新增两个工具，旧工具签名不变

- `sector_fund_flow`：行业 / 概念 / 地域板块资金流排行，与东财官网同口径。
- `market_events`：指定日期的龙虎榜、涨停池、公告和业绩预告，严格 JSON，as-of 安全。

`brief` / `medium` / `full` / `tech` / `kline_daily` / `kline_range` / `market_breadth` 的参数和
JSON 外壳没有变。

### 3. 与 main 的数据差异

12 个标的逐字对比，96 行差异里 90 行是上面的改名，实质差异只有 2 处：

- `SH512480` 多拿到一整维资金流（净增益）。
- `SH600118` 市净率 10.63 / 10.64，两个源对同一天的口径抖动，已登记为已知差异。

## 二、部署方必须改的

### 1. 配置名：`CN_STOCK_` 前缀去掉，六项改了名

服务**不再读取**旧名字，也不会报错——写着旧名的 `.env` 会静默退回默认值。升级前逐项改：

| 旧名（main） | 新名（2.0.0） | 备注 |
| --- | --- | --- |
| `CN_STOCK_BATCH_QUERY_CONCURRENCY` | `BATCH_CONCURRENCY` | |
| `CN_STOCK_DATA_FETCH_MAX_WORKERS` | `FETCH_MAX_WORKERS` | |
| `CN_STOCK_DATA_FETCH_MAX_IN_FLIGHT` | `FETCH_MAX_IN_FLIGHT` | |
| `CN_STOCK_FINANCE_CACHE_TTL_SECONDS` | `FINANCE_CACHE_TTL_SECONDS` | |
| `CN_STOCK_FINANCE_CACHE_MAX_ENTRIES` | `FINANCE_CACHE_MAX_ENTRIES` | |
| `CN_STOCK_REPORT_CACHE_ENABLED` | `CACHE_ENABLED` | 现在管所有缓存命名空间，不只报告 |
| `CN_STOCK_REPORT_CACHE_DISK_ENABLED` | `CACHE_DISK_ENABLED` | |
| `CN_STOCK_REPORT_CACHE_DIR` | `CACHE_DIR` | 默认值从 `.runtime/report-cache` 改为 `.runtime/cache` |
| `CN_STOCK_REPORT_CACHE_MAX_ENTRIES` | `CACHE_REPORT_MAX_ENTRIES` | |
| `CN_STOCK_REPORT_CACHE_LIVE_TTL_SECONDS` | `CACHE_INTRADAY_TTL_SECONDS` | |
| `CN_STOCK_REPORT_CACHE_SETTLE_HHMM` | `MARKET_EPOCH_SETTLE_TIME` | 另有 `MARKET_EPOCH_WARMUP_TIME` / `MARKET_EPOCH_FINAL_TIME` / `MARKET_EPOCH_BUFFER_MINUTES` 三项新增 |
| `CN_STOCK_TONGHUASHUN_AUTH_FILE` | `MARKET_BREADTH_AUTH_FILE` | |
| `CN_STOCK_TONGHUASHUN_COOLDOWN_SECONDS` | `MARKET_BREADTH_COOLDOWN_SECONDS` | |
| `CN_STOCK_CHROME_NO_SANDBOX` | `BROWSER_NO_SANDBOX` | |
| `CN_STOCK_XVFB_DISPLAY_NUMBER` | `XVFB_DISPLAY_NUMBER` | |
| `CN_STOCK_XVFB_SCREEN` | `XVFB_SCREEN` | |
| `AKSHARE_PROXY_*` 四项 | 不变 | 第三方插件的前缀，保留 |

想和别的程序共存、担心重名，设 `ENV_PREFIX`（例如 `ENV_PREFIX=CNSTOCK_`），所有新名字统一加这个前缀。
新增的几十项配置（取数源顺序、页面兜底、熔断、浏览器）全部有默认值，零配置即可启动，见 README「配置」。

### 2. 包名 `qtf_mcp` 改为 `finmcp`

只影响 `import qtf_mcp` 的人。启动命令 `cn-stock-mcp`、`./start.sh`、`./stop.sh` 都不变。
服务装成包之后跑的是 site-packages 里的副本，切分支必须重装：用 `./switch.sh <分支>`，它会先卸干净再装。

### 3. 网关默认关闭

`AKSHARE_PROXY_ENABLED` 默认 `0`。关着时基本数据、K 线、资金流全部有非东财的兜底源，
数据仍然齐全，但资金流走浏览器页面，服务水平见第三节。

## 三、服务水平：取决于网关开不开

2.0.0 的验证有一条前提：部署机上网关是关的（`HTTP channel mode=impersonate reason=auto:proxy_disabled`），
东财直连的 impersonate 通道失败率 20% 到 30%，资金流基本全走浏览器兜底。**下面的数字是这个形态的数字。**
开网关后的表现一次都没量过，不能套用。

### 数据正确性（两种形态都成立）

| 指标 | 部署机 09-06 14:35 | 本机 09-06 16:10 |
| --- | ---: | ---: |
| 工具可用率 | 100% (22/22) | 100% (22/22) |
| 维度完整率 | 99% (527/530) | 100% (528/530) |
| 回归一致率 | 100% (29/29) | 100% (29/29) |

### 耗时与容量（网关关闭，浏览器兜底是稳态）

| 场景 | 同时在途的标的数 | 完成 | P50 | P95 | 最慢 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 并发 2（verify） | 8 | 22/22 | 约 17s | 20.7s | 24.3s |
| 并发 5 | 20 | 5/5 | 37.5s | 52.1s | 52.1s |
| 并发 10 | 40 | 15/15 | 39.1s | 102.0s | 102.0s |

- 零失败，是慢不是坏。延迟几乎全部来自浏览器页面加载（2 核机上 P50 6.4s、P95 76.5s），
  加大页面并发是反向的：更多 Chromium 抢同样两个核。
- 调用超时 120s。并发 10 的 P95 已经贴到 102s，上游再抖一点就是超时。**不开网关，请把下游并发限制在 5 以内。**
- 这组并发数字量的是一次突发（顶到在途上限就停发）。稳态只会更差，正式的容量上限用第四节的闭环压测重量。
- 这两组数字还没有包含 09-06 下午落地的 fund_flow / realtime 数据源层缓存。本机验证显示同一轮里
  浏览器加载从 44 次降到 22 次、基本数据上游请求从 57 次降到 30 次；部署机上的效果待重跑。

### 内存

| 状态 | 数值 | 口径 |
| --- | ---: | --- |
| 空闲（浏览器已回收） | 243 MiB | 只剩 Python 进程，其中约 134 MiB 是 pandas / akshare / mcp 的导入地板，90 分钟纹丝不动，无泄漏 |
| 压测峰值 | 约 840 MiB | 部署机监控折算的机器级增量；脚本按 RSS 相加报 1482 MiB 是重复计了 Chromium 的共享页 |
| 预算 | 500 MiB | AGENTS.md 第四条 |

超预算的部分全部是浏览器活跃窗口。浏览器空闲回收现在按市场时段分档：盘中 90 分钟，盘外 5 分钟。
要把峰值也压进预算，唯一的杠杆是让浏览器兜底不再是稳态，也就是开网关。

### 09-06 晚间三处取数层改动

- **K 线兜底每标的 8 个请求降到 2 个。** 腾讯日 K 不再经 AkShare 的按年循环，一页 640 根直接取。
  归一和"认不认这个代码"都照抄 AkShare，`prove_equivalence.py` 62 份可判定文档 0 差异。
  部署机上 K 线占上游耗时 79%、每请求约 1.9s，预期 K 线 task 从约 12s 降到 4s，待部署后复测。
- **科创50 的资金流有了第二条路。** `FUND_FLOW_PROVIDERS=eastmoney,eastmoney_delay`：主源拒绝时
  `push2delay` 主机给当日一行。等价性比对里唯一的差异就是 `SH000688` 从"暂无资金流向数据"
  变成有当日五行，其余文档逐字相同。
- **板块分级表有了第二个源。** `SECTOR_TAXONOMY_PROVIDERS=shenwan,swsresearch`。部署机 21:04 第一次调
  `sector_fund_flow` 就是 `ranked=未分级`：乐咕乐股对机房 IP 回 302 跳人机验证页，申万两级分类全挂，
  496 个板块只能混排、父子同榜。申万宏源研究所官网的 JSON 接口是同一套标准，一级 31/31、二级 124 个
  同名（比乐咕少 7 个小板块），乐咕缺的级由它补，乐咕给全时不发请求。**服务器 `.env` 里若写死了
  `SECTOR_TAXONOMY_PROVIDERS=shenwan`，新默认不会生效，要改成两项或删掉这一行。**

### 09-07 首个交易日的修正（收盘后部署）

- **浏览器身份回到 main 分支的原样。** 2.0.0 给资金流向页面加的伪装（`--disable-blink-features=AutomationControlled`、
  CDP 覆盖 UA 与 client hints、注入脚本、zh-CN 上下文）在部署机上适得其反：盘外两次交错 A/B，每种身份 36 次加载，
  main 原样 36/36 拿到今日块，只加那个参数 26/36，完整伪装 23/36。上午 brief 的实时资金流命中率因此从
  main 时期的 92 到 95 掉到 54。伪装改为 `BROWSER_DISGUISE=1` 才启用，默认关。
- **被拒不再 reload，直接换 tab；接口断连后给页面 6 秒自己重发；缺的那块是接口拒的就不再加载。**
  部署机数据：首加载被拒后靠新 tab 救回 7 次、reload 3 次；12 次在首次断连后 1.9 到 3.7 秒内由页面自己重发拿到。
- **实时那条路接上熔断器**（`fund_flow_browser`，与历史兜底共用阈值、窗口、冷却），只有"要今日却被拒"计失败。
- **brief 与 medium 不再为历史表去打页面**（`FetchRequirements.fund_flow_page`），只有 full 渲染历史表。
- 部署方要同时把 .env 里的 `FUND_FLOW_PAGE_MAX_LOADS` 回到 `2`、`FUND_FLOW_PAGE_COOLDOWN_SECONDS` 回到 `60`：
  09-07 上午部署机是 5 和 20，一个标的一次调用最多 10 次加载压在十几秒内，是滑块成批出现的放大器。
- **科创50 这类无页面标的盘中有实时资金流了。** 新能力 `realtime_fund_flow`，`eastmoney_delay` 取 push2delay 分钟线
  最后一行（当日累计五档净流入），净占比按当日成交额折算；`REALTIME_FUND_FLOW_PROVIDERS` 接线，默认开。
  报告形状与页面路径一致：一行标的名称加五行 `当日X净流入 … X净占比`。
- **同花顺日 K 两处修正。** 年份文件 5xx 或非 JSONP 现在抛出让链路落到腾讯，不再当"没上市"静默跳过
  （本机曾因此少了 240 日五行）；404 才是没上市。盘中当天的占位行（开高低为空）跳过，不再让整个源报错。

### 已知限制

- **科创50（`SH000688`）的历史资金流仍只有当日一行。** 它没有资金流向页面，`push2delay` 也只回最近一天，
  `full` 的历史表在主源拒绝时只有一行。
- **缓存住坏数没有自动识别。** 09-06 12:15 上游发过一批坏数，被缓存到纪元滚动，靠手工删缓存目录解决。
  同样的事再发生一次，仍然只能靠基线比对发现。
- 跨源已知差异清单见 `verification/baseline/README.md`，例如创业板指成交量东财对、腾讯和新浪同源偏低 3.5%。

## 四、上线清单（服务器上执行）

前提：本地 `feat/v2.0.0` 已推送。部署机之前跑的是 `f96d261`，缺 PSS 内存统计、空闲回收分档、
fund_flow / realtime 缓存三个提交。

```bash
cd /root/.openclaw/workspace-finance/repos/mcp-cn-a-stock
git fetch origin
./switch.sh feat/v2.0.0          # 拉取、卸旧、重装、自检包能否读到参考数据
grep -c '^CN_STOCK_' .env        # 应为 0；不为 0 按第二节改名
grep '^SECTOR_TAXONOMY_PROVIDERS' .env   # 若只有 shenwan，改成 shenwan,swsresearch 或删掉用默认
./stop.sh && ./start.sh
head -5 logs/cn-stock-mcp.log    # 看 HTTP channel mode= 这一行，确认是预期的通道
```

三项验证，缺一项不发：

```bash
export MCPORTER_CONFIG=/root/.openclaw/workspace/config/mcporter.json

# 1. 数据：可用率、维度矩阵、基线回归；Linux 上内存一节自动按 PSS 统计
python3 scripts/verify_release.py

# 2. 容量：固定并发闭环 1 / 5 / 10 批，各跑 120s。压测起隔离实例，先停主服务免得抢 CPU。
#    --memory-budget 0：浏览器兜底是稳态时并发 1 的峰值 PSS 就超预算，不关掉中止就量不出曲线；
#    内存这道闸门由上面的 verify_release 判，压测照样把 PSS 峰值写进报告
./stop.sh
.venv/bin/python scripts/loadtest_mcp.py --launch --port 8790 --closed-loop \
    --steps 1,5,10 --step-seconds 120 --memory-budget 0
./start.sh

# 3. 复核缓存去重生效：同一轮里每个标的的页面加载应只有 1 次
grep -o '资金流向页面兜底成功 \S*' logs/cn-stock-mcp.log | sort | uniq -c | sort -rn | head

# 4. 板块分级在部署机上到底有没有：调一次 sector_fund_flow 之后 ranked= 应是个数字（二级约 120），不是 未分级
grep -o 'sector_fund_flow .*ranked=[^ ]*' logs/cn-stock-mcp.log | tail -3
```

判据：

| 项 | 过线 |
| --- | --- |
| verify_release 结论 | 可用率 ≥ 99%，回归一致率 100% |
| 闭环并发 5 | P95 留出 120s 超时的余量；这一档的 P95 就是写给下游的 SLA |
| PSS 峰值 | ≤ 500 MiB，或由负责人明确放宽这条约束 |
| 浏览器加载次数 | 每标的每纪元 1 次 |

三项过了之后：先按第一节发下游通知并给缓冲期，再放流量。
