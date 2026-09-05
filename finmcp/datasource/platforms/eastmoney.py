"""东方财富。

这个文件目前只放板块资金流。个股那几维（K 线、基本数据、资金流）还散在
cn_stock_source 里走 efinance/AkShare，迁过来是后面的阶段——**先接新能力、
再迁老能力**，这样每一步都能单独验证。

## 两个端点，一主一备

主：``push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:{1,2,3}``（经 AkShare 封装），
字段全——涨跌幅、主力净额和净占比、超大/大/中/小四档、领涨股。走本项目的伪装通道。

备：``data.eastmoney.com/dataapi/bkzj/getbkzj``，只给板块名和主力净额，但**它不在
伪装通道的接管名单里，本机 push2 连不上时它直连就通**（2026-09-05 实测 HTTP 200）。
少几列好过整层没有——所以它以 ``partial=True`` 的降级形态存在，而不是被丢掉。
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import platform as pf
from ..sector_fund_flow import SectorFlow, SectorFundFlowBoard

logger = logging.getLogger("qtf_mcp")

#: 本项目的板块类型 → 东财 m:90 下的 t 值
_SECTOR_T = {"industry": "2", "concept": "3", "region": "1"}
#: 本项目的板块类型 → AkShare 的中文参数
_AK_SECTOR = {"industry": "行业资金流", "concept": "概念资金流", "region": "地域资金流"}
#: 本项目的口径 → AkShare 的中文参数
_AK_PERIOD = {"today": "今日", "5d": "5日", "10d": "10日"}


def _f(row, key):
    """取一个数值字段，取不到或不是数就给 None——报告里"没有"和"0"是两回事。"""
    import pandas as pd

    value = row.get(key)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class EastmoneyPlatform(pf.Platform):
    name, label = "eastmoney", "东财"
    capabilities = frozenset({"sector_fund_flow"})

    def degraded(self) -> bool:
        # 伪装通道一进冷却，push2 的请求就退回原生 requests，而它被接管的理由正是
        # 拒绝原生 requests——不用数失败次数也知道必败，直接让位给下一个平台。
        from ..http_channel import impersonated_hosts_degraded

        return impersonated_hosts_degraded()

    def fetch_sector_fund_flow(self, request) -> Optional[SectorFundFlowBoard]:
        import akshare as ak

        sector = _AK_SECTOR.get(request.sector_type)
        period = _AK_PERIOD.get(request.period)
        if sector is None or period is None:
            return None
        frame = ak.stock_sector_fund_flow_rank(indicator=period, sector_type=sector)
        if frame is None or frame.empty:
            return None
        prefix = period          # 列名前缀就是"今日"/"5日"/"10日"
        sectors = []
        for _, row in frame.iterrows():
            sectors.append(SectorFlow(
                name=str(row.get("名称", "")),
                change_pct=_f(row, f"{prefix}涨跌幅"),
                main_net=_f(row, f"{prefix}主力净流入-净额"),
                main_pct=_f(row, f"{prefix}主力净流入-净占比"),
                xl_net=_f(row, f"{prefix}超大单净流入-净额"),
                l_net=_f(row, f"{prefix}大单净流入-净额"),
                m_net=_f(row, f"{prefix}中单净流入-净额"),
                s_net=_f(row, f"{prefix}小单净流入-净额"),
                leader=str(row.get(f"{prefix}主力净流入最大股", "") or ""),
            ))
        return SectorFundFlowBoard(
            sectors=tuple(sectors), sector_type=request.sector_type,
            period=request.period, source=self.name,
        )


class EastmoneyDataApiPlatform(pf.Platform):
    """东财 dataapi 端点。字段少一半，但在 push2 连不上时它还通。

    只支持"今日"——这个端点没有 5 日/10 日口径。别的口径直接 supports() 返回 False，
    让上层继续找下一个平台，而不是返回一份口径不对的数据。
    """

    name, label = "eastmoney_dataapi", "东财(dataapi)"
    capabilities = frozenset({"sector_fund_flow"})

    def supports(self, capability: str, request) -> bool:
        return request.period == "today" and request.sector_type in _SECTOR_T

    def fetch_sector_fund_flow(self, request) -> Optional[SectorFundFlowBoard]:
        import json

        import requests

        t = _SECTOR_T[request.sector_type]
        url = ("https://data.eastmoney.com/dataapi/bkzj/getbkzj"
               f"?key=f174&code=m%3A90%2Bt%3A{t}")
        response = requests.get(url, timeout=15, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/145.0.0.0 Safari/537.36"),
        })
        payload = json.loads(response.text)
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        if not diff:
            return None
        sectors = tuple(
            SectorFlow(name=str(item.get("f14", "")), code=str(item.get("f12", "")),
                       main_net=float(item["f174"]) if item.get("f174") is not None else None)
            for item in diff
        )
        return SectorFundFlowBoard(
            sectors=sectors, sector_type=request.sector_type,
            period=request.period, source=self.name, partial=True,
        )


pf.register(EastmoneyPlatform())
pf.register(EastmoneyDataApiPlatform())
