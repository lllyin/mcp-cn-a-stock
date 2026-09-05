"""板块资金流：契约、分级、降级形态、渲染。

这一维回答个股资金流答不了的问题——"茅台主力净流入 3.68亿"没有语境，是整个板块
在被买还是只有它。
"""

from __future__ import annotations

import datetime

import pytest

from finmcp.datasource import platform as pf
from finmcp.datasource import sector_fund_flow as sff
from finmcp.datasource import sector_taxonomy as stx
from finmcp.mcp_app import _render_sector_fund_flow

AS_OF = datetime.date(2026, 9, 4)


def _board(partial=False, source="eastmoney", levels=(None, None, None), **kwargs):
    names = ("传媒", "半导体", "持平板块")
    return sff.SectorFundFlowBoard(
        sectors=(
            sff.SectorFlow(name=names[0], change_pct=2.32, main_net=6.174e9, level=levels[0],
                           main_pct=7.24, xl_net=4.36e9, l_net=1.81e9, leader="天娱数科"),
            sff.SectorFlow(name=names[1], change_pct=-1.2, main_net=-6.02e10, level=levels[1],
                           main_pct=-3.1, xl_net=-4e10, l_net=-2e10, leader="某某股份"),
            sff.SectorFlow(name=names[2], change_pct=0.0, main_net=0.0, level=levels[2]),
        ),
        sector_type="industry", period="today", source=source, partial=partial,
        as_of=AS_OF, **kwargs,
    )


# --- 契约 -------------------------------------------------------------------


def test_the_contract_is_registered():
    assert pf.contract_of(sff.CAPABILITY) is not None


def test_an_empty_board_violates_the_contract():
    """一个板块都没有，那就是没取到，别让它冒充成功。"""
    empty = sff.SectorFundFlowBoard(sectors=(), sector_type="industry", period="today")
    assert pf.contract_of(sff.CAPABILITY)(empty) is False
    assert pf.contract_of(sff.CAPABILITY)(_board()) is True


def test_a_wrong_shape_violates_the_contract():
    assert pf.contract_of(sff.CAPABILITY)({"传媒": 1}) is False


# --- 分级：只排同一层 --------------------------------------------------------


def test_a_board_without_levels_says_so():
    assert _board().levels_known is False
    assert _board(levels=(1, 2, 3)).levels_known is True


def test_only_one_level_is_ranked():
    """父子同时上榜就是同一笔钱数两遍。实测过：电子 -817.95亿 里含着半导体 -602.46亿。"""
    board = _board(levels=(1, 2, None), level_scheme="申万")
    two = _render_sector_fund_flow(board, top=5)
    assert "半导体" in two          # 二级，默认排它
    assert "传媒" not in two        # 一级，不和二级混排
    assert "申万二级行业" in two and "源共 3 个" in two

    one = _render_sector_fund_flow(board, top=5, level=1)
    assert "传媒" in one and "半导体" not in one
    assert "申万一级行业" in one


def test_the_default_level_matches_what_eastmoney_shows():
    """东财官网的行业板块资金流排行 3 页 50 行全是申万二级，一个一级都没有。

    排一级不算错，但和用户在东财、券商 App 上看到的那张榜对不上——对不上就得
    解释，解释不清会被当成数据错了。
    """
    assert stx.DEFAULT_RANK_LEVEL == 2
    assert stx.RANK_LEVELS == (1, 2)


def test_an_unlevelled_industry_board_ranks_everything_and_warns():
    """行业本来有层级，这次没分出来——照样出结果，但必须说清这一榜可能有重复。"""
    text = _render_sector_fund_flow(_board(levels_applicable=True), top=5)
    assert "传媒" in text and "半导体" in text
    assert "分级表取不到" in text and "数两遍" in text


def test_a_type_without_any_hierarchy_is_not_warned_about():
    """概念板块天然没有层级，那不是缺失，不该报警吓人。"""
    text = _render_sector_fund_flow(_board(levels_applicable=False), top=5)
    assert "传媒" in text and "半导体" in text
    assert "备注" not in text


def test_the_taxonomy_only_covers_industries():
    """概念和地域没有分级，申万也不分它们——连请求都不该发。"""
    platform = pf.get("shenwan")
    assert platform.supports(stx.CAPABILITY,
                             stx.SectorTaxonomyRequest(sector_type="industry")) is True
    assert platform.supports(stx.CAPABILITY,
                             stx.SectorTaxonomyRequest(sector_type="concept")) is False


def test_a_name_outside_the_scheme_has_no_level():
    """名单里没有 ≠ 没有层级，是"这个标准不认它"。猜一个会把它排进错的层。"""
    taxonomy = stx.SectorTaxonomy(levels={"传媒": 1, "证券Ⅱ": 2}, scheme="shenwan")
    assert taxonomy.level_of("传媒") == 1
    assert taxonomy.level_of("证券Ⅲ") is None
    assert taxonomy.names_at(1) == frozenset({"传媒"})


# --- 数据日期 ---------------------------------------------------------------


def test_the_as_of_date_is_the_last_open_session():
    """周末查出来必须是上一个交易日，不能写"今日"。"""
    saturday = datetime.datetime(2026, 9, 5, 15, 11)
    assert sff._as_of(saturday) == datetime.date(2026, 9, 4)


def test_before_the_open_the_date_is_still_the_previous_session():
    """交易日 08:00 还没开盘，拿到的仍是昨天那批。"""
    before_open = datetime.datetime(2026, 9, 4, 8, 0)
    assert sff._as_of(before_open) < datetime.date(2026, 9, 4)


def test_after_the_open_the_date_is_today():
    trading = datetime.datetime(2026, 9, 4, 10, 30)
    assert sff._as_of(trading) == datetime.date(2026, 9, 4)


def test_the_title_carries_the_date_not_a_relative_word():
    text = _render_sector_fund_flow(_board(), top=2)
    assert "2026-09-04" in text.splitlines()[0]
    assert "今日" not in text


# --- 降级源：字段少一半，但要标出来 ------------------------------------------


def test_the_degraded_source_serves_every_period():
    """三个口径 dataapi 都能给——key 选哪个字段就是哪个口径。"""
    platform = pf.get("eastmoney_dataapi")
    for period in sff.PERIODS:
        assert platform.supports(
            sff.CAPABILITY, sff.SectorFundFlowRequest(period=period)) is True


@pytest.mark.parametrize("period,field", [
    ("today", "f62"), ("5d", "f164"), ("10d", "f174"),
])
def test_each_period_asks_for_its_own_field(period, field):
    """口径和字段号必须配对。

    这条是补的：字段号一度写死成 f174（10 日），于是"当日"拿到的是 10 日的数——
    2026-09-05 实测传媒 当日 61.74亿 / 5日 65.46亿 / 10日 68.52亿，报告上标着
    "当日"的是 68.52亿。错配不报错、不缺数，只会安静地换一个口径。
    """
    url = pf.get("eastmoney_dataapi").url_for("industry", period)
    assert f"key={field}" in url


def test_a_wrong_sector_type_is_not_served():
    platform = pf.get("eastmoney_dataapi")
    assert platform.supports(
        sff.CAPABILITY, sff.SectorFundFlowRequest(sector_type="不存在")) is False


def test_the_degraded_render_drops_columns_instead_of_showing_blanks():
    """少画几列，好过画一堆空格子让人以为数据丢了。"""
    wide = _render_sector_fund_flow(_board(levels=(1, 1, 1), level_scheme="申万"), top=2)
    narrow = _render_sector_fund_flow(
        _board(partial=True, source="eastmoney_dataapi", levels=(1, 1, 1),
               level_scheme="申万"), top=2)

    def header(text):
        return next(line for line in text.splitlines() if line.startswith("| 板块 |"))

    assert "主力净流入最大股" in header(wide) and "涨跌幅" in header(wide)
    assert header(narrow) == "| 板块 | 主力净流入 |"
    assert "备注：降级源" in narrow
    assert "备注" not in wide


# --- 渲染 -------------------------------------------------------------------


def test_inflow_and_outflow_are_ranked_separately():
    text = _render_sector_fund_flow(_board(), top=5)
    inflow = text.index("净流入前")
    outflow = text.index("净流出前")
    assert inflow < text.index("传媒") < outflow
    assert outflow < text.index("半导体")


def test_a_flat_sector_lands_in_neither_list():
    """净额为 0 既不是流入也不是流出，塞进任何一边都是误导。"""
    text = _render_sector_fund_flow(_board(), top=5)
    assert "持平板块" not in text


def test_amounts_are_rendered_in_chinese_units():
    text = _render_sector_fund_flow(_board(), top=2)
    assert "61.74亿" in text and "-602.00亿" in text


def test_the_scope_line_has_a_fixed_shape():
    """脚注是唯一能追溯"这个数字凭什么"的地方，格式定死才追得动。"""
    line = next(l for l in _render_sector_fund_flow(_board(), top=2).splitlines()
                if l.startswith("- 口径："))
    assert line == "- 口径：2026-09-04 | 当日 | 覆盖 3 个行业板块 | 数据源：eastmoney"


# --- 缓存 -------------------------------------------------------------------
#
# 缓存的是上游那份 board，不是渲染结果——top/level 只影响渲染，按报告缓存会有
# 900 个 key，把 512 条的上限撑爆。


@pytest.fixture
def live_cache(monkeypatch):
    """开一份真缓存。conftest 默认把缓存关掉了，这几条要的正是它开着的行为。"""
    from finmcp import cache as cache_module

    cache = cache_module.ReportCache(enabled=True, disk_enabled=False, live_ttl_seconds=60)
    cache_module.set_report_cache(cache)
    yield cache
    cache_module.set_report_cache(
        cache_module.ReportCache(enabled=False, disk_enabled=False))


def _stub_platform(monkeypatch, calls, *, partial=False):
    """记账用的假平台，数一共问了上游几次。"""
    class Counting(pf.Platform):
        name = label = "counting"
        capabilities = frozenset({sff.CAPABILITY})

        def fetch_sector_fund_flow(self, request):
            calls.append((request.sector_type, request.period))
            return sff.SectorFundFlowBoard(
                sectors=(sff.SectorFlow(name="传媒", main_net=1e9),),
                sector_type=request.sector_type, period=request.period,
                source=self.name, partial=partial,
            )

    pf.register(Counting(), replace=True)
    monkeypatch.setenv("SECTOR_FUND_FLOW_PROVIDERS", "counting")
    monkeypatch.setenv("SECTOR_TAXONOMY_PROVIDERS", "off")
    return lambda: pf.unregister("counting")


def test_a_repeat_query_does_not_hit_upstream_again(monkeypatch, live_cache):
    calls: list = []
    cleanup = _stub_platform(monkeypatch, calls)
    try:
        first = sff.resolve(sff.SectorFundFlowRequest())
        second = sff.resolve(sff.SectorFundFlowRequest())
        assert len(calls) == 1, "第二次应该命中缓存，不该再问上游"
        assert second is not None and second.sectors == first.sectors
        assert second.as_of == first.as_of
    finally:
        cleanup()


def test_different_periods_do_not_share_an_entry(monkeypatch, live_cache):
    """口径进 key，否则 5 日会读到当日的数——正是上一个 bug 的形状。"""
    calls: list = []
    cleanup = _stub_platform(monkeypatch, calls)
    try:
        for period in ("today", "5d", "10d"):
            sff.resolve(sff.SectorFundFlowRequest(period=period))
        assert [c[1] for c in calls] == ["today", "5d", "10d"]
    finally:
        cleanup()


def test_a_degraded_result_is_never_cached(monkeypatch, live_cache):
    """非交易日一个纪元长达 64 小时，缓住降级源就是整个周末都只有两列。

    不缓的代价是下次重试一遍主源——正是想要的：主源一恢复就能拿到全字段。
    """
    calls: list = []
    cleanup = _stub_platform(monkeypatch, calls, partial=True)
    try:
        sff.resolve(sff.SectorFundFlowRequest())
        sff.resolve(sff.SectorFundFlowRequest())
        assert len(calls) == 2
    finally:
        cleanup()


def test_the_cache_switch_turns_this_off_too(monkeypatch):
    """REPORT_CACHE_ENABLED=0 必须把这一层也关掉，否则等价性证明是假的。"""
    calls: list = []
    cleanup = _stub_platform(monkeypatch, calls)
    try:
        sff.resolve(sff.SectorFundFlowRequest())
        sff.resolve(sff.SectorFundFlowRequest())
        assert len(calls) == 2
    finally:
        cleanup()


def test_a_corrupt_cache_entry_is_ignored_not_fatal(live_cache):
    """旧版本写下的条目形状可能不一样，不该让这次查询挂掉。"""
    assert sff._from_payload({"sectors": [{"没有这个字段": 1}]}) is None
    assert sff._from_payload({}) is None
    assert sff._from_payload("不是字典") is None


def test_only_the_primary_source_gets_a_breaker():
    """降级源不装熔断：装了只是把"少两列"变成"整层没有"，换不到东西。"""
    assert sff._breaker_for("eastmoney") is not None
    assert sff._breaker_for("eastmoney_dataapi") is None


def test_the_breaker_skips_a_dead_primary(monkeypatch):
    """主源挂了要拦住，否则每次调用都先付一遍它的超时。

    这一层格外需要：降级源的结果按设计不进缓存，没有熔断就是每次都重试死主源。
    """
    dead: list = []

    class Dead(pf.Platform):
        name, label = "eastmoney", "东财"
        capabilities = frozenset({sff.CAPABILITY})

        def fetch_sector_fund_flow(self, request):
            dead.append(1)
            raise ConnectionError("push2 不通")

    class Backup(pf.Platform):
        name, label = "eastmoney_dataapi", "东财(dataapi)"
        capabilities = frozenset({sff.CAPABILITY})

        def fetch_sector_fund_flow(self, request):
            return sff.SectorFundFlowBoard(
                sectors=(sff.SectorFlow(name="传媒", main_net=1e9),),
                sector_type=request.sector_type, period=request.period,
                source=self.name, partial=True)

    real_dead, real_backup = pf.get("eastmoney"), pf.get("eastmoney_dataapi")
    sff._breakers.clear()
    pf.register(Dead(), replace=True)
    pf.register(Backup(), replace=True)
    monkeypatch.setenv("SECTOR_TAXONOMY_PROVIDERS", "off")
    try:
        for _ in range(6):
            assert sff.resolve(sff.SectorFundFlowRequest()) is not None
        # 阈值 3 次之后就该跳过，不该 6 次全打
        assert len(dead) < 6, f"死掉的主源被打了 {len(dead)} 次，熔断没生效"
    finally:
        sff._breakers.clear()
        pf.register(real_dead, replace=True)
        pf.register(real_backup, replace=True)
