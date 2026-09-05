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


def test_only_the_top_level_is_ranked():
    """父子同时上榜就是同一笔钱数两遍。实测过：电子 -817.95亿 里含着半导体 -602.46亿。"""
    text = _render_sector_fund_flow(
        _board(levels=(1, 2, None), level_scheme="申万"), top=5)
    assert "传媒" in text          # 一级，进榜
    assert "半导体" not in text    # 二级，不和一级混排
    assert "申万一级行业" in text and "源共 3 个" in text


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


def test_the_degraded_source_only_serves_today():
    """dataapi 没有 5 日/10 日口径，与其返回口径不对的数据，不如让位。"""
    platform = pf.get("eastmoney_dataapi")
    assert platform.supports(sff.CAPABILITY,
                             sff.SectorFundFlowRequest(period="today")) is True
    assert platform.supports(sff.CAPABILITY,
                             sff.SectorFundFlowRequest(period="5d")) is False


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
