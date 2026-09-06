"""东方财富。

这个文件放板块资金流和个股/指数资金流。K 线和基本数据还散在 cn_stock_source 里走
efinance/AkShare，迁过来是后面的阶段——**先接新能力、再迁老能力**，这样每一步都能
单独验证。

## 个股/指数资金流：两台主机，一主一备

主：``push2his.eastmoney.com/api/qt/stock/fflow/daykline/get``（经 AkShare 封装），
``lmt=0`` 给全部历史。走伪装通道，被拒时整维没有。

备：``push2delay.eastmoney.com`` 上同一个接口。**只回最近一天**，``lmt`` 给多少都一样；
但它不在伪装通道的接管名单里，push2his 拒绝出口 IP 的同一时刻它仍应答（2026-09-06
本机实测，1.000688 / 1.600519 / 0.399006 的当日行和 push2his 逐字节相同）。它存在的
理由是覆盖浏览器页面兜底够不到的标的——科创 50 这类指数没有资金流向页面，主源一次
``RemoteDisconnected`` 就整维缺失（部署机 09-06 14:35 那轮的 3 项缺失全在它身上）。
只有一行，所以 ``complete=False``：有页面的标的编排层还会去页面把 120 行历史补回来。

## 板块资金流：两个端点，一主一备

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
from ..fund_flow_source import FundFlowHistory
from ..sector_fund_flow import SectorFlow, SectorFundFlowBoard

logger = logging.getLogger("finmcp")

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


#: 个股资金流接口 ``fields2`` 的 15 个字段，按位置对应的列名。和 AkShare 的
#: ``stock_individual_fund_flow`` 一字不差——归一到同一套列名是两台主机能互为
#: 备份的前提。最后两位是接口保留位，AkShare 也丢掉。
_FUND_FLOW_RAW_COLUMNS = [
    "日期",
    "主力净流入-净额", "小单净流入-净额", "中单净流入-净额", "大单净流入-净额", "超大单净流入-净额",
    "主力净流入-净占比", "小单净流入-净占比", "中单净流入-净占比", "大单净流入-净占比", "超大单净流入-净占比",
    "收盘价", "涨跌幅", "-", "-",
]
_FUND_FLOW_COLUMNS = [
    "日期", "收盘价", "涨跌幅",
    "主力净流入-净额", "主力净流入-净占比",
    "超大单净流入-净额", "超大单净流入-净占比",
    "大单净流入-净额", "大单净流入-净占比",
    "中单净流入-净额", "中单净流入-净占比",
    "小单净流入-净额", "小单净流入-净占比",
]
_FUND_FLOW_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
_DELAY_FUND_FLOW_URL = "https://push2delay.eastmoney.com/api/qt/stock/fflow/daykline/get"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36")


def _fund_flow_frame(klines: list):
    """``fflow/daykline`` 的 ``klines`` 字符串 → AkShare 列名的表。

    每一步照 ``ak.stock_individual_fund_flow`` 做（拆逗号、按位置命名、选 13 列、
    日期转 date、其余 ``to_numeric``），两台主机给的表才会逐字相同。
    """
    import pandas as pd

    frame = pd.DataFrame([item.split(",") for item in klines])
    frame.columns = _FUND_FLOW_RAW_COLUMNS
    frame = frame[_FUND_FLOW_COLUMNS]
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce").dt.date
    for column in _FUND_FLOW_COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


class EastmoneyPlatform(pf.Platform):
    name, label = "eastmoney", "东财"
    capabilities = frozenset({"sector_fund_flow", "fund_flow"})

    def degraded(self) -> bool:
        # 伪装通道一进冷却，push2 的请求就退回原生 requests，而它被接管的理由正是
        # 拒绝原生 requests——不用数失败次数也知道必败，直接让位给下一个平台。
        from ..http_channel import impersonated_hosts_degraded

        return impersonated_hosts_degraded()

    def fetch_fund_flow(self, request) -> Optional[FundFlowHistory]:
        """push2his 的全部历史，经 AkShare。入参写法和迁移前那次调用完全一样。"""
        import akshare as ak

        frame = ak.stock_individual_fund_flow(stock=request.code, market=request.exchange)
        if frame is None or frame.empty:
            return None
        return FundFlowHistory(frame=frame, complete=True)

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


#: 本项目的口径 → 东财的主力净额字段号。三个口径在 push2 和 dataapi 两个端点上是
#: 同一套字段号：push2 用它做 ``fid0``，dataapi 用它做 ``key``，返回值也以它为键。
#:
#: 这张表曾经写死成 f174，代价是**当日口径拿到的其实是 10 日的数**——2026-09-05
#: 实测传媒：当日 61.74亿、5日 65.46亿、10日 68.52亿，报告上写着"当日"的是 68.52亿。
#: 一个字段号错配不会报错、不会缺数，只会安静地给出另一个口径的值。
_PERIOD_FIELD = {"today": "f62", "5d": "f164", "10d": "f174"}


class EastmoneyDataApiPlatform(pf.Platform):
    """东财 dataapi 端点。只给板块名和主力净额，但在 push2 连不上时它还通。

    三个口径都支持——``key`` 选哪个字段就是哪个口径，和 push2 用的是同一套字段号。
    """

    name, label = "eastmoney_dataapi", "东财(dataapi)"
    capabilities = frozenset({"sector_fund_flow"})

    def supports(self, capability: str, request) -> bool:
        return request.sector_type in _SECTOR_T and request.period in _PERIOD_FIELD

    @staticmethod
    def url_for(sector_type: str, period: str) -> str:
        """这次请求的 URL。单拎出来是为了能不联网就验字段号配对。"""
        return ("https://data.eastmoney.com/dataapi/bkzj/getbkzj"
                f"?key={_PERIOD_FIELD[period]}&code=m%3A90%2Bt%3A{_SECTOR_T[sector_type]}")

    def fetch_sector_fund_flow(self, request) -> Optional[SectorFundFlowBoard]:
        import json

        import requests

        field = _PERIOD_FIELD[request.period]
        response = requests.get(
            self.url_for(request.sector_type, request.period), timeout=15, headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/145.0.0.0 Safari/537.36"),
            })
        payload = json.loads(response.text)
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        if not diff:
            return None
        # 取值用的字段号必须和请求时那个是同一个变量，不能各写各的——写岔了就是
        # 上面那种"标着当日、其实是 10 日"的错，而且悄无声息。
        sectors = tuple(
            SectorFlow(name=str(item.get("f14", "")), code=str(item.get("f12", "")),
                       main_net=float(item[field]) if item.get(field) is not None else None)
            for item in diff
        )
        return SectorFundFlowBoard(
            sectors=sectors, sector_type=request.sector_type,
            period=request.period, source=self.name, partial=True,
        )


class EastmoneyDelayPlatform(pf.Platform):
    """push2delay 主机上的个股资金流接口。只有最近一天，但主源被拒时它还通。

    不在伪装通道的接管名单里，所以 ``degraded()`` 保持默认的 False——伪装通道冷却
    与它无关。``complete=False`` 让编排层知道这只是当日一行。
    """

    name, label = "eastmoney_delay", "东财(delay)"
    capabilities = frozenset({"fund_flow"})

    @staticmethod
    def _get(secid: str) -> dict:
        """发一次请求。单拎出来是为了测试能不联网注入响应。"""
        import time

        import requests

        response = requests.get(
            _DELAY_FUND_FLOW_URL,
            params={
                "lmt": "0", "klt": "101", "secid": secid,
                "fields1": "f1,f2,f3,f7", "fields2": _FUND_FLOW_FIELDS,
                "ut": "b2884a393a59ad64002292a3e90d46a5", "_": int(time.time() * 1000),
            },
            headers={"User-Agent": _UA},
            timeout=15,
        )
        return response.json()

    def fetch_fund_flow(self, request) -> Optional[FundFlowHistory]:
        payload = self._get(request.secid)
        klines = ((payload or {}).get("data") or {}).get("klines") or []
        if not klines:
            return None
        return FundFlowHistory(frame=_fund_flow_frame(klines), complete=False)


pf.register(EastmoneyPlatform())
pf.register(EastmoneyDataApiPlatform())
pf.register(EastmoneyDelayPlatform())
