"""板块资金流：契约、降级形态、渲染。

这一维回答个股资金流答不了的问题——"茅台主力净流入 3.68亿"没有语境，是整个板块
在被买还是只有它。
"""

from __future__ import annotations

import pytest

from finmcp.datasource import platform as pf
from finmcp.datasource import sector_fund_flow as sff
from finmcp.mcp_app import _render_sector_fund_flow


def _board(partial=False, source="eastmoney"):
    return sff.SectorFundFlowBoard(
        sectors=(
            sff.SectorFlow(name="传媒", change_pct=2.32, main_net=6.174e9,
                           main_pct=7.24, xl_net=4.36e9, l_net=1.81e9, leader="天娱数科"),
            sff.SectorFlow(name="半导体", change_pct=-1.2, main_net=-6.02e10,
                           main_pct=-3.1, xl_net=-4e10, l_net=-2e10, leader="某某股份"),
            sff.SectorFlow(name="持平板块", change_pct=0.0, main_net=0.0),
        ),
        sector_type="industry", period="today", source=source, partial=partial,
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
    wide = _render_sector_fund_flow(_board(), top=2)
    narrow = _render_sector_fund_flow(_board(partial=True, source="eastmoney_dataapi"), top=2)
    def header(text):
        return next(line for line in text.splitlines() if line.startswith("| 板块 |"))

    assert "主力净流入最大股" in header(wide) and "涨跌幅" in header(wide)
    # 只比表头：降级那份的告警行里本来就会提到"涨跌幅…取不到"，拿全文判会误伤。
    assert header(narrow) == "| 板块 | 主力净流入 |"
    assert "⚠️" in narrow and "降级源" in narrow
    assert "⚠️" not in wide


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


def test_the_footer_says_where_it_came_from():
    text = _render_sector_fund_flow(_board(), top=2)
    assert "覆盖 3 个行业板块" in text and "来源：eastmoney" in text
