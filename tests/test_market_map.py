"""市场云图：取数、契约、裁剪、两种输出格式。

重点在几处容易安静出错的地方：分页缺一页会悄悄给出一个更小的池子、加权字段的字段号
请求和取值写岔了不会报错只会让格子全不对、裁剪之后调用方拿去算占比而分母不全。
"""

from __future__ import annotations

import datetime
import json

import pytest

from finmcp import market_map_view as view
from finmcp.datasource import market_map_source as mms
from finmcp.datasource.platforms import eastmoney


def _stock(symbol="SH600519", name="贵州茅台", pct=1.0, size=1e12, sector="白酒Ⅱ", **extra):
    return mms.MarketMapStock(symbol=symbol, name=name, change_pct=pct,
                              size=size, sector=sector, float_cap=size, **extra)


_UNSET = object()


def _map(stocks=_UNSET, **over):
    # 哨兵而不是 `stocks or 默认`：传空列表是有意义的输入（契约要判它），
    # 用 or 会把空列表变回默认那只，于是"空图不合契约"那条测试平凡通过。
    if stocks is _UNSET:
        stocks = [_stock()]
    base = dict(stocks=tuple(stocks), board="all", size_field="float_cap",
                upstream_total=max(1, len(stocks)), as_of=datetime.date(2026, 9, 9),
                source="eastmoney")
    base.update(over)
    return mms.MarketMap(**base)


# --- 板块选择 ----------------------------------------------------------------


def test_the_five_boards_partition_the_whole_market():
    """五个板块不重不漏地拼成全A。

    2026-09-09 实测上游 total：1846+621+1640+1450+354 = 5911，与 ``all`` 相等。
    这里只钉住"每个板块都有自己的 fs、且互不相同"——真实 total 会随上市家数变，
    钉数字等于每周挂一次测试。
    """
    selectors = {code: fs for code, (_, fs) in mms.BOARDS.items()}
    boards = [c for c in selectors if c != "all"]
    assert len(set(selectors[c] for c in boards)) == len(boards), "有两个板块用了同一个 fs"
    # all 的 fs 就是其余几个拼起来的
    parts = set(selectors["all"].split(","))
    for code in boards:
        assert set(selectors[code].split(",")) <= parts, f"{code} 不在 all 里"
    assert parts == {p for c in boards for p in selectors[c].split(",")}, "all 多了或少了"


def test_as_of_uses_the_configured_market_warmup(monkeypatch):
    monkeypatch.setattr(mms.market_session, "WARMUP_TIME", datetime.time(8, 45))
    monkeypatch.setattr(mms.trading_calendar, "is_trading_day", lambda day: True)
    monkeypatch.setattr(mms.trading_calendar, "previous_trading_day",
                        lambda day: day - datetime.timedelta(days=1))
    assert mms._as_of(datetime.datetime(2026, 9, 10, 8, 44)) == datetime.date(2026, 9, 9)
    assert mms._as_of(datetime.datetime(2026, 9, 10, 8, 45)) == datetime.date(2026, 9, 10)


def test_board_names_use_codes_because_shangzheng_is_ambiguous():
    """「上证」含不含科创板有歧义，这个歧义要在参数名上解掉。"""
    assert mms.BOARDS["sse"][0] == "上证主板"
    assert mms.BOARDS["star"][0] == "科创板"
    assert mms.BOARDS["sse"][1] != mms.BOARDS["star"][1]


@pytest.mark.parametrize("board,size", [("nasdaq", "float_cap"), ("all", "volume")])
def test_unknown_board_or_size_is_rejected(board, size):
    with pytest.raises(ValueError):
        mms.resolve(mms.MarketMapRequest(board=board, size=size))


# --- 契约 --------------------------------------------------------------------


def test_contract_checks_structure_but_keeps_missing_amounts():
    contract = mms._honours_contract
    assert contract(_map()) is True
    assert contract(_map([_stock(sector="")])) is False
    assert contract(_map([_stock(size=None)])) is True
    assert contract(_map([_stock(), _stock("SH600520", size=None)])) is True
    assert contract(_map([])) is False
    assert contract("不是 MarketMap") is False


def test_contract_checks_every_stock_not_only_the_first_page():
    stocks = [_stock(symbol=f"SH{600000 + i}") for i in range(25)]
    stocks[-1] = _stock("SH600999", sector="")
    assert mms._honours_contract(_map(stocks)) is False


def test_completeness_is_about_pages_not_about_having_data():
    """取到一半也是"有数据"。完整与否看页号和总数，不看非空。"""
    assert _map(upstream_total=1).complete is True
    assert _map(upstream_total=100).complete is False
    assert _map(upstream_total=1, missing_pages=(3,)).complete is False


def test_an_incomplete_snapshot_is_not_cached():
    """缺页多半是上游一时的事。缓了它，之后每次调用都拿到同一个缺页的池子。"""
    namespace = __import__("finmcp.cache", fromlist=["namespace"]).namespace(
        mms.CACHE_NAMESPACE)
    assert namespace.cacheable(_map(upstream_total=1), None) is True
    assert namespace.cacheable(_map(upstream_total=100), None) is False


def test_the_snapshot_survives_a_cache_round_trip():
    """磁盘层要能原样还原——存成四元组是为了省字节，不能顺手丢字段。"""
    namespace = __import__("finmcp.cache", fromlist=["namespace"]).namespace(
        mms.CACHE_NAMESPACE)
    original = _map([_stock(), _stock("SZ300750", "宁德时代", 0.4, 1.4e12, "电池")])
    restored = namespace.decode(namespace.encode(original))
    assert restored == original


# --- 加权字段 ----------------------------------------------------------------


def test_the_weight_field_is_requested_and_read_through_one_mapping():
    """请求和取值必须用同一个变量。

    各写各的就会出现"标着流通市值、其实是成交额"——不报错、不缺数，只是格子大小
    全不对。板块资金流里踩过同类的坑（当日口径拿到了 10 日的数）。
    """
    assert eastmoney._MARKET_MAP_SIZE_FIELD == {"float_cap": "f21", "turnover": "f6"}
    assert set(eastmoney._MARKET_MAP_SIZE_FIELD) == set(mms.SIZE_FIELDS)


def test_float_cap_is_the_default_because_of_construction_bank():
    """建设银行是判据：总市值全市场第二，A 股流通市值只有 1038 亿，图里是个细条。

    默认口径要是总市值，建行就成了大格，和主流云图对不上。
    """
    assert mms.MarketMapRequest().size == "float_cap"


@pytest.mark.parametrize("raw,expected", [
    (3.92, 3.92), (0, 0.0), ("-", None), (None, None), ("", None), (True, None),
    ("1.5", 1.5),
])
def test_upstream_dashes_become_none_not_an_exception(raw, expected):
    """停牌和上市首日的市值上游给的是字符串 ``"-"``。

    直接 float() 会抛，而抛在翻页循环里会把整页丢掉——一页 100 只。
    """
    assert eastmoney._number(raw) == expected


def test_the_market_prefix_comes_from_the_exchange_field():
    """f13 区分沪市；深市与北交所同为 0，北交所还要按代码段归一。"""
    assert eastmoney._prefixed("600519", 1) == "SH600519"
    assert eastmoney._prefixed("300750", 0) == "SZ300750"
    assert eastmoney._prefixed("920268", "0") == "BJ920268"
    assert eastmoney._prefixed("430047", 0) == "BJ430047"
    assert eastmoney._prefixed("871981", 0) == "BJ871981"


# --- 分页 --------------------------------------------------------------------


class _FakeUpstream:
    """按页发数据的假上游。``broken`` 里的页号抛异常。"""

    def __init__(self, total, broken=(), page_size=100):
        self.total, self.broken, self.page_size = total, set(broken), page_size
        self.pages_asked: list = []

    def get(self, url, timeout=None, params=None, headers=None):
        page = params["pn"]
        self.pages_asked.append(page)
        if page in self.broken:
            raise RuntimeError("上游 502")
        start = (page - 1) * self.page_size
        rows = [{"f12": f"{600000 + i:06d}", "f13": 1, "f14": f"股票{i}",
                 "f3": 1.0, "f21": 1e10, "f6": 1e8, "f100": f"行业{i % 5}"}
                for i in range(start, min(start + self.page_size, self.total))]
        return type("R", (), {"text": json.dumps({"data": {"total": self.total,
                                                           "diff": rows}})})()


def _fetch(monkeypatch, upstream, board="all"):
    import requests

    monkeypatch.setattr(requests, "get", upstream.get)
    return eastmoney.EastmoneyPlatform().fetch_market_map(
        mms.MarketMapRequest(board=board))


def test_pagination_walks_until_the_upstream_total_is_covered(monkeypatch):
    """单页硬上限 100 行（pz 给 6000 也只回 100），所以翻页次数是 ceil(total/100)。"""
    upstream = _FakeUpstream(250)
    result = _fetch(monkeypatch, upstream)
    assert len(result.stocks) == 250
    assert upstream.pages_asked == [1, 2, 3]
    assert result.complete is True


def test_market_map_has_a_second_endpoint_provider():
    assert mms.DEFAULT_PROVIDER_ORDER == ("eastmoney", "eastmoney_delay")
    assert "market_map" in eastmoney.EastmoneyDelayPlatform.capabilities
    assert eastmoney.EastmoneyPlatform.market_map_url != \
        eastmoney.EastmoneyDelayPlatform.market_map_url


def test_delay_provider_uses_the_same_contract_and_its_own_host(monkeypatch):
    import requests

    seen = []

    def get(url, timeout=None, params=None, headers=None):
        seen.append(url)
        rows = [{"f12": "688981", "f13": 1, "f14": "中芯国际", "f3": 1.0,
                 "f21": 1e12, "f100": "半导体"}]
        return type("R", (), {"text": json.dumps({"data": {"total": 1,
                                                               "diff": rows}})})()

    monkeypatch.setattr(requests, "get", get)
    result = eastmoney.EastmoneyDelayPlatform().fetch_market_map(
        mms.MarketMapRequest(board="star")
    )
    assert seen == [eastmoney.EastmoneyDelayPlatform.market_map_url]
    assert result.stocks[0].symbol == "SH688981"


def test_a_failed_page_keeps_the_rest_and_says_how_much_is_missing(monkeypatch):
    """静默丢掉一页就是悄悄给了个小 100 只的池子，而调用方会拿它当全集算占比。"""
    result = _fetch(monkeypatch, _FakeUpstream(250, broken=[2]))
    assert len(result.stocks) == 150
    assert result.missing_pages == (2,)
    assert result.complete is False
    assert any("第 2 页取不到" in w for w in result.warnings)
    assert any("100 只" in w for w in result.warnings)


def test_a_failed_first_page_hands_over_to_the_next_source(monkeypatch):
    """第一页就没有时没有"部分"可言，返回 None 让位给下一个源。"""
    assert _fetch(monkeypatch, _FakeUpstream(250, broken=[1])) is None


def test_pagination_has_a_hard_ceiling(monkeypatch):
    """翻页的终止条件依赖上游的 total。total 回个荒唐的大数，没有上限就一直翻下去。"""
    upstream = _FakeUpstream(999999)
    result = _fetch(monkeypatch, upstream)
    assert len(upstream.pages_asked) == eastmoney._MARKET_MAP_MAX_PAGES
    assert any("上限" in w for w in result.warnings)


def test_a_stock_without_an_industry_is_kept_not_dropped(monkeypatch):
    """行业缺失时它照样有涨跌幅和面积，只是归不了组。丢掉会让"全市场"少几只而没人知道。"""
    import requests

    def get(url, timeout=None, params=None, headers=None):
        rows = [{"f12": "600519", "f13": 1, "f14": "有行业", "f3": 1.0, "f21": 1e10,
                 "f100": "白酒Ⅱ"},
                {"f12": "600520", "f13": 1, "f14": "没行业", "f3": 2.0, "f21": 1e10,
                 "f100": ""}]
        return type("R", (), {"text": json.dumps({"data": {"total": 2, "diff": rows}})})()

    monkeypatch.setattr(requests, "get", get)
    result = eastmoney.EastmoneyPlatform().fetch_market_map(mms.MarketMapRequest())
    assert len(result.stocks) == 2
    assert result.stocks[1].sector == "未分类"


def test_the_board_filter_is_pushed_upstream(monkeypatch):
    """筛选是少取，不是取回来再滤——这是把 60 页压到 7 页的唯一办法。"""
    import requests

    seen: list = []

    def get(url, timeout=None, params=None, headers=None):
        seen.append(params["fs"])
        return type("R", (), {"text": json.dumps({"data": {"total": 0, "diff": []}})})()

    monkeypatch.setattr(requests, "get", get)
    eastmoney.EastmoneyPlatform().fetch_market_map(mms.MarketMapRequest(board="star"))
    assert seen == [mms.BOARDS["star"][1]]


def test_total_budget_returns_partial_data_and_marks_untried_pages(monkeypatch):
    """总预算用尽时保住已取页面，并把没尝试的页全部报出来。"""
    import requests

    now = [0.0]
    asked = []

    def get(url, timeout=None, params=None, headers=None):
        asked.append((params["pn"], timeout))
        now[0] += 1.1
        start = (params["pn"] - 1) * 100
        rows = [{"f12": f"{600000 + i:06d}", "f13": 1, "f14": f"股票{i}",
                 "f3": 1.0, "f21": 1e10, "f100": "行业"}
                for i in range(start, min(start + 100, 500))]
        return type("R", (), {"text": json.dumps({"data": {"total": 500,
                                                               "diff": rows}})})()

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(eastmoney.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(eastmoney, "MARKET_MAP_BUDGET_SECONDS", 2.5)
    result = eastmoney.EastmoneyPlatform().fetch_market_map(mms.MarketMapRequest())

    assert [page for page, _ in asked] == [1, 2]
    assert len(result.stocks) == 200
    assert result.missing_pages == (3, 4, 5)
    assert result.complete is False
    assert all(timeout <= 2.5 for _, timeout in asked)


def test_total_budget_never_starts_a_request_that_cannot_get_one_second(monkeypatch):
    """剩余不足一秒时不把 timeout 撑回一秒，避免预算只是建议。"""
    import requests

    now = [0.0]
    timeouts = []

    def get(url, timeout=None, params=None, headers=None):
        timeouts.append(timeout)
        now[0] += 1.2
        rows = [{"f12": "600519", "f13": 1, "f14": "贵州茅台", "f3": 1.0,
                 "f21": 1e12, "f100": "白酒Ⅱ"}]
        return type("R", (), {"text": json.dumps({"data": {"total": 201,
                                                               "diff": rows}})})()

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(eastmoney.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(eastmoney, "MARKET_MAP_BUDGET_SECONDS", 2.0)
    result = eastmoney.EastmoneyPlatform().fetch_market_map(mms.MarketMapRequest())

    assert timeouts == [2.0]
    assert result.missing_pages == (2, 3)


# --- 裁剪 --------------------------------------------------------------------


def _groups(n):
    return [{"sector": f"S{i}", "members": [], "size": 1.0, "change_pct": 10.0 - i,
             "median_pct": 10.0 - i, "up": 0, "down": 0} for i in range(n)]


def test_trimming_takes_both_ends_not_just_the_top():
    """云图看的是分布的两端。只给最涨的那批，就看不出今天是普涨还是分化。"""
    kept, trim = view.select(_groups(130), 10)
    names = [g["sector"] for g in kept]
    assert names[:2] == ["S0", "S1"] and names[-2:] == ["S128", "S129"]
    assert trim["sectors_returned"] == 20 and trim["sectors_total"] == 130


@pytest.mark.parametrize("total,ask", [(130, 10), (20, 10), (5, 10), (4, 2), (3, 2), (1, 5)])
def test_trimming_never_repeats_a_sector(total, ask):
    """头尾相接时会重叠。按下标切而不按值去重——那些字典里装着几百个成员对象。"""
    kept, _ = view.select(_groups(total), ask)
    names = [g["sector"] for g in kept]
    assert len(names) == len(set(names)), names
    assert len(names) <= total


def test_asking_for_everything_reports_no_ranking():
    """全要时没有"按什么排"这回事，ranked_by 该是空的——写个值会让人以为被裁过。"""
    _, trim = view.select(_groups(30), "all")
    assert trim["ranked_by"] is None
    assert trim["sectors_returned"] == trim["sectors_total"] == 30


def test_a_trimmed_view_warns_against_computing_shares():
    """裁剪之后分母不全。不说的话，读者会拿表里的面积去算占比。"""
    stocks = [_stock(f"SH{600000 + i}", f"股票{i}", float(i), 1e11, f"行业{i}")
              for i in range(30)]
    report = view.render(_map(stocks), fmt="markdown", sectors=3, rank_by="change_pct")
    assert "不能作为全市场占比的分母" in report
    assert "涨跌" in report



# --- 行业内个股裁剪 ------------------------------------------------------------


def test_stocks_per_sector_limits_listed_members():
    """默认 20 只；给 0 全列。裁剪只影响列出哪些，不影响行业聚合。"""
    stocks = [_stock(f"SH{600000+i}", f"股票{i}", float(i), 1e11, "银行",
                     main_net=10.0)
              for i in range(30)]
    data = _map(stocks)
    # 默认 20
    payload = json.loads(view.render(data, fmt="json", sectors="all"))
    sector = payload["sectors"][0]
    assert len(sector["stocks"]) == 20
    assert sector["stocks_total"] == 30
    assert sector["stocks_omitted"] == 10
    # 全列
    payload_full = json.loads(view.render(data, fmt="json", sectors="all",
                                          stocks_per_sector=0))
    assert len(payload_full["sectors"][0]["stocks"]) == 30
    assert payload_full["sectors"][0]["stocks_omitted"] == 0


def test_stocks_per_sector_keeps_the_strongest_by_change():
    """裁掉的是涨跌幅靠后的，保留的是最强的那批。"""
    stocks = [_stock(f"SH{600000+i}", f"股票{i}", float(i), 1e11, "半导体")
              for i in range(10)]
    data = _map(stocks)
    payload = json.loads(view.render(data, fmt="json", sectors="all",
                                     stocks_per_sector=3))
    symbols = [s["symbol"] for s in payload["sectors"][0]["stocks"]]
    assert symbols == ["SH600009", "SH600008", "SH600007"]


def test_stocks_per_sector_does_not_affect_sector_aggregates():
    """资金流合计按全部成员算，不因列出几只而少算。"""
    stocks = [_stock(f"SH{600000+i}", f"股票{i}", float(i), 1e11, "银行",
                     main_net=100.0)
              for i in range(30)]
    data = _map(stocks)
    md = view.render(data, fmt="markdown", sectors="all", stocks_per_sector=5)
    # 30 × 100 = 3000，合计必须仍然是 3000
    assert "3,000.00" in md
    assert "资金流覆盖 30/30" in md


def test_stocks_per_sector_smaller_than_sector_size_lists_all():
    """行业只有 3 只时给 20 不报错，全列。"""
    stocks = [_stock(f"SH{600000+i}", f"股票{i}", float(i), 1e11, "白酒Ⅱ")
              for i in range(3)]
    data = _map(stocks)
    payload = json.loads(view.render(data, fmt="json", sectors="all",
                                     stocks_per_sector=20))
    assert len(payload["sectors"][0]["stocks"]) == 3
    assert payload["sectors"][0]["stocks_omitted"] == 0


def test_markdown_respects_stocks_per_sector():
    stocks = [_stock(f"SH{600000+i}", f"股票{i}", float(i), 1e11, "银行")
              for i in range(30)]
    data = _map(stocks)
    md = view.render(data, fmt="markdown", sectors="all", stocks_per_sector=5)
    # 只列前 5 只（涨跌幅最高的）
    assert "股票29" in md
    assert "股票24" not in md  # 第 6 名不该出现


@pytest.mark.asyncio
async def test_the_tool_rejects_bad_stocks_per_sector():
    import sys
    mcp_app = sys.modules["finmcp.mcp_app"]
    with pytest.raises(ValueError):
        await mcp_app.market_map(stocks_per_sector=-1)



# --- 两种格式 ----------------------------------------------------------------


def test_json_carries_base_data_and_no_derived_values():
    """派生值一概不放：调用方一行 groupby 就有，塞进响应只是每次多传几十 KiB。"""
    payload = json.loads(view.render(_map(), fmt="json", sectors="all"))
    sector = payload["sectors"][0]
    assert set(sector) == {"sector", "stocks", "stocks_total", "stocks_omitted"}, sector.keys()
    assert set(sector["stocks"][0]) == {
        "symbol", "name", "change_pct", "last", "float_cap", "amount_yuan", "main_net", "main_pct"}
    for derived in ("market_cap", "change_pct", "median_pct", "up", "down",
                    "count", "breadth"):
        assert derived not in sector, f"{derived} 是能算出来的，不该返回"


def test_json_declares_the_size_field_and_provenance():
    """口径和出处不是派生值。

    不声明 size_field，调用方不知道拿到的是流通市值还是成交额，而两者画出来的图
    完全不同；裁剪时不说 ranked_by 和被略掉多少，调用方没法判断这几个是怎么选的。
    """
    payload = json.loads(view.render(_map(), fmt="json", sectors="all"))
    for key in ("board", "as_of", "weight_by", "source", "upstream_total",
                "returned", "complete", "sectors_total", "sectors_returned",
                "ranked_by", "warnings"):
        assert key in payload, key


def test_json_is_compact_because_size_is_the_point():
    """带缩进体积涨两三倍，而体积正是这个格式存在的理由。"""
    body = view.render(_map(), fmt="json", sectors="all")
    assert '":' in body and '": ' not in body
    assert "\n" not in body


def test_markdown_returns_the_same_base_stocks_as_json():
    stocks = [_stock("SH601398", "工商银行", 0.76, 2.16e12, "银行"),
              _stock("SH601939", "建设银行", 1.31, 1.04e11, "银行")]
    report = view.render(_map(stocks), fmt="markdown", sectors="all")
    assert "### 银行" in report
    payload = json.loads(view.render(_map(stocks), fmt="json", sectors="all"))
    for stock in payload["sectors"][0]["stocks"]:
        assert stock["symbol"] in report
        assert stock["name"] in report
        assert f'{stock["float_cap"]:,.2f}' in report
    assert "中位" not in report and "| 加权涨跌 |" not in report
    assert "| 面积 |" not in report


def test_missing_change_does_not_dilute_weighted_change_to_zero():
    stocks = [_stock("SH600001", pct=2.0, size=100.0),
              _stock("SH600002", pct=None, size=900.0)]
    groups = view.group(stocks)
    assert groups[0]["size"] == 1000.0       # 面积仍含全部流通市值
    assert groups[0]["change_pct"] == 2.0    # 颜色只按有涨跌数据的成员加权


def test_missing_amount_is_preserved_in_both_output_formats():
    data = _map([_stock(size=None)])
    payload = json.loads(view.render(data, fmt="json", sectors="all"))
    assert payload["sectors"][0]["stocks"][0]["float_cap"] is None
    report = view.render(data, fmt="markdown", sectors="all")
    assert "SH600519" in report and "| +1.00% | — |" in report
    with pytest.raises(ValueError, match="json/markdown"):
        view.render(data, fmt="view")


def test_view_states_the_size_field_and_the_source():
    report = view.render(_map(), fmt="markdown", sectors="all")
    assert "行业涨跌加权 流通市值" in report
    assert "来源 eastmoney" in report
    turnover = view.render(_map(size_field="turnover"), fmt="markdown", sectors="all")
    assert "行业涨跌加权 成交额" in turnover


def test_view_says_when_the_snapshot_is_incomplete():
    """不完整要写在抬头，不能埋在备注里——读者会拿它当全市场。"""
    report = view.render(_map(upstream_total=5911), fmt="markdown", sectors="all")
    assert "不完整" in report and "5911" in report


def test_both_formats_pass_the_upstream_warnings_through():
    warned = _map(upstream_total=250, missing_pages=(2,),
                  warnings=("第 2 页取不到，本次少了约 100 只",))
    assert "第 2 页取不到" in json.loads(view.render(warned, fmt="json"))["warnings"][0]
    assert "第 2 页取不到" in view.render(warned, fmt="markdown")


# --- 工具层接线 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_returns_the_requested_format(monkeypatch):
    """工具 → 能力 → 缓存 → 渲染整条接线。

    平台层的测试盖不住这一段：参数校验、fmt 分派、取不到时报什么，都在工具里。
    """
    import sys

    mcp_app = sys.modules["finmcp.mcp_app"]
    stocks = [_stock("SH688981", "中芯国际", 3.92, 6.18e11, "半导体"),
              _stock("SH688041", "海光信息", -0.46, 4.0e11, "半导体")]
    monkeypatch.setattr(mms, "resolve",
                        lambda request, **kw: _map(stocks, board=request.board,
                                                   size_field=request.size))

    body = await mcp_app.market_map(board="star", fmt="json", sectors=0)
    payload = json.loads(body)
    assert payload["board"] == "star" and payload["returned"] == 2
    assert payload["sectors"][0]["sector"] == "半导体"

    text = await mcp_app.market_map(board="star", fmt="markdown", sectors=0)
    assert text.startswith("# 市场云图　科创板")


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"fmt": "png"}, {"fmt": "view"}, {"rank_by": "unknown"}, {"board": "nasdaq"}, {"weight_by": "volume"}, {"sectors": -1},
])
async def test_the_tool_rejects_bad_arguments(kwargs):
    """手滑打错一个参数要当场报错，不能默默退回默认值给出一份不是你要的图。"""
    import sys

    mcp_app = sys.modules["finmcp.mcp_app"]
    with pytest.raises(ValueError):
        await mcp_app.market_map(**kwargs)


@pytest.mark.asyncio
async def test_the_tool_says_so_when_every_source_failed(monkeypatch):
    import sys

    mcp_app = sys.modules["finmcp.mcp_app"]
    monkeypatch.setattr(mms, "resolve", lambda request, **kw: None)
    with pytest.raises(RuntimeError, match="全部源都没给出结果"):
        await mcp_app.market_map(board="star")


# --- 退市股 ------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "-", "--", "null", "None", "  ", None])
def test_upstream_placeholders_become_unclassified(raw):
    """上游对退市股给的是字面的 ``"-"``——不是 null，也不是空串。

    直接用它会在报告里冒出一个叫 ``-`` 的行业，实测出现过（退市泽达、退市观典）。
    """
    assert eastmoney._sector_of(raw) == "未分类"
    assert eastmoney._sector_of("半导体") == "半导体"


def test_a_sector_without_a_change_pct_is_not_ranked_as_the_worst():
    """退市那一组整组都是 None，会被排序键甩到末尾。

    而 ``ranked[-N:]`` 取的是末尾——于是"退市"被当成跌得最多的那批捞上来。
    实测出现过：科创板的两端里挤进一行 ``- | 0亿 | — | 退市泽达、退市观典``。
    """
    groups = _groups(6) + [{"sector": "未分类", "members": [], "size": 0.0,
                            "change_pct": None, "median_pct": None, "up": 0, "down": 0}]
    kept, trim = view.select(groups, 2)
    assert "未分类" not in [g["sector"] for g in kept]
    assert trim["sectors_total"] == 7


def test_asking_for_everything_still_keeps_the_unclassified_group():
    """裁剪时不进两端，全要时不能丢——它们仍然是市场的一部分。"""
    groups = _groups(3) + [{"sector": "未分类", "members": [], "size": 0.0,
                            "change_pct": None, "median_pct": None, "up": 0, "down": 0}]
    kept, _ = view.select(groups, "all")
    assert "未分类" in [g["sector"] for g in kept]


def test_a_trimmed_view_says_how_many_out_of_how_many():
    """只写"44 只"会被读成"科创板只有 44 只"。"""
    stocks = [_stock(f"SH{600000 + i}", f"股票{i}", float(i - 15), 1e11, f"行业{i}")
              for i in range(30)]
    report = view.render(_map(stocks, upstream_total=621), fmt="markdown", sectors=3, rank_by="change_pct")
    assert "（共 621 只）" in report
    full = view.render(_map(stocks, upstream_total=30), fmt="markdown", sectors="all")
    assert "（共" not in full, "没裁剪时不必啰嗦"


def test_sector_net_flow_selects_both_ends_and_members_sort_by_change():
    stocks = [
        _stock("SH600001", "大市值弱股", 0.29, 1e12, "软件", main_net=20),
        _stock("BJ920592", "小市值强股", 4.38, 1e9, "软件", main_net=80),
        _stock("SH600002", pct=-1, sector="中间", main_net=3),
        _stock("SH600003", pct=10, sector="流出", main_net=-90),
    ]
    data = _map(stocks)
    payload = json.loads(view.render(data, fmt="json", sectors=1))
    assert [g["sector"] for g in payload["sectors"]] == ["软件", "流出"]
    assert payload["ranked_by"] == "member_main_net_sum"
    assert [s["symbol"] for s in payload["sectors"][0]["stocks"]] == ["BJ920592", "SH600001"]
    markdown = view.render(data, fmt="markdown", sectors=1)
    assert "### 软件\n\n| 代码 |" in markdown
    assert markdown.index("小市值强股") < markdown.index("大市值弱股")
    assert "成员主力净流入合计：100.00 元" in markdown
    assert "### 流出" in markdown and "### 中间" not in markdown


def test_partial_flow_ranks_by_known_subtotal_and_is_disclosed():
    data = _map([
        _stock("SH600001", sector="缺值", main_net=1000),
        _stock("SH600002", sector="缺值", main_net=None),
        _stock("SH600003", sector="齐全", main_net=0),
    ])
    payload = json.loads(view.render(data, fmt="json", sectors=1))
    assert [g["sector"] for g in payload["sectors"]] == ["缺值", "齐全"]
    assert any("按已知成员小计参与排名" in w for w in payload["warnings"])
    markdown = view.render(data, fmt="markdown", sectors="all")
    assert "### 缺值" in markdown
    assert "已知成员主力净流入小计：1,000.00 元；资金流覆盖 1/2" in markdown


def test_flow_fields_are_fetched_in_one_request_and_survive_disk(monkeypatch):
    import requests
    calls = []
    row = {"f12": "688981", "f13": 1, "f14": "中芯国际", "f100": "半导体",
           "f2": 119.18, "f3": -1.1, "f21": 238430782075, "f6": 2288876808,
           "f62": -152796990, "f184": -6.68}
    def get(url, **kwargs):
        calls.append(kwargs["params"])
        return type("R", (), {"text": json.dumps({"data": {"total": 1, "diff": [row]}})})()
    monkeypatch.setattr(requests, "get", get)
    result = eastmoney.EastmoneyDelayPlatform().fetch_market_map(mms.MarketMapRequest())
    assert len(calls) == 1
    assert {"f2", "f3", "f6", "f21", "f62", "f184"} <= set(calls[0]["fields"].split(","))
    stock = result.stocks[0]
    assert stock.main_net == -152796990 and stock.main_pct == -6.68
    assert stock.float_cap == 238430782075 and stock.amount_yuan == 2288876808
    assert mms._from_payload(json.loads(json.dumps(mms._payload(result)))) == result


def test_missing_change_sorts_last_even_when_market_cap_is_largest():
    stocks = [_stock("SH600003", pct=None, size=1e15),
              _stock("SH600001", pct=0), _stock("SH600002", pct=-2)]
    payload = json.loads(view.render(_map(stocks), fmt="json", sectors="all"))
    assert [s["symbol"] for s in payload["sectors"][0]["stocks"]] == ["SH600001", "SH600002", "SH600003"]
