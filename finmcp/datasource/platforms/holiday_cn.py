"""holiday-cn：国务院节假日安排，换算成交易日名单。

排在 ``sina`` 之后、``weekday`` 之前，补的是「取不到交易所名单」和「只能按星期猜」
之间那一大段落差。这段落差是量出来的——2024-01-02 .. 2026-12-31 共 728 个候选日，
拿 sina 的权威名单当答案：

    holiday_cn  一致 727/728 = 99.863%
    weekday     错 56 天（春节、国庆整段被算成交易日）

也就是说：**直接退到 weekday 会错 56 天，加这一级把其中 55 天救回来。**

## 数据源

    https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/<年份>.json

每年一个文件，约 5 KiB，裸 GET 无鉴权。仓库由国务院公告驱动更新，每条形如
``{"name": "春节", "date": "2026-02-17", "isOffDay": true}``。

## 换算规则：调休上班日必须忽略

``isOffDay=False`` 是**调休上班日**——劳动法上算工作日，但**交易所照旧不开**
（它们全都落在周末）。所以规则只有一条:

    交易日 = 周一到周五 且 不在 isOffDay=True 的集合里

把调休上班日当成交易日是这个源最容易踩的错，而且错得很隐蔽——报告里会凭空多出
几根 K 线，每一根的数值都在合理区间。实测 2025+2026 共 11 个调休上班日，本模块
误判 0 个；而且它们**全部落在周末**，所以「周一到周五」那一条本身就已经排除了它们。

``trading_calendar`` 的文档从一开始就点了这件事——「节假日历解决不了这件事：它不知道
调休上班日不开市」。那句话针对的是「直接拿节假日历当交易日历」，不是「节假日历没用」：
换算做对了，它是个好源。

## 唯一那个已知差异：交易所比国务院多休

上面 728 天里唯一对不上的是 **2024-02-09（周五，除夕）**：国务院当年的安排把除夕
定为工作日（鼓励放假），而**沪深交易所那天休市**。这一类"交易所比国务院多休"的日子
不在放假安排里，这个源会把它算成交易日。

判据上这是可以接受的：它只在 sina 已经取不到时才轮得上，那时候的对照不是权威名单
而是 weekday，而 weekday 在这 728 天里错 56 个。用 1 个换 55 个。
"""

from __future__ import annotations

import datetime
import json
import logging

from .. import platform as pf

logger = logging.getLogger("finmcp")

_URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
#: 往前取几年。240 日均线要跨约一年，请求区间又会再往前推 20 天，所以当年加前两年
#: 足够盖住最长的窗口；再多取只是白付请求，而这个源本来就是降级路径。
_YEARS_BACK = 2
_TIMEOUT_SECONDS = 8


class HolidayCnPlatform(pf.Platform):
    name, label = "holiday_cn", "holiday-cn 节假日"
    capabilities = frozenset({"trading_calendar"})

    def fetch_trading_calendar(self, request):
        import requests

        from ..trading_calendar import Calendar

        today = datetime.date.today()
        # ``through`` 是调用方声明"要覆盖到哪天"，可能落在明年——那一年的文件在
        # 国务院公告发布前不存在，取不到就少一年，不是失败。
        last = max(today.year, getattr(getattr(request, "through", None), "year", today.year))
        years = range(today.year - _YEARS_BACK, last + 1)

        session = requests.Session()
        off_days: set = set()
        covered: list = []
        for year in years:
            try:
                response = session.get(_URL.format(year=year), timeout=_TIMEOUT_SECONDS)
                if response.status_code == 404:
                    # 那一年的放假安排还没公布。已经取到的年份照常用。
                    logger.debug("holiday-cn 没有 %s 年的文件（安排未公布）", year)
                    continue
                response.raise_for_status()
                payload = json.loads(response.text)
            except Exception as error:
                logger.warning("holiday-cn 取 %s 年失败: %s", year, error)
                continue
            days = payload.get("days")
            if not isinstance(days, list):
                logger.warning("holiday-cn %s 年正文没有 days 数组，跳过", year)
                continue
            added = 0
            for item in days:
                # 只收放假日。isOffDay=False 是调休上班日，交易所不开市，忽略它——
                # 当成交易日会让报告凭空多出几根 K 线。
                if not isinstance(item, dict) or not item.get("isOffDay"):
                    continue
                try:
                    off_days.add(datetime.date.fromisoformat(item["date"]))
                    added += 1
                except (KeyError, TypeError, ValueError):
                    continue
            if added:
                covered.append(year)

        if not covered:
            return None
        # 只声明真正取到了文件的那几年。covers_through 报得比实际大，会让边界外的
        # 查询把"名单里没有"误读成"那天不开市"（见 Calendar.covers_through）。
        first = datetime.date(min(covered), 1, 1)
        through = datetime.date(max(covered), 12, 31)
        trading = frozenset(
            day for day in _each_day(first, through)
            if day.weekday() < 5 and day not in off_days
        )
        if not trading:
            return None
        logger.debug(
            "holiday-cn 覆盖 %s 年，放假 %d 天，换出交易日 %d 天",
            ",".join(map(str, covered)), len(off_days), len(trading),
        )
        return Calendar(days=trading, covers_through=through, source=self.name)


def _each_day(start: datetime.date, end: datetime.date):
    cursor = start
    step = datetime.timedelta(days=1)
    while cursor <= end:
        yield cursor
        cursor += step


pf.register(HolidayCnPlatform())
