# 缓存层设计

> 状态：**待落地。** 这份文档是实现依据，改代码之前先读它；实现和文档不一致时，
> 要么改代码，要么回来改文档，不要让两边各说各的。
>
> 相关：[项目架构](architecture.md)、[技术实现说明 §10 报告缓存](technical-details.md#10-报告缓存)。

## 一、判据只有一条

**缓存唯一的正当理由是"这段时间里这份数据不会变"。**

防风控、省积分、变快、上游挂了还能出数——这些都是这条成立之后自然得到的**好处**，
不是决定缓不缓的**判据**。把好处当判据，就会去缓那些其实在变的数据，用"看起来有数据"
换掉"数据是对的"，正好撞上 AGENTS §一。

每接一处缓存只问两个问题：

1. **这份数据什么时候变？** → 决定失效口径
2. **变之前有没有人会重复要它？** → 决定值不值得缓

第二问同样能否掉一处缓存：数据一年才变一次，但一辈子只被读一次，缓了也没用。

这条判据还有一个推论，第 2.5 节会用到：**判断"变没变"，看数据本身比看时钟准。**
时钟只是数据行为的近似，近似会错。

## 二、市场纪元：失效口径的地基

### 2.1 只有两种变化节奏

**跟着市场走**——行情、资金流、市场宽度、板块排行。盘中一直在变，收盘之后冻住，
直到下一次开盘。用**市场纪元**表达。

**按时长走**——交易日历、行业分级、财务报表。和交易时段无关，按自己的节奏更新
（一年一两次、季报一次）。用固定 TTL。

钉了过去日期的历史查询（`market_events date=2026-08-20`）不是第三种，是第一种的
退化：**纪元恒定**，所以永不失效。

### 2.2 边界要可配，因为上游不在交易所的时刻定稿

交易所的时刻表是事实：**09:30 开盘、11:30 午休、13:00 续盘、15:00 收盘**。
但**上游数据源不在这些时刻定稿**：

- 盘前：09:30 还没开盘，上游已经开始更新当日数据，所以纪元要提前进入"盘中"。
- 盘后：15:00 收了盘，东财的资金流页面还在整理，AkShare 的当日资金流行更晚才落地。
  这段时间数据仍在动，不能当成定稿。

提前多少、延后多少取决于上游当时的行为，**会变**，所以必须能配。

```
        [warmup]      开盘                午休          续盘          收盘   [settle]      [final]
          09:15      09:30              11:30         13:00         15:00    15:30         16:00
 ─ CLOSED ──┼───── LIVE ─────────────────┼─buf─ LUNCH ──┼──── LIVE ────┼─ POSTCLOSE ─┼─LIVE─┼─buf─ CLOSED ─
            └── 可配 ────────────────────────────────────────────────── 可配 ────────┴─ 可配
```

| 边界 | 回答的问题 | 默认 |
|---|---|---|
| `warmup` | 上游从几点开始更新当日数据 | 09:15 |
| `settle` | 当日数据几点算定稿、可以完全复用 | 15:30 |
| `final` | 资金流几点从"抓页面"切回"读接口" | **16:00** |

午休和续盘不给配：它们是交易所事实，上游在这两处的滞后由统一的 `buffer`
（默认 5 分钟）覆盖，不值得各开一个配置项。

### 2.3 这三个边界是全项目的单一事实源

现在有一处**隐蔽的重复**：`research.is_realtime_fund_flow_window()` 写的是

```python
(now.hour == 9 and now.minute >= 15) or (10 <= now.hour <= 16)   # 即 09:15 ≤ t < 17:00
```

这和 `cache.PRE_OPEN=09:15` / `cache.BRANCH_FLIP=17:00` 是**同两个边界的第二种写法**。
改一处、另一处不动，纪元就会跨越"报告从抓页面版切成读接口版"的那一刻——同一个纪元里
存在两种形状的报告，而"命中与否不改变返回内容"正是报告缓存赖以成立的前提。

所以边界提成单一事实源，两边都读它：

```python
# finmcp/market_session.py（新文件）
WARMUP_TIME: datetime.time      # 09:15
SETTLE_TIME: datetime.time      # 15:30
FINAL_TIME:  datetime.time      # 16:00
BUFFER:      datetime.timedelta # 5min

def phase_and_epoch(now=None) -> tuple[str, str]: ...   # cache.market_phase 迁到这里
def is_realtime_fund_flow_window(now=None) -> bool:     # research 里那个迁到这里
    return WARMUP_TIME <= now.time() < FINAL_TIME
```

**`final` 必须同时是纪元边界和分支翻转点，不能拆成两个配置。** 拆开就会让一个纪元
横跨翻转点，正是上面那个 bug。

### 2.4 边界校验

配错了不能让服务起不来，也不能默默接受一个会算错的值。越界夹回合法区间 + 打 WARNING，
和现在 `_clamp_settle` 一致：

| 边界 | 约束 | 越界的后果（所以要拦） |
|---|---|---|
| `warmup` | `07:00 ≤ warmup ≤ 09:30` | 晚于开盘，则 09:30→warmup 这段真在交易却被判成 CLOSED，会把昨天纪元的数据当成今天的发出去 |
| `settle` | `15:00 ≤ settle ≤ final` | 早于收盘，则连续竞价的一段被算进"完全复用"，而那时 `today_volume_est_ratio` 还在动 |
| `final` | `settle ≤ final ≤ 23:00` | 早于 settle 会让两个纪元次序颠倒 |
| `buffer` | `0 ≤ buffer ≤ 60min` | — |

### 2.5 `final=16:00` 需要一道守卫

把 `final` 从 17:00 提到 16:00，有一处已知风险，必须连守卫一起做，否则是数据倒退。

`cache.py` 现有注释记着：**AkShare 的当日资金流行"在 17:00 之后几分钟才落地"**，
17:00 这个值和其后 5 分钟的缓冲就是为它留的。`final=16:00` 意味着 16:00 就切到读接口，
而 16:05 之后进入 CLOSED 纪元——那个纪元长达 17 小时。如果当日资金流那时还没落地，
**一份缺当日资金流的报告会被冻结整晚**。

（措辞不会错——`## 资金流向（YYYY-MM-DD）` 会如实标成上一个交易日。但"缺一段"被
冻 17 小时，仍然是 AGENTS §一 要拦的那类问题。）

守卫按第一节那条推论来做：**别猜时钟，看数据。**

> 一份报告只有在**当日资金流已经落地**时，才允许写进 CLOSED 纪元；没落地就留在
> POSTCLOSE 的短 TTL 里，等落地了自然进。

判据是现成的 `research.has_today_fund_flow_from_api(data)`，落点也是现成的——
和 `is_cacheable_report` 同一个位置，多一个条件而已：

```python
def is_cacheable(text: str, *, phase: str, fund_flow_settled: bool) -> bool:
    if not text.strip() or any(m in text for m in TRANSIENT_MARKERS):
        return False
    if phase == PHASE_CLOSED and not fund_flow_settled:
        return False      # 当日资金流还没落地，别把这一版冻进 17 小时的纪元
    return True
```

这样 `final` 配早了最多让缓存少命中几次，不会让报告少一段数据。**配错的代价从
"数据缺失"降到"性能损失"**，这正是这个守卫的意义。

配到多早才合适，等交易日实测：观察当日资金流行实际几点落地，再定 `final`。
在那之前 16:00 是安全的——有守卫兜着。

## 三、逐维盘点

| 数据 | 什么时候变 | 会被重复要吗 | 结论 |
|---|---|---|---|
| `market_events` 事件池 | 钉过去日期：**永不变**；钉当天：盘后发布 | 会 | ✅ **收益最大**，12–16s → 亚秒 |
| 已渲染报告 | 跟市场 | 会 | ✅ 已有，收编 |
| 板块资金流 | 跟市场 | 会 | ✅ 已有，收编 |
| 全市场宽度 | 跟市场 | 会 | ⚠️ 有缓存但用的是**平 TTL 不是纪元**，非交易日仍每 300s 打一次上游 |
| 财务摘要 | 季报发布 | 会，`medium`/`full` 共用 | ✅ 已有，收编 + 落盘 |
| 交易日历 | 一年一次 | 会，四处共用 | ✅ 已有，收编 + 落盘 |
| 行业分级 | 一年一两次 | 会 | ✅ 已有，收编 + 落盘 |
| 资金流页面解析 | 盘中秒级 | 一次请求内两个用途要同一页 | ➖ 不收编，见 §5 |
| K 线 / 基本数据 / 盘中行情 | 跟市场 | 只有跨工具才有收益，无生产调用分布 | ⏸ **暂不做**，等服务器日志 |
| 股票代码表 | 上市退市 | 启动时读本地 `confs/markets.json`，不打上游 | ❌ 不需要 |

**明确不缓的**：失败结果、降级结果。一个纪元长达 64 小时，把一次瞬时降级腌进去，
就是整个周末只有两列。这条现在在报告缓存（`is_cacheable_report`）和板块资金流
（看 `partial`）各写了一遍，收编时提成命名空间的 `cacheable` 谓词。

### `market_events` 的实测

2026-09-06 连查两次，7 个源 + 回看 3 天，钉 2026-08-20：

```
15.82s / 12.45s，各 1.6 MB
两次响应除 fetched_at 外逐字节相同
```

同一份 1.6 MB 的数据每次重拉 12–16 秒，而它永远不会再变。

## 四、模块设计

### 4.1 泛化 `cache.py`，不新建子系统

`ReportCache` 已经有：市场纪元、盘中 TTL、有界 LRU、磁盘层、key 构造。板块资金流
上周直接复用它就跑通了，说明这套东西够用。缺的只有三样——命名空间、软/硬过期、单飞。

```
finmcp/
  market_session.py   新增：纪元边界 + 校验 + phase_and_epoch + 资金流窗口（单一事实源）
  cache.py            改造：ReportCache → Cache，加命名空间 / 软硬过期 / 单飞
```

### 4.2 命名空间

各有各的额度，互不挤占——`market_events` 一条 1.6 MB，和个股报告混在同一份 512 条
额度里会把报告条目全挤掉。

```python
@dataclass(frozen=True)
class Namespace:
    name: str
    max_entries: int
    epoch_bound: bool = True          # True=跟市场纪元；False=纯 TTL
    ttl_seconds: float = 0.0          # 软过期；epoch_bound 时只在盘中纪元生效
    max_age_seconds: float = 0.0      # 硬过期上限，0 = 只受 epoch 约束
    disk: bool = False
    encode: Callable | None = None    # 值 → 可 JSON 化；None = 值本身可 JSON 化
    decode: Callable | None = None
    cacheable: Callable[..., bool] | None = None   # 失败/降级结果不写入
```

**内存层存活对象，磁盘层存编码后的 JSON。** 内存命中不付编解码代价；`encode`/`decode`
只在落盘和回读时调用。`decode` 失败（旧版本写下的形状对不上）当作没缓存过，
不能让一条坏条目使这次查询失败。

**磁盘布局**：`{CACHE_DIR}/{namespace}/epoch-{epoch}/{digest}.json`。清扫器按命名空间
各扫各的，且只删 `epoch-` 前缀的目录——它不能删自己没创建过的东西。

### 4.3 软过期 / 硬过期

| | 判据 | 过期之后怎么办 |
|---|---|---|
| **软过期** | `ttl_seconds` 到了 | 想刷新；**刷不到就继续用旧的**，并在输出里标注 |
| **硬过期** | 纪元变了，或超过 `max_age_seconds` | 条目作废，绝不使用 |

一个概念同时覆盖两件事：

- **上游挂了还能出数**——不用单独做一套 stale 机制，它就是软过期的自然结果。
- **跨纪元的旧值一定拦得住**——昨天 15:00 的收盘宽度，到今天 10:30 只旧了 19.5 小时，
  任何以"天"为量级的时长上限都会放行；但它跨了纪元，是硬过期，直接作废。

细则：

- `ttl_seconds` 对 `epoch_bound` 的命名空间**只在盘中纪元生效**。收盘后的纪元里数据
  已冻结，再设软过期只会白打上游。
- 显式传入的 `date-YYYY-MM-DD` 纪元不是盘中纪元，所以**没有 TTL**，只受 LRU 淘汰。
- 用了旧值必须让调用方知道，且**必须出现在输出里**——悄悄返回旧数据比少一段数据更糟：
  少一段看得见，旧一天看不见。

```python
entry = cache.get_or_load("finance", key, loader)
if not entry.fresh:
    warnings.append(f"财务摘要来自 {entry.age_text} 前的缓存，上游当前不可用")
```

### 4.6 总开关不管 TTL 型命名空间

`CACHE_ENABLED=0` 只关**跟市场走**的那些（报告、板块资金流、事件池、市场宽度）。
交易日历和行业分类不受它影响，两条理由：

- **关掉它不改变任何输出。** 同一份名单，读缓存和重新取得到的完全一样，
  对等价性证明没有任何贡献。它们不是"某次查询的结果"，是加载一次的参考数据。
- **关掉它的代价是灾难性的。** 交易日历决定市场纪元，而纪元每算一次 phase 就要用
  一次——实测 `CACHE_ENABLED=0` 时三次 `is_trading_day` 打了三次上游、0.51 秒。
  生产里等于每个请求都多付几次网络往返。

想强制重取用 `clear()`，那是"重置"该做的事，不是总开关。

### 4.4 取用 API

```python
@dataclass(frozen=True)
class Entry:
    value: Any
    fresh: bool          # False = 软过期后刷新失败，用的是旧值
    age_seconds: float
    age_text: str        # "12 分钟"，给报告措辞用

def get_or_load(ns: str, key: str, loader: Callable[[], Any], *,
                epoch: str | None = None) -> Entry | None: ...

async def aget_or_load(ns: str, key: str, loader: Callable[[], Awaitable[Any]], *,
                       epoch: str | None = None) -> Entry | None: ...
```

- **返回 `None`** 表示：loader 没给出结果，也没有可用的旧值。调用方按"这次没取到"处理。
- **`epoch` 显式传入**用于钉过去日期的查询：传 `date-2026-08-20`，这条永不跨纪元失效。
  不传就用当前市场纪元。`epoch` 参与 key 的摘要，所以不同纪元天然是不同条目。
- **`key`** 由调用方拼，只放"影响上游请求的参数"。渲染参数（`top`/`level`/`keywords`）
  一律不进 key——板块资金流那次的教训：进了就是 900 个 key，不进只有 9 个。

**两个入口，因为项目里同步异步都有**：分级、日历、财务、`market_events` 跑在线程里，
报告和市场宽度跑在事件循环上。同步版用 `threading.Lock` 做单飞，异步版用
`asyncio.Task`（就是 `realtime_ff._page_inflight` 那份，含 `asyncio.shield`——
一个等待者被取消不能中断别人需要的加载）。**两者共用同一份 Store 和 Namespace 声明，
只是"等待"的原语不同**；用一个原语硬扛两边会把 `threading.Lock` 带进事件循环，
堵住整个服务。

**单飞下 loader 抛异常**：异常传给所有等待者，各自再按自己的 stale 规则回退。
不把异常写进缓存。

### 4.5 `market_events` 按 (源, 日期) 缓存

不按整次请求缓：`keywords` / `symbols` / `max_rows_per_source` 都是**本地过滤参数**，
进 key 就是无界 key 空间（自由文本）。

接缝在 `public_events._fetch_source` 的最内层上游调用：

| 源 | 上游调用 | 缓存 key |
|---|---|---|
| `limit_up` / `strong` / `previous_limit_up` / `broken_board` | `stock_zt_pool_*_em(date=D)` | `(源, D)` |
| `lhb` | `stock_lhb_detail_em(start=D, end=D)` | `(lhb, D)` |
| `announcements` | 循环 `stock_notice_report(date=D-i)`，**本来就是按天的** | `(announcements, D-i)` 逐天 |
| `earnings_forecast` | `stock_yjyg_em(date=报告期)` | `(earnings_forecast, 报告期)`，季度粒度，一年 4 个 key |

按天缓公告还有个附带好处：`lookback=3` 和 `lookback=5` 两次查询**共用条目**，
第二次只补 2 天。本地过滤（关键词、标的、截断）留在缓存之外，每次照做。

`epoch`：钉过去日期传 `date-YYYY-MM-DD`（永不失效），钉当天用当前纪元。

## 五、命名空间清单

| 命名空间 | 缓什么 | `epoch_bound` | `ttl` | `max_entries` | 落盘 |
|---|---|---|---|---|---|
| `report` | 已渲染报告 | ✅ | 盘中 30s | 512 | ✅ |
| `market_events` | 单源单日事件 | ✅（钉日期=恒定） | 盘中 30s | 64 | ✅ |
| `sector_flow` | 板块资金流 board | ✅ | 盘中 30s | 16 | ✅ |
| `market_breadth` | 全市场宽度 | ✅ | 盘中 15s | 1 | ❌ |
| `finance` | 财务摘要 | ❌ | 6h | 512 | ✅ 新增 |
| `calendar` | 交易日历 | ❌ | 24h | 1 | ✅ 新增 |
| `taxonomy` | 行业分级 | ❌ | 24h | 4 | ✅ 新增 |

`market_breadth` 只有 1 条：`get_market_breadth()` 不带参数，全市场只有一个快照。

三处新增落盘的理由一致：有效期以小时计，而进程重启是分钟级的事——不落盘等于每次
重启都重付一次上游。

`fund_flow_page`（`realtime_ff._page_cache`）**不收编**：它管的是"一次请求内两个用途
共用同一个页面"，生命周期是单次请求级，和这一层解决的问题不是一回事，收编只会
把两件事搅在一起。

## 六、配置

配置名全部重排，**不保留旧名字**（2.0 尚未上线，改名无成本）。落地时同步改
`.env.example` 和 README，`test_config_docs.py` 会强制三者一致。

### 市场纪元边界

| 配置名 | 作用 | 默认 |
|---|---|---|
| `MARKET_EPOCH_WARMUP_TIME` | 从这一刻起按盘中对待。上游在开盘前就开始更新当日数据 | `0915` |
| `MARKET_EPOCH_SETTLE_TIME` | 当日数据从这一刻起算定稿、可完全复用 | `1530` |
| `MARKET_EPOCH_FINAL_TIME` | 资金流从"抓页面"切回"读接口"的时刻，同时是纪元边界 | `1600` |
| `MARKET_EPOCH_BUFFER_MINUTES` | 午休、傍晚这些边界后的整理缓冲 | `5` |

越界夹回并打 WARNING，约束见 §2.4。这四项同时决定报告缓存的纪元划分和盘中资金流的
取数分支，改之前先读 §2.3 和 §2.5。

### 缓存

| 配置名 | 作用 | 默认 |
|---|---|---|
| `CACHE_ENABLED` | 总开关，作用于**跟市场走**的命名空间。TTL 型的（交易日历、行业分类）不受它影响，理由见 §4.6 | `1` |
| `CACHE_DISK_ENABLED` | 磁盘层开关 | `1` |
| `CACHE_DIR` | 磁盘层目录 | `.runtime/cache` |
| `CACHE_INTRADAY_TTL_SECONDS` | 盘中软过期时长的默认值，`0` = 盘中不复用 | `30` |
| `CACHE_STALE_ON_ERROR` | 软过期后刷新失败，是否继续用旧值（一定会在输出里标注） | `1` |
| `CACHE_<NS>_TTL_SECONDS` | 覆盖单个命名空间的 TTL | 见 §五 |
| `CACHE_<NS>_MAX_ENTRIES` | 覆盖单个命名空间的条数上限 | 见 §五 |

按命名空间只留两项覆盖——多了没人调，也说不清各自该配多少。

改名对照（旧 → 新）：

```
REPORT_CACHE_ENABLED              → CACHE_ENABLED
REPORT_CACHE_DISK_ENABLED         → CACHE_DISK_ENABLED
REPORT_CACHE_DIR                  → CACHE_DIR
REPORT_CACHE_INTRADAY_TTL_SECONDS → CACHE_INTRADAY_TTL_SECONDS
REPORT_CACHE_MAX_ENTRIES          → CACHE_REPORT_MAX_ENTRIES
REPORT_CACHE_SETTLE_TIME          → MARKET_EPOCH_SETTLE_TIME
FINANCE_CACHE_TTL_SECONDS         → CACHE_FINANCE_TTL_SECONDS
FINANCE_CACHE_MAX_ENTRIES         → CACHE_FINANCE_MAX_ENTRIES
TRADING_CALENDAR_TTL_SECONDS      → CACHE_CALENDAR_TTL_SECONDS
SECTOR_TAXONOMY_TTL_SECONDS       → CACHE_TAXONOMY_TTL_SECONDS
```

`FUND_FLOW_PAGE_REUSE_SECONDS` 不改：它属于 `FUND_FLOW_PAGE_*` 那一组，一起读才说得通。

## 七、必须成立的不变量

落地时逐条写成测试。它们是这套设计的正确性边界，破一条就说明实现走偏了。

| # | 不变量 | 破了会怎样 |
|---|---|---|
| 1 | 一个纪元内绝不跨越 `warmup` / `final` | 同一纪元里出现两种形状的报告 |
| 2 | 命中与否不改变返回内容（除 stale 标注） | 缓存从优化变成数据源 |
| 3 | `CACHE_ENABLED=0` 时**跟市场走**的命名空间都不读不写 | `prove_equivalence.py` 的比对是假的 |
| 4 | 失败结果、降级结果永不写入 | 一次瞬时故障被冻 64 小时 |
| 5 | 当日资金流未落地时，报告不进 CLOSED 纪元 | 缺一段的报告被冻 17 小时（§2.5） |
| 6 | 硬过期的条目在任何情况下都不被返回 | 跨交易日的旧数据被当成当天的 |
| 7 | 返回旧值时 `fresh=False`，且调用方在输出里标注 | 悄悄返回旧数据 |
| 8 | 同一 `(ns, key)` 并发只发一次上游 | 缓存踩踏，最容易触发风控的时刻 |
| 9 | 每个命名空间的条数不超过自己的上限 | 大条目挤掉小条目 |
| 10 | 越界的边界配置被夹回并告警，服务照常启动 | 配错一个值服务起不来 |

## 八、落地顺序

| 阶段 | 内容 | 验证 |
|---|---|---|
| 0 | `market_session.py`：边界配置化 + 校验 + `phase_and_epoch` + 资金流窗口迁过来。**先保持 `final=17:00`** | 与现在逐值等价；`prove_equivalence.py` 逐字比对；不变量 1、10 |
| 1 | §2.5 的守卫（当日资金流未落地不进 CLOSED） | 不变量 5；构造"资金流只到昨天"的报告，断言它不写入 CLOSED 纪元 |
| 2 | `final` 默认值改 16:00 | 交易日实测：16:00–17:00 之间报告仍带当日资金流；没带的话守卫应拦住不写缓存 |
| 3 | `cache.py` 泛化：命名空间、软/硬过期、单飞、配置改名 | `report` 行为不变；`prove_equivalence.py`；不变量 2、3、4、6、7、8、9 |
| 4 | `market_events` 接上（现在完全没有） | 全新接入，无等价性包袱；钉过去日期 12–16s → 应 <0.5s；两次响应除 `fetched_at` 外逐字节相同 |
| 5 | `finance` / `calendar` / `taxonomy` / `sector_flow` 收编，三处加落盘 | `prove_equivalence.py`；重启后应直接命中磁盘层 |
| 6 | `market_breadth` 平 TTL 换纪元 | 非交易日上游请求数应从 12 次/小时降到每纪元 1 次 |
| 7 | 命中率接进 `verify_release.py` 诊断一节 | 报告里多一节，每个命名空间一行 |

**0 → 1 → 2 的顺序不能调。** 守卫必须先于 `final=16:00` 落地，否则中间那段时间
线上会出现"缺当日资金流的报告被冻一整晚"。阶段 0 保持 17:00 是为了让它成为纯改造，
能用 `prove_equivalence.py` 证明；把改默认值单拎成阶段 2，是为了让"行为变了"这件事
只发生在一个提交里，出问题好回退。

先做 `market_events` 而不是先收编老的几处：它完全没有缓存，没有等价性包袱，收益又最大，
正好用它验证泛化后的 `cache.py` 好不好用；拿主链路的报告缓存去试错要贵得多。

### 观测

每个命名空间导出 `hits / misses / stores / evictions / stale_serves / entries`，
一处汇总，接进 `verify_release.py`。现在报告缓存有 hits/misses，其余五处一个数都没有
——调不动的东西不该继续加。

## 九、这套缓存不解决什么

- **上游给错数据。** 缓存只让同一份数据少取几遍，不判断它对不对。
- **首次访问的延迟。** 冷 key 该多慢还是多慢。预热能削，但预热本身增加上游请求数，
  和"防风控"相反。
- **风控本身。** 降低频率，降不到零。真被限流要靠熔断、伪装通道和网关。
- **跨进程共享。** 磁盘层是同机持久化，不是分布式缓存。多实例部署各缓各的。
