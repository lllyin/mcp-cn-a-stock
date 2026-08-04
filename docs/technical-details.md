# CnStock 技术实现说明

本文面向维护者和部署人员，说明 CnStock 的内部架构、数据获取策略、并发模型、输出契约
和性能调优边界。安装及日常使用请先阅读[项目 README](../README.md)。

## 1. 运行结构

服务基于 FastMCP，默认以无状态 Streamable HTTP 运行：

```text
MCP client
    |
    v
FastMCP tools (qtf_mcp/mcp_app.py)
    |
    +-- report/indicator assembly (qtf_mcp/research.py)
    |
    +-- compatibility data layer (qtf_mcp/datafeed.py)
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
| `qtf_mcp/mcp_app.py` | MCP tool、参数和 Pydantic 输出模型 |
| `qtf_mcp/research.py` | 报告组装及技术指标计算 |
| `qtf_mcp/datafeed.py` | 将统一数据对象转换为旧研究层字典格式 |
| `qtf_mcp/datasource/base.py` | `DataSource`、`StockData`、`FetchRequirements` |
| `qtf_mcp/datasource/cn_stock_source.py` | AkShare/efinance 数据源实现和执行器 |
| `qtf_mcp/datasource/realtime_ff.py` | 交易时段实时资金流浏览器路径 |
| `qtf_mcp/datasource/market_breadth.py` | 全市场涨跌分布、缓存和回退 |

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
| `CN_STOCK_DATA_FETCH_MAX_WORKERS` | 8 | 同时执行同步数据任务的线程数 |
| `CN_STOCK_DATA_FETCH_MAX_IN_FLIGHT` | 16 | 已运行和已提交任务的总上限 |

Python 的 `ThreadPoolExecutor` 内部队列没有业务级上限，因此服务在提交前使用事件循环所属的
`asyncio.Semaphore` 做 admission control。达到上限后的请求以轻量协程等待，不会继续向线程池
堆积任务和参数对象。

客户端取消或超时时，已经运行的同步网络调用无法被 Python 安全中断。此时 permit 会一直保留到
底层 future 真正结束，防止调用方通过反复超时绕过并发上限。

每个同步任务在 DEBUG 日志中记录：

- `admission`：等待进入有界执行器的时间。
- `queue`：提交后在线程池队列等待的时间。
- `service`：同步函数实际执行时间。

这些指标用于区分服务端排队与上游接口耗时，不会进入 MCP 响应。

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

## 6. AkShare Proxy Patch

代理补丁在 `CNStockDataSource` 模块加载时安装，只 hook 指定的东财域名。支持的新变量名为：

```env
AKSHARE_PROXY_GATEWAY=...
AKSHARE_PROXY_TOKEN=...
AKSHARE_PROXY_RETRY=30
```

旧变量 `AKSHARE_PROXY_IP`、`AKSHARE_PROXY_PASSWORD`、`AKSHARE_PROXY_PORT` 继续兼容。
`AKSHARE_PROXY_PORT` 实际表示重试次数，这是历史命名问题。

一次 MCP 调用可能产生多个命中代理的 HTTP 请求，失败重试还会放大请求数。因此真实并发
benchmark 应以代理尝试数设置预算，不能用 MCP 调用次数估算积分消耗。高样本并发测试应优先使用
冻结 fixture 或延迟回放，只用少量真实调用校准网络方向。

降低重试次数可能改变最终成功率和错误内容，属于可靠性策略变更，不应混入纯性能优化。

## 7. 市场宽度数据

`market_breadth` 与股票报告使用独立的数据获取链路：

1. 优先使用同花顺数据。
2. 复用持久化认证信息，必要时通过 Playwright 刷新。
3. 认证失败或处于冷却期时回退到 efinance。
4. 响应通过 `source` 和 `warnings` 暴露实际数据源及降级情况。

相关配置：

| 变量 | 用途 |
| --- | --- |
| `CN_STOCK_TONGHUASHUN_AUTH_FILE` | 覆盖认证缓存文件路径 |
| `CN_STOCK_TONGHUASHUN_COOLDOWN_SECONDS` | 认证失败后的冷却时间，默认 300 秒 |
| `CN_STOCK_CHROME_NO_SANDBOX` | 为 Chromium 增加 no-sandbox 参数 |
| `CN_STOCK_XVFB_DISPLAY_NUMBER` | `start.sh` 使用的 Xvfb 显示号，默认 99 |
| `CN_STOCK_XVFB_SCREEN` | Xvfb 屏幕配置，默认 `1920x1080x24` |

`CN_STOCK_CHROME_NO_SANDBOX` 会降低浏览器隔离，仅应在受控容器且 Chromium sandbox 确实不可用
时启用。

市场宽度结果带有短 TTL 缓存，并通过锁合并并发 cache miss，避免多个请求同时刷新同一份全市场
数据。调用方仍应检查 `trade_date` 和 `market_time`，尤其是在收盘后、周末和回退场景。

## 8. 输出与错误契约

报告类工具返回 Pydantic 模型：

- `reports` 保存成功结果。
- `errors` 保存单标的失败，不因一个标的失败而丢弃整个批次。
- `warnings` 保存截断、回退或部分数据提示。
- `symbols_count` 表示应用批量上限后的请求数量，不等同于成功数量。
- `timestamp` 是报告生成时间，因此不适合作为跨时间逐字节比较字段。

`tech` 的数值字段只允许 JSON number 或 `null`，不会输出 `NaN`/`Infinity`。指标日期按最近交易日
优先排列。`brief`、`medium`、`full` 则将 Markdown 文本放入结构化批量响应中。

## 9. 性能验证原则

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

## 10. 测试边界

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
- 批量技术报告是否保持输入顺序。
- 运行时版本是否与项目元数据一致并写入启动日志。

涉及真实数据源的测试会受网络、交易时段和代理积分影响，不应作为高频单元测试。单元测试应在
同步数据函数边界使用 fixture 或 monkeypatch，真实 MCP 调用用于发布前的小样本集成验证。
