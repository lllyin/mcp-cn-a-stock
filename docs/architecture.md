# CnStock 架构

> 这份文档讲的是**底座**：有哪些层、每一层负责什么、要加东西该动哪一层。
> 单点的实现细节、取值依据和实测数据在[技术实现说明](technical-details.md)；
> 项目准则在仓库根目录的 `AGENTS.md`。
>
> 改这个项目的结构之前先读它。照着做，加一个上游只需要**写一个类 + 注册一行**、
> 加一个工具只需要动最上面那一层；如果发现要横跨好几层改，那是抽象出了问题，
> 回来改这份文档，不要绕过去。

## 一、全景

```
                    MCP client（CherryStudio / mcporter / 任意 MCP 客户端）
                              │
┌─────────────────────────────┼──────────────────────────────────────────┐
│ 工具层    mcp_app.py        │  工具定义、参数校验、批次准入、报告缓存     │
│                             │  对外契约在这里定死：谁返回 Markdown、      │
│                             │  谁返回严格 JSON                          │
└─────────────────────────────┼──────────────────────────────────────────┘
                              │  symbol 已规范化（symbols.py）
┌─────────────────────────────┼──────────────────────────────────────────┐
│ 组装层    research.py       │  指标计算 + 报告渲染。只跟"一份统一的数据"   │
│           datafeed.py       │  打交道，不知道数据是谁给的                 │
└─────────────────────────────┼──────────────────────────────────────────┘
                              │  StockData / dict
┌─────────────────────────────┼──────────────────────────────────────────┐
│ 编排层    cn_stock_source.py│  按 FetchRequirements 决定这次要哪几维、    │
│           base.py           │  并发去取、有界线程池、失败怎么算            │
└─────────────────────────────┼──────────────────────────────────────────┘
                              │  按维度分头去问
┌─────────────────────────────┼──────────────────────────────────────────┐
│ 取数层    platform.py       │  平台 → 能力 → 归一（第四节详述）           │
│           <维度>.py         │  每一维定义请求类型和归一后的契约            │
│           platforms/*.py    │  每个上游一个文件，自己负责归一              │
└─────────────────────────────┼──────────────────────────────────────────┘
                              │
                   东财 / 腾讯 / 新浪 / 同花顺 / …

横切（不属于任何一层，被多层共用）：
   http_channel.py   出站怎么发（proxy / impersonate / direct）
   cache.py          报告缓存与市场纪元
   trading_calendar  今天开不开市，四个地方都问它
   observability.py  request_id / tool / symbol 贯穿日志
```

**每一层只依赖它下面那一层的契约，不依赖实现。** 这条是全部设计的支点：
`research.py` 不知道 K 线是腾讯给的还是同花顺给的，`mcp_app.py` 不知道有没有走
浏览器兜底。换掉一个上游不该让上面三层动一行。

## 二、每一层的职责与边界

| 层 | 代表文件 | 负责 | **不**负责 |
|---|---|---|---|
| 工具层 | `mcp_app.py` | 工具签名、参数校验与纠错、批次准入、报告缓存读写、错误措辞 | 算指标、判断数据从哪来 |
| 组装层 | `research.py`、`datafeed.py` | 技术指标、报告文本、数值格式化 | 发请求、判断上游是否可用 |
| 编排层 | `cn_stock_source.py` | 按需取数、并发与线程池、跨维度的拼装 | 具体某个上游怎么请求 |
| 取数层 | `platform.py`、`platforms/` | 逐级回退、交叉合成、单位与字段归一 | 谁对谁错的仲裁 |

边界上最容易破的两处，写清楚免得下次又破：

- **归一必须发生在取数层。** 只要有一个字段带着"腾讯给的是股"这种事实往上走，
  上面每一层都得跟着认识腾讯。
- **跨能力的补全归维度，不归平台。** 平台只把**自己**的数据归一。板块的层级来自
  申万的行业分类（另一个能力、另一个上游），东财这个平台无从填它，所以由
  `sector_fund_flow.resolve()` 在拿到结果之后补上——补不到就留 `None`，
  渲染层如实说"这一批分不出层级"。
- **上游身份不能穿透到工具层。** 唯一的例外是它作为**元信息**出现——报告末尾
  写"来源：eastmoney+tencent"是可以的，因为那是给人看的，不参与逻辑。

## 三、一次 `brief` 调用怎么穿过这些层

```
brief(symbol="SZ000333")
  1  mcp_app       规范化代码（SH000333 → SZ000333）、拆批、排 BATCH_CONCURRENCY 的队
  2  mcp_app       算缓存 key（含市场纪元）→ 命中就直接返回，后面全不走
  3  cn_stock_source  按 FetchRequirements 决定这次要 K 线 + 基本数据 + 资金流
  4  各维度        kline_source / basic_info / intraday_quote 各自问自己那条平台链
  5  platform.resolve  四道闸门 → 发请求 → 校验契约 → 合成 → 返回统一结构
  6  datafeed      StockData → 研究层要的 dict
  7  research      算指标、渲染 Markdown
  8  mcp_app       写回缓存（只在"重新生成会得到同样字节"的纪元里）
```

第 2 步和第 8 步是同一件事的两头，中间那 6 步是可跳过的——这就是缓存能安全存在的
前提：**它缓存的是渲染结果，不是上游数据**，命中与否不改变返回内容。

## 四、取数层：平台 → 能力 → 归一

这是底座里最厚的一层，也是准则 AGENTS.md §二 的落地形态。

### 4.1 为什么要单独有这一层

最早每一维数据各建一个注册表，同一个上游在里面是几个互不相干的类：

```
basic_info._PROVIDERS     = {eastmoney, tencent}
intraday_quote._PROVIDERS = {fund_flow_page, tencent}
kline_source._PROVIDERS   = {tencent, sina}
```

腾讯出现三次，但"腾讯"这个平台的事实只有一份：它的主机、请求头、GBK 编码、代码
写法（`sh600519`）、成交量给的是股不是手、对北交所抛 KeyError、被限流时的表现。
这些事实被切进三个注册表，于是：

- **共享不了。** 接同花顺时，`SH000001 → hs_1A0001` 这张映射表它的 K 线能力和
  行情能力都要用，但两个注册表之间没有能放它的地方。
- **健康状态串不起来。** 腾讯要是被限流，`kline_source` 的失败计数不会告诉
  `intraday_quote` 那边也在失败。
- **接一个源要写 N 个类。** 雪球能给行情、基本数据、热度、内部交易四样，按那种
  切法就是四个类分散在四个文件里，共同的 token 获取逻辑没地方放。

所以把"平台"提成一等公民。**但有一条不跟着上提：顺序留在维度级**——同一个平台在
不同维度的优先级不一样。K 线该把同花顺排第一（创业板指只有它和东财是对的），
基本数据该把东财排第一（字段最全）。

### 4.2 三层各自负责什么

```
┌─ 平台 Platform ────────────────────────────────────────────┐
│  身份   name（配置里写的标识符）、label（中文，打日志用）        │
│  传输   host、headers、编码、JSONP 剥壳、session/token         │
│  映射   本项目的 SH600519 → 这个平台认的写法                    │
│  健康   熔断状态、已知降级判据（每个能力各自计数，平台级共享判据） │
│  能力   capabilities = {"kline", "quote", ...}               │
└────────────────────────────────────────────────────────────┘
                    ↓ 注册进
┌─ 能力注册表 ───────────────────────────────────────────────┐
│  平台 × 能力 的二维表。顺序由各维度自己的环境变量决定：          │
│      KLINE_PROVIDERS=tonghuashun,tencent,sina               │
│      BASIC_INFO_PROVIDERS=eastmoney,tencent                 │
│  一个通用的 resolve() 走完逐级回退 / 交叉合成                   │
└────────────────────────────────────────────────────────────┘
                    ↓ 返回
┌─ 归一 ─────────────────────────────────────────────────────┐
│  契约由维度定义   KlineFrame / BasicInfo / Calendar / ...      │
│  归一由平台实现   它才知道自己的单位、字段名、时区、编码          │
│  公共工具共享     kline_frame.py 这种，谁都能 import，不产生循环 │
└────────────────────────────────────────────────────────────┘
```

**归一为什么放在平台里而不是抽出来做一层"适配器"**：单位和字段名的坑是平台特有的
（腾讯的指数成交量给股、个股也给股，新浪沪市指数给手、深市指数给股），把它们集中
到一个适配器里就变成了一大堆 `if platform == ...`，正是要消灭的东西。契约放在维度
侧，是为了让"报告需要哪些字段"这件事只有一个定义处。

### 4.3 契约是强制的，不是约定

**一个能力有且只有一种归一后的结构。** 谁提供这个能力都得归一到它——这是"平台可以
随便换"的全部前提：调用方只认契约，不认是谁给的。

契约在维度模块里登记，`resolve()` 对每个平台的返回值实际校验：

```python
# 维度侧：声明归一后长什么样
pf.define_capability("trading_calendar", Calendar)
pf.define_capability("kline", lambda f: set(FALLBACK_FRAME_COLUMNS) <= set(f.columns),
                     describe="含标准列的日线表")
```

可以是类型（`isinstance` 判），也可以是校验函数——K 线返回 DataFrame，光判类型说明
不了列对不对，而**列不对正是接一个新源最容易出的错**。

**校验不过不抛异常，是当成"这个平台没给出可用结果"往下一个平台走。** 一个写坏的新
provider 应该降级到旧的，而不是把脏数据灌进报告：报告里一个形状不对的字段，比少一
个源难查得多。违规会记进 `status[f"{name}_contract_violation"]` 并打 WARNING，排查
时一眼能看到是谁的归一没做对。

没登记契约的能力不校验——迁移期间新旧能力可以共存。

### 4.4 基类

```python
# finmcp/datasource/platform.py

class Platform(abc.ABC):
    name: str = ""                       # 配置里写的标识符，小写英文
    label: str = ""                       # 中文名，只用于日志和报告
    capabilities: frozenset[str] = frozenset()

    def supports(self, capability: str, request) -> bool:
        """这个平台能不能处理这个请求。默认全能处理。

        返回 False 的请求**连发都不发**。腾讯对北交所代码抛 KeyError，靠 try/except
        发现等于每个北交所标的每次白付一个往返；声明清楚就省掉了。
        """
        return True

    def degraded(self) -> bool:
        """此刻这个平台是不是已知必败。默认不是。

        和熔断的区别：熔断是"数了 N 次失败之后才知道"，这个是"现在就知道"。
        东财的实例：伪装通道一进冷却，push2/push2his 的请求就退回原生 requests，
        而它们被接管的理由正是拒绝原生 requests——不用数也知道会失败。
        """
        return False
```

能力方法按约定命名 `fetch_<capability>`，签名 `(request) -> 契约 | None`：

```python
class TonghuashunPlatform(Platform):
    name, label = "tonghuashun", "同花顺"
    capabilities = frozenset({"kline", "quote"})

    def fetch_kline(self, request: KlineRequest) -> Optional[pd.DataFrame]: ...
    def fetch_quote(self, request: QuoteRequest) -> Optional[IntradayQuote]: ...
```

`register()` 在注册时校验：声明了的能力必须有对应方法，拼错立刻报错，而不是等到
线上少一个源才发现。

**基类不假设 HTTP。** `fund_flow_page` 走浏览器、`efinance` 是个库、交易日历的
`weekday` 兜底是纯计算——它们都是平台。HTTP 相关的东西放在可选的 `HttpPlatform`
子类里（headers、编码、JSONP、retry），需要的继承它。

### 4.5 通用 resolve

一个函数覆盖两种现存行为，靠两个钩子区分：

```python
def resolve(capability, request, *, order=None, merge=None, enough=None, status=None):
    """按配置顺序问每个平台，直到 enough() 说够了。

    merge=None      → 第一个给出非空结果的赢（K 线、行情是这种）
    merge=函数      → 把后面的结果合进前面的（基本数据是这种：A 给了名称、
                      B 给了市值，合起来才完整；已有值的不被覆盖）
    enough=None     → 拿到任何非空结果就停
    enough=函数     → 由维度决定"够不够"（基本数据要求必须有市值，
                      否则东财 snapshot 一通就返回，市值那组永远轮不到腾讯补）
    """
```

对照现存的实现：

| 维度 | merge | enough | 行为 |
|---|---|---|---|
| K 线 | 无 | 无 | 首个非空即返回 |
| 盘中行情 | 无 | 无 | 同上 |
| 交易日历 | 无 | 无 | 同上，末位是纯计算的 `weekday` 平台 |
| 板块资金流 | 无 | 无 | 同上，层级由维度事后补 |
| 板块分级 | 无 | 无 | 只有申万一家，概念/地域 `supports()` 直接 False |
| 基本数据 | 字段级填补 | `has_valuation` | 走完才够，`eastmoney+tencent` |

返回值带上是谁给的：

```python
@dataclass(frozen=True)
class Resolved:
    value: object
    platform: str      # 编排要靠它判断——东财给的 K 线不补当日 bar，兜底源给的要补
    merged_from: tuple  # 合成时记全，日志里 "eastmoney+tencent" 看得出是拼的
```

`status` 字典把"为什么空"带回调用方：`<platform>_unsupported` 逐个记，全都不支持时
记 `unsupported`——那是覆盖缺口，和"上游安静"两回事，工具的措辞不一样。

### 4.6 健康状态怎么分层

两级，因为它们回答的是不同问题：

| 级别 | 谁持有 | 怎么判 | 例子 |
|---|---|---|---|
| **已知降级** `degraded()` | 平台 | 现在就知道必败，不用数 | 伪装通道冷却期内的东财 |
| **熔断** | 每个 (平台, 能力) 一个 | 数失败次数/窗口 | `eastmoney/kline` 连续失败 3 次 |

熔断按 (平台, 能力) 而不是按平台，是因为同一个平台的不同接口会分别被拒——东财的
资金流接口挂掉时 K 线接口可能还好。但 `degraded()` 是平台级的，一个判据管住它所有
能力，这正好对上"伪装通道降级 → 东财全线必败"这个事实。

`resolve()` 里的顺序：先看 `degraded()`（零成本），再看熔断，再看 `supports()`，
最后才发请求。四道闸门从便宜到贵。

## 五、横切关注点

这四样不属于任何一层，被多层共用，所以各自只有一个安装点：

| 关注点 | 单一安装点 | 谁在用 | 关键约束 |
|---|---|---|---|
| 出站 HTTP | `http_channel.py` | 所有走网络的平台 | 四种模式互斥，一个进程只装一个；平台不自己决定怎么发 |
| 报告缓存 | `cache.py` + 工具层 | 工具层 | 只在"重新生成会得到同样字节"的纪元里复用，所以命中与否不改变返回内容 |
| 交易日历 | `trading_calendar.py` | 缓存纪元、资金流窗口、市场宽度 TTL | 全项目唯一判"今天开不开市"的地方；再写 `weekday() < 5` 就是 bug |
| 可观测 | `observability.py` | 全部 | `request_id/tool/symbol` 要跟着进线程池，否则最该串起来的那几行日志全是 `-` |

并发和内存的闸门是串联的，调一个必须看另一个：

```
BATCH_CONCURRENCY（工具层，几个批次同时在跑）
      └─ FETCH_MAX_IN_FLIGHT（编排层，同步任务总量）
            └─ FETCH_MAX_WORKERS（线程池大小）
      └─ FUND_FLOW_PAGE_CONCURRENCY（几个页面同时加载）
            └─ BROWSER_MAX_PAGES（几个渲染进程 ≈ 峰值内存，每个约 130 MiB）
```

只提其中一个，下一级立刻变成新瓶颈；提到底则会撞 500 MiB 的内存上限（AGENTS §四）。

## 六、要加东西，动哪一层

### 加一个 MCP 工具

只动工具层：定义签名和返回契约，复用已有维度。`sector_fund_flow` 是参照——工具本身
不到 60 行，取数全在下面。如果发现要往下写取数代码，先问一句是不是该新增一个能力。

### 加一个数据能力（比如"板块资金流"）

1. 新建 `datasource/<能力>.py`：定义请求类型、归一后的结构、`define_capability()`
2. 写 `SECTOR_FUND_FLOW_PROVIDERS` 这样的顺序配置
3. 至少一个平台实现 `fetch_<能力>`

`resolve()` 本身不用动。

### 加一个上游平台

以雪球为例，它能给行情、热度、内部交易：

```python
# finmcp/datasource/platforms/xueqiu.py
class XueqiuPlatform(HttpPlatform):
    name, label = "xueqiu", "雪球"
    capabilities = frozenset({"quote", "popularity", "insider_trade"})

    def _session(self):            # token 获取只写一次，三个能力共用
        ...

    def fetch_quote(self, request): ...
    def fetch_popularity(self, request): ...
    def fetch_insider_trade(self, request): ...

register(XueqiuPlatform())
```

然后在 `platforms/__init__.py` import 一行，把 `xueqiu` 写进对应维度的
`*_PROVIDERS`。**到此为止**——不改调用链、不改别的平台、不加 if/else。做不到就是
抽象没提对，改文档不要绕过去。

## 七、取数层的迁移进度

分阶段做，每一步都能单独验证：

| 阶段 | 内容 | 状态 | 怎么证明没坏 |
|---|---|---|---|
| 0 | `platform.py` 基类 + 注册表 + resolve + 单测 | ✅ | 单测；不接线，零风险 |
| 1 | **交易日历**作为第一个能力落在新架构上 | ✅ | 全新维度，没有等价性包袱；同时是这套抽象的第一份验证 |
| 2 | K 线迁到平台层 | ✅ | `prove_equivalence.py` 前后逐字比对，54 份文档 0 差异 |
| 3 | 同花顺平台、板块资金流 | ✅ | 预期**不等价**——看的是"差异只出现在指数、且往权威值靠" |
| 4 | 基本数据 + 盘中行情迁过来 | 待做 | `prove_equivalence.py` |
| 5 | 东财个股那几维从 `cn_stock_source` 迁出 | 待做 | 同上 |

先做交易日历是有意的：它是全新维度，用它验证抽象，比拿一个有等价性要求的维度去
试错便宜得多。而且它的兜底路径恰好检验了一个设计点——"取不到日历就退回
`weekday()<5`"在新架构里不是 if/else，是一个叫 `weekday` 的内建平台，
配成 `TRADING_CALENDAR_PROVIDERS=sina,weekday`。降级从藏在代码里变成配置里看得见的一行。

## 八、边界：这套架构不解决什么

- **口径冲突。** 字段级合成解的是"A 缺 a、B 有 a"；两个源对同一字段给出不同的值
  是仲裁问题，按配置顺序信前面那个，并把差异记进 `KNOWN_DIFFERENCES`（已核实可忽略）
  或 `PENDING_DISAGREEMENTS`（还没定谁对）。创业板指的成交量东财对、腾讯和新浪都低
  3.5%，合并多少次也变不出对的数。
- **上游本身的错。** 平台层只保证"取到的是谁给的、按什么顺序取的"，不保证对。
  谁对谁错要靠跨源校验（`INTRADAY_QUOTE_CROSS_CHECK_PCT`）和基线比对去发现。
- **性能。** 逐级回退天然是串行的，这是为了"上一级没给结果才付下一级的代价"。
  真需要并发探测多个源，那是 `resolve()` 之外的另一个函数，别把它塞进这条链。
- **跨层的临时捷径。** 想让工具层直接问某个平台要一手数据时，先问为什么组装层拿不到
  它——多半是缺一个能力，而不是缺一条捷径。
