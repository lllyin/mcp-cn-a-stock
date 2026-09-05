"""兜底日历：周一到周五算交易日。

它**不知道节假日**——这不是缺陷，是它存在的全部意义：把"取不到日历时怎么办"从
散在四处的 ``weekday() < 5`` 变成配置里看得见、能单独关掉、能单独测试的一行
（``TRADING_CALENDAR_PROVIDERS=sina,weekday``）。

排在配置末位，只要前面任何一个平台给出真名单就轮不到它。
"""

from __future__ import annotations

import datetime

from .. import platform as pf


class WeekdayPlatform(pf.Platform):
    name, label = "weekday", "周一至周五"
    capabilities = frozenset({"trading_calendar"})

    #: 只是个"足够远"的边界，不代表这份日历对那天真的有效。空的 days 集合才是
    #: 它的自我声明：判断函数看到 days 为空就退回按星期算。
    _HORIZON = datetime.date(2099, 12, 31)

    def fetch_trading_calendar(self, request):
        from ..trading_calendar import Calendar

        return Calendar(days=frozenset(), covers_through=self._HORIZON, source=self.name)


pf.register(WeekdayPlatform())
