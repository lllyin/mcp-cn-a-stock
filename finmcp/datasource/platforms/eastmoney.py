"""东方财富。

这个文件放板块资金流和个股/指数资金流。K 线和基本数据还散在 cn_stock_source 里走
efinance/AkShare，迁过来是后面的阶段——**先接新能力、再迁老能力**，这样每一步都能
单独验证。

## 个股/指数资金流：两台主机，一主一备

主：``push2his.eastmoney.com/api/qt/stock/fflow/daykline/get``（经 AkShare 封装），
``lmt=0`` 给全部历史。走伪装通道，被拒时整维没有。

备：``push2delay.eastmoney.com`` 上同一个接口。**只回最近一天**，``lmt`` 给多少都一样；
但它不在伪装通道的接管名单里，push2his 拒绝出口 IP 的同一时刻它仍应答（2026-09-06
实测 1.000688 / 1.600519 / 0.399006 的当日行和 push2his 逐字节相同）。它存在的
理由是覆盖浏览器页面兜底够不到的标的——科创 50 这类指数没有资金流向页面，主源一次
``RemoteDisconnected`` 就整维缺失（09-06 14:35 那轮的 3 项缺失全在它身上）。
只有一行，所以 ``complete=False``：有页面的标的编排层还会去页面把 120 行历史补回来。

## 板块资金流：两个端点，一主一备

主：``push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:{1,2,3}``（经 AkShare 封装），
字段全——涨跌幅、主力净额和净占比、超大/大/中/小四档、领涨股。走本项目的伪装通道。

备：``data.eastmoney.com/dataapi/bkzj/getbkzj``，只给板块名和主力净额，但**它不在
伪装通道的接管名单里，push2 连不上的出口上它直连就通**（2026-09-05 实测 HTTP 200）。
少几列好过整层没有——所以它以 ``partial=True`` 的降级形态存在，而不是被丢掉。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ...config import MARKET_MAP_BUDGET_SECONDS
from .. import platform as pf
from ..fund_flow_source import FundFlowHistory
from ..realtime_fund_flow_source import RealtimeFundFlow
from ..market_map_source import MarketMap, MarketMapStock
from ..sector_fund_flow import SectorFlow, SectorFundFlowBoard

logger = logging.getLogger("finmcp")

#: 本项目的板块类型 → 东财 m:90 下的 t 值
_SECTOR_T = {"industry": "2", "concept": "3", "region": "1"}
#: 本项目的板块类型 → AkShare 的中文参数
_AK_SECTOR = {"industry": "行业资金流", "concept": "概念资金流", "region": "地域资金流"}
#: 本项目的口径 → AkShare 的中文参数
_AK_PERIOD = {"today": "今日", "5d": "5日", "10d": "10日"}


def _number(value):
    """把上游的一个字段值转成数。

    东财在没有数据时给的是字符串 ``"-"``（停牌、新股上市首日的市值），不是 null——
    直接 float() 会抛，而抛在翻页循环里会把整页丢掉。取不到就给 None：
    报告里"没有"和"0"是两回事。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


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
#: 分钟线：每分钟一行，最后一行是当日累计。字段位置与日线前六位相同：时刻、主力、小单、
#: 中单、大单、超大单；接口对 klt=1 不给净占比字段。zjlx 页面"今日"栏就是它填的。
_DELAY_MINUTE_FUND_FLOW_URL = "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get"
_MINUTE_FUND_FLOW_FIELDS = "f51,f52,f53,f54,f55,f56"
#: push2delay 的请求头。与 _PUSH2_HEADERS 用同一版 Chrome：旧版 UA 的请求会被
#: push2his 分档成"扰动副本"或直接拒连（见 http_channel._AUTH_IDENTITY_HEADERS 的注释），
#: 这里保持自洽的现代身份。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")


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


#: 云图用的字段号。f12 代码 / f13 市场（1 沪 0 深北）/ f14 名称 / f3 涨跌幅 /
#: f21 流通市值 / f6 成交额 / f100 所属行业。
#: 加权字段 → 上游字段号。请求和取值共用这张表，避免字段标签与数据错配。
_MARKET_MAP_SIZE_FIELD = {"float_cap": "f21", "turnover": "f6"}
#: 上游单页硬上限。2026-09-09 实测 pz 给 200/500/1000/2000/6000 一律只回 100 行，
#: 所以翻页次数是 ceil(total/100)，没法靠调大 pz 省掉。
_MARKET_MAP_PAGE_SIZE = 100
#: 一次取数最多翻几页。全 A 5911 只是 60 页；留到 80 页是给上市家数增长的余量。
#: 有这个上限是因为翻页的终止条件依赖上游的 total——total 要是回了个荒唐的大数，
#: 没有上限就会一直翻下去（AGENTS §三：任何 N × 单次超时的结构都要有总预算）。
_MARKET_MAP_MAX_PAGES = 80


#: 补齐一组普通浏览器会带的头。
#:
#: **这里刻意不带 Cookie。** push2 要求一个名为 nid18 的 Cookie 而且认值，只有页面
#: 里的 JS 写出来的那个放行——伪造一个同格式的随机值实测 1/6，和不带（2/8）没有
#: 区别。而带上它反而有害：出站通道那一层遵守"调用方自己给了 Cookie 就不覆盖"，
#: 于是一个假值会把真凭据挡在外面。凭据由 datasource/eastmoney_auth 统一采集和注入。
#:
#: 这些头保持普通浏览器形状；凭据由 datasource/eastmoney_auth 统一采集和注入。
_PUSH2_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/152.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


#: 上游在"没有行业"时给过的几种写法。退市股是 "-"，新股偶尔是空串。
_NO_SECTOR = {"", "-", "--", "null", "None"}


def _sector_of(raw) -> str:
    value = str(raw or "").strip()
    return "未分类" if value in _NO_SECTOR else value


def _prefixed(code: str, market) -> str:
    """带市场前缀的代码。东财把深市和北交所的 f13 都写成 0，需再看代码段。"""
    if code.startswith(("4", "8", "92")):
        return "BJ" + code
    return ("SH" if str(market) == "1" else "SZ") + code


class EastmoneyPlatform(pf.Platform):
    name, label = "eastmoney", "东财"
    capabilities = frozenset({"sector_fund_flow", "fund_flow", "market_map"})
    market_map_url = "https://push2.eastmoney.com/api/qt/clist/get"

    def degraded(self) -> bool:
        # 伪装通道冷却时通常跳过东财；有浏览器签发的凭据后，普通请求仍有成功机会。
        # 这里只覆盖本平台的 push2 / push2his 能力。K 线的独立熔断仍按原判据处理。
        from .. import eastmoney_auth
        from ..http_channel import impersonated_hosts_degraded

        return impersonated_hosts_degraded() and not eastmoney_auth.has_credential()

    def fetch_fund_flow(self, request) -> Optional[FundFlowHistory]:
        """push2his 的全部历史，经 AkShare。入参写法和迁移前那次调用完全一样。"""
        import akshare as ak

        frame = ak.stock_individual_fund_flow(stock=request.code, market=request.exchange)
        if frame is None or frame.empty:
            return None
        return FundFlowHistory(frame=frame, complete=True)


    def fetch_market_map(self, request) -> Optional[MarketMap]:
        """全市场（或某板块）逐只股票，分页取回。

        **取到几页就返回几页**，缺的页号记进 ``missing_pages``。静默丢掉一页就是
        悄悄给了个小 60 只的池子，而调用方会拿它当全集去算板块占比（AGENTS §一）。
        第一页就取不到才算整源失败——那时没有"部分"可言。
        """
        import json

        import requests

        size_field = _MARKET_MAP_SIZE_FIELD[request.size]
        fields = "f12,f13,f14,f2,f3,f6,f21,f100,f62,f184"
        stocks: list = []
        missing: list = []
        total = None
        page = 1
        deadline = time.monotonic() + MARKET_MAP_BUDGET_SECONDS
        while page <= _MARKET_MAP_MAX_PAGES:
            remaining = deadline - time.monotonic()
            if remaining < 1.0:
                if page == 1:
                    return None
                # total 已知时，把还没尝试的页也列入缺页；调用方才能知道返回的不是全集。
                last_page = min(
                    _MARKET_MAP_MAX_PAGES,
                    max(page, (int(total or 0) + _MARKET_MAP_PAGE_SIZE - 1)
                        // _MARKET_MAP_PAGE_SIZE),
                )
                missing.extend(range(page, last_page + 1))
                break
            try:
                response = requests.get(
                    self.market_map_url,
                    timeout=min(15, max(1.0, remaining)),
                    params={
                        "pn": page, "pz": _MARKET_MAP_PAGE_SIZE,
                        "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
                        "fs": request.selector, "fields": fields,
                        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    },
                    headers=_PUSH2_HEADERS,
                )
                payload = ((json.loads(response.text) or {}).get("data") or {})
                rows = payload.get("diff") or []
            except Exception:
                logger.warning("云图第 %s 页取不到 board=%s", page, request.board,
                               exc_info=True)
                if page == 1:
                    return None          # 一页都没有，让位给下一个源
                missing.append(page)
                rows = []
            else:
                if total is None:
                    total = payload.get("total") or 0
                if not rows:
                    break                # 正常翻完
            for row in rows:
                code = str(row.get("f12") or "")
                if not code:
                    continue
                stocks.append(MarketMapStock(
                    symbol=_prefixed(code, row.get("f13")),
                    name=str(row.get("f14") or ""),
                    change_pct=_number(row.get("f3")),
                    size=_number(row.get(size_field)),
                    # 行业缺失时不丢这只股票：它照样有涨跌幅和面积，只是归不了组。
                    # 丢掉会让"全市场"少几只而没人知道。
                    #
                    # 上游对退市股给的是字面的 "-"（不是 null，也不是空串），
                    # 直接用它会在报告里冒出一个叫 "-" 的行业。
                    sector=_sector_of(row.get("f100")),
                    last=_number(row.get("f2")),
                    float_cap=_number(row.get("f21")),
                    amount_yuan=_number(row.get("f6")),
                    main_net=_number(row.get("f62")),
                    main_pct=_number(row.get("f184")),
                ))
            if total and len(stocks) + len(missing) * _MARKET_MAP_PAGE_SIZE >= total:
                break
            page += 1
        if not stocks:
            return None
        warnings = []
        if missing:
            warnings.append(
                f"第 {'、'.join(str(p) for p in missing)} 页取不到，"
                f"本次少了约 {len(missing) * _MARKET_MAP_PAGE_SIZE} 只"
                + (f"（占 {len(missing) * _MARKET_MAP_PAGE_SIZE / total:.1%}）" if total else ""))
        if page > _MARKET_MAP_MAX_PAGES:
            warnings.append(f"翻页到达上限 {_MARKET_MAP_MAX_PAGES} 页，后面的没取")
        return MarketMap(
            stocks=tuple(stocks), board=request.board, size_field=request.size,
            upstream_total=total or len(stocks), missing_pages=tuple(missing),
            warnings=tuple(warnings),
        )

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
    capabilities = frozenset({"fund_flow", "realtime_fund_flow", "market_map"})
    market_map_url = "https://push2delay.eastmoney.com/api/qt/clist/get"

    def fetch_market_map(self, request) -> Optional[MarketMap]:
        """主集群不可用时从 delay 集群取得同口径列表。"""
        return EastmoneyPlatform.fetch_market_map(self, request)

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

    @staticmethod
    def _get_minutes(secid: str) -> dict:
        """分钟线一次请求。单拎出来是为了测试能不联网注入响应。"""
        import time

        import requests

        response = requests.get(
            _DELAY_MINUTE_FUND_FLOW_URL,
            params={
                "lmt": "0", "klt": "1", "secid": secid,
                "fields1": "f1,f2,f3,f7", "fields2": _MINUTE_FUND_FLOW_FIELDS,
                "ut": "b2884a393a59ad64002292a3e90d46a5", "_": int(time.time() * 1000),
            },
            headers={"User-Agent": _UA},
            timeout=15,
        )
        return response.json()

    def fetch_realtime_fund_flow(self, request) -> Optional[RealtimeFundFlow]:
        """分钟线最后一行 = 当日累计五档净流入。盘外分钟线是空的，返回 None 让调用方按暂无处理。"""
        payload = self._get_minutes(request.secid)
        data = (payload or {}).get("data") or {}
        klines = data.get("klines") or []
        if not klines:
            return None
        parts = str(klines[-1]).split(",")
        if len(parts) < 6:
            return None
        try:
            main, small, medium, large, xlarge = (float(x) for x in parts[1:6])
        except ValueError:
            return None
        return RealtimeFundFlow(
            time=parts[0], main_net=main, xl_net=xlarge, l_net=large, m_net=medium, s_net=small,
            name=str(data.get("name") or ""), source=self.name,
        )


pf.register(EastmoneyPlatform())
pf.register(EastmoneyDataApiPlatform())
pf.register(EastmoneyDelayPlatform())
