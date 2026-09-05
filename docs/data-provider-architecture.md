# 取数架构：平台 → 能力 → 归一

> 这份文档是准则 AGENTS.md §二 的落地形态。改动取数层之前先读它；接一个新数据源
> 之前也先读它——照着做应该只需要**写一个类 + 注册一行**，如果发现要改别的文件，
> 那是抽象出了问题，回来改这份文档而不是绕过去。

## 一、为什么要有这一层

现在每一维数据各建了一个注册表，同一个上游在里面是几个互不相干的类：

```
basic_info._PROVIDERS     = {eastmoney, tencent}
intraday_quote._PROVIDERS = {fund_flow_page, tencent}
kline_source._PROVIDERS   = {tencent, sina}
```

腾讯出现三次，但"腾讯"这个平台的事实只有一份：它的主机、请求头、GBK 编码、代码
写法（`sh600519`）、成交量给的是股不是手、对北交所抛 KeyError、被限流时的表现。
这些事实被切进了三个注册表，于是：

- **共享不了。** 接同花顺时，`SH000001 → hs_1A0001` 这张映射表它的 K 线能力和
  行情能力都要用，但两个注册表之间没有能放它的地方。
- **健康状态串不起来。** 腾讯要是被限流，`kline_source` 的失败计数不会告诉
  `intraday_quote` 那边也在失败。这和当初"伪装通道降级了，每个源却要各自重新
  发现一遍"是同一类问题——那次的修法就是让源直接问通道，这里同理。
- **接一个源要写 N 个类。** 雪球能给行情、基本数据、热度、内部交易四样，按现在的
  切法就是四个类分散在四个文件里，共同的 token 获取逻辑没地方放。

所以把"平台"提成一等公民。**但有一条不跟着上提：顺序留在维度级**——同一个平台在
不同维度的优先级不一样。K 线该把同花顺排第一（创业板指只有它和东财是对的），
基本数据该把东财排第一（字段最全）。

## 二、三层各自负责什么

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
│  契约由维度定义   KlineFrame / BasicInfo / IntradayQuote ...   │
│  归一由平台实现   它才知道自己的单位、字段名、时区、编码          │
│  公共工具共享     kline_frame.py 这种，谁都能 import，不产生循环 │
└────────────────────────────────────────────────────────────┘
```

**归一为什么放在平台里而不是抽出来做一层"适配器"**：单位和字段名的坑是平台特有的
（腾讯的指数成交量给股、个股也给股，新浪沪市指数给手、深市指数给股），把它们集中
到一个适配器里就变成了一大堆 `if platform == ...`，正是要消灭的东西。契约放在维度
侧，是为了让"报告需要哪些字段"这件事只有一个定义处。

### 契约是强制的，不是约定

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

## 三、基类

```python
# qtf_mcp/datasource/platform.py

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

## 四、通用 resolve

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

对照今天的两个实现：

| 维度 | merge | enough | 等价于今天的 |
|---|---|---|---|
| K 线 | 无 | 无 | `kline_source.resolve()` 首个非空即返回 |
| 盘中行情 | 无 | 无 | `intraday_quote.resolve()` |
| 基本数据 | 字段级填补 | `has_valuation` | `basic_info.resolve()` + `_merge()` |

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

## 五、健康状态怎么分层

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

## 六、加一个新平台要做什么

以雪球为例，它能给行情、热度、内部交易：

```python
# qtf_mcp/datasource/platforms/xueqiu.py
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

然后把 `xueqiu` 写进对应维度的 `*_PROVIDERS`。**到此为止**——不改调用链、不改别的
平台、不加 if/else。做不到就是抽象没提对，改文档不要绕过去。

新增一个**能力**（比如板块资金流）多两步：在维度模块里定义请求类型和契约，写一个
`SECTOR_FUND_FLOW_PROVIDERS` 配置。resolve 本身不用动。

## 七、迁移顺序

分阶段，每一步都能单独验证：

| 阶段 | 内容 | 怎么证明没坏 |
|---|---|---|
| 0 | `platform.py` 基类 + 注册表 + resolve + 单测 | 单测；不接线，零风险 |
| 1 | **交易日历**作为第一个能力落在新架构上 | 全新维度，没有等价性包袱；同时是这套抽象的第一份验证 |
| 2 | K 线迁到平台层 | `prove_equivalence.py` 前后逐字比对 |
| 3 | 基本数据 + 盘中行情迁过来 | 同上 |
| 4 | 同花顺平台（修创业板指）、板块资金流 | 预期**不等价**——创业板指的数会变，要看的是"差异只出现在它、且往权威值靠" |

先做交易日历是有意的：它是全新维度，用它验证抽象，比拿一个有等价性要求的维度去
试错便宜得多。而且它的兜底路径恰好能检验一个设计点——"取不到日历就退回
`weekday()<5`"在新架构里不是 if/else，是一个叫 `weekday` 的内建平台，
配成 `TRADING_CALENDAR_PROVIDERS=sina,weekday`。降级从藏在代码里变成配置里看得见的一行。

## 八、边界：这套架构不解决什么

- **口径冲突。** 字段级合成解的是"A 缺 a、B 有 a"；两个源对同一字段给出不同的值
  是仲裁问题，按配置顺序信前面那个，并把差异记进 `KNOWN_DIFFERENCES`。创业板指的
  成交量东财对、腾讯和新浪都低 3.5%，合并多少次也变不出对的数。
- **上游本身的错。** 平台层只保证"取到的是谁给的、按什么顺序取的"，不保证对。
  谁对谁错要靠跨源校验（`INTRADAY_QUOTE_CROSS_CHECK_PCT`）和基线比对去发现。
- **性能。** 逐级回退天然是串行的，这是为了"上一级没给结果才付下一级的代价"。
  真需要并发探测多个源，那是 `resolve()` 之外的另一个函数，别把它塞进这条链。
