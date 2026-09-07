"""交易日历的公共入口。**要判"这天开不开市"的代码都从这里进。**

和 ``market_session``（现在是盘中还是收盘后）平级：那个管一天之内的时段，这个管
"这一天算不算交易日"。两者一起构成全项目关于时间的唯一事实来源。

## 用法

    from finmcp import market_calendar

    market_calendar.is_trading_day(day)                 # 这天开不开市
    market_calendar.previous_trading_day(day)           # 上一个交易日（不含当天）
    market_calendar.next_trading_day(day)               # 下一个交易日（不含当天）
    market_calendar.trading_days(start, end)            # 闭区间内的交易日
    market_calendar.missing_trading_days(a, b)          # 开区间内还剩几个交易日
    market_calendar.load()                              # 名单本身，带来源和覆盖边界

**任何地方再写 ``weekday() < 5`` 都是 bug。** 那个写法曾经散在四处，代价见
``datasource/trading_calendar`` 的模块文档：周六为不存在的交易日拉起 Chromium 抓了
66 次页面、国庆整周把报告缓存退化成 30 秒 TTL。

## 为什么实现不在这个文件里

取数那一半（sina / holiday-cn / 按星期兜底）是一个 **capability**，走的是全项目
统一的"平台 → 能力 → 归一"注册表（见 docs/architecture.md），所以它必须住在
``datasource/`` 下面。搬到包根会形成真的循环导入：``finmcp/datasource/__init__.py``
在导入期就实例化 ``CNStockDataSource()``，而 ``cn_stock_source → kline_source →
trading_calendar``。

所以这里只做门面：**一个稳定的公开路径，加一份给调用方看的文档**。后面要把它做成
MCP tool，也从这个文件长出去。

约束一条：``datasource/`` 下面的代码**不要 import 这个模块**，直接用
``from . import trading_calendar``。门面只给上层用；让底层反过来依赖它就会把上面
那个循环引回来。有测试盯着这条（``test_market_calendar.py``）。
"""

from __future__ import annotations

from .datasource.trading_calendar import (
    CAPABILITY,
    Calendar,
    CalendarRequest,
    DEFAULT_PROVIDER_ORDER,
    PROVIDER_ORDER_ENV,
    is_trading_day,
    load,
    missing_trading_days,
    next_trading_day,
    previous_trading_day,
    reset_cache,
    trading_days,
)

__all__ = [
    "CAPABILITY",
    "Calendar",
    "CalendarRequest",
    "DEFAULT_PROVIDER_ORDER",
    "PROVIDER_ORDER_ENV",
    "is_trading_day",
    "load",
    "missing_trading_days",
    "next_trading_day",
    "previous_trading_day",
    "reset_cache",
    "trading_days",
]
