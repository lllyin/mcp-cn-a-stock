"""同花顺。

接它进来只为一件事：**它是唯一一个免鉴权、又和东财口径一致的历史行情源**。

## 为什么需要第三家

创业板指 2026-09-04 的成交量，2026-09-05 用同花顺人工核对定案（5036.5亿 / 2亿手）：

    东财      200,462,500 手 / 5036.48亿    ✅
    同花顺    200,462,510 手 / 5036.48亿    ✅
    腾讯      193,413,042 手 / 4998.09亿    ✗ 低 3.52% / 0.76%
    新浪      193,413,042 手 /（无此字段）   ✗ 与腾讯一字不差

腾讯和新浪同源，互相校验不了。东财取不到时（push2 拒绝某些出口 IP 就是这种情况），
整条兜底链给出的创业板指成交量就是错的，而且各周期均量全都跟着错。同花顺补上的
正是这个缺口。

交叉验证做过：6 个标的 × 3 个交易日，同花顺与腾讯的成交额**除创业板指外逐位相同**，
成交量除单位外也相同。换上来不会在别处引入新错。

## 接口

    实时  https://d.10jqka.com.cn/v6/realhead/hs_<code>/last.js
    历史  https://d.10jqka.com.cn/v6/line/hs_<code>/01/{last|<年份>|all}.js

裸 GET，无 cookie、无 token、无浏览器——比本项目已有的那条同花顺路径（market_breadth
用 Playwright 有头浏览器拿 cookie）便宜一个量级。必须带 Referer 和 Accept，缺了
返回 0 字节。

历史返回 JSONP 包着的 ``日期,开,高,低,收,成交量(股),成交额(元),换手率,,,``，注意
**列序和别家不同**：这里是"开高低收"，腾讯是"开收高低"。归一在这个类里做完。

## 代码写法

深市直接用六位码（``hs_399006``），**沪市指数要用内部码**——上证 ``1A0001``、
科创50 ``1B0688``。踩过：``hs_000001`` 返回的是平安银行不是上证指数，``hs_000688``
返回的是个股不是科创50。这种"取到了但取错标的"比取不到危险得多，所以映射表宁可
写死也不猜。
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Optional

from ...config import KLINE_TONGHUASHUN_BUDGET_SECONDS
from .. import platform as pf
from ..kline_frame import _finalize_fallback_frame

logger = logging.getLogger("finmcp")

_BASE = "https://d.10jqka.com.cn/v6/line"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    # Referer 和 Accept 不能省；其余按普通浏览器导航补齐，减少年份文件间歇 502。
    "Referer": "https://stockpage.10jqka.com.cn/",
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

#: 沪市指数的内部码。深市指数和个股都直接用六位码，不进这张表。
#: 只列本项目 finmcp/confs/indices.json 里出现过的，加指数时同步加一行。
_SSE_INDEX_CODES = {
    "000001": "1A0001",   # 上证指数
    "000300": "1A0300",   # 沪深300
    "000016": "1A0016",   # 上证50
    "000688": "1B0688",   # 科创50
    "000905": "1A0905",   # 中证500
    "000852": "1A0852",   # 中证1000
}

#: 按年取，不取 all。实测三种粒度的体积（创业板指）：
#:
#:     last.js   11 KiB   约 140 根（7 个月）——算不了 240 日均量
#:     2026.js   13 KiB   年初至今
#:     all.js   147 KiB   3951 根，2010 年至今
#:
#: 报告要 240 日均量，跨度约一年，取两个年份文件（约 32 KiB）就够，比 all 少 4.7 倍。
#: 按年取还有个好处：跨年请求自然只多取一个文件，不会因为窗口长一天就翻倍。
_YEAR_URL = "{base}/hs_{code}/{segment}/{year}.js"

#: 年份文件 5xx 的重试次数（不含首次）。
#:
#: 5xx 抛出去是对的——静默 continue 会得到一条断裂的序列而链路不回退。但抛出去的
#: 代价是**整个源失败**，对指数就是整条 240 根序列换成腾讯口径、成交量低 3.5%，
#: 而报告里看不出来。一次瞬时 502 不该付这么大的代价。
#:
#: 判据是它确实瞬时：2026-09-08 机房出口的线上日志里同花顺 kline 6 成功 6 失败（**50%**），
#: 全部是 502，而且 08:13:26、08:17:55、08:17:56 三个时刻**同一秒内既有 502 又有成功**
#: ——同一个 2024.js 相隔 0.27 秒一次 502 一次 200。同一时刻家用宽带出口打 24 次
#: 全部 200，所以这是那个出口特有的间歇性拒绝，不是同花顺整体挂了。
#:
#: 只重试 1 次：502 返回很快（约 150ms），按独立失败率算一次重试把 50% 压到约 25%，
#: 再多一次的边际收益减半而每次失败都要多付一个请求。404 不重试——那是「未上市」
#: 或缺口，重试改变不了。
#:
#: 注意重试的代价不对称：502 很快返回，超时要付满 ``_REQUEST_TIMEOUT``、重试就是两倍。
#: 所以重试被 ``KLINE_TONGHUASHUN_BUDGET_SECONDS`` 关着，只能花预算剩下的部分。
_YEAR_FILE_RETRIES = 1

#: 单个年份文件的超时。和总预算的分工：每个文件最多等这么久，整个源最多等那么久。
_REQUEST_TIMEOUT = 15

#: 预算剩不到这么多秒就不再发请求——注定超时的请求只是把失败推迟，还占着一个连接。
_MIN_USEFUL_SLICE = 2.0


def tonghuashun_code(prefixed: str) -> Optional[str]:
    """带市场前缀的码（``sh600519``/``sz399006``/``bj920021``）→ 同花顺认的代码。

    历史行情和盘中行情两个端点用同一套代码规则，所以这一份只该有一处——照
    ``intraday_quote.tencent_code`` 的先例。认不出返回 None，调用方跳过这个源。
    """
    normalized = (prefixed or "").lower()
    market, digits = normalized[:2], normalized[2:]
    if market == "sh" and digits in _SSE_INDEX_CODES:
        return _SSE_INDEX_CODES[digits]
    if market == "sh" and digits.startswith("000"):
        # 沪市 000 开头一定是指数，但不在表里——宁可不给，也不要去问
        # hs_000xxx 拿回一只同名深市个股的数据。
        return None
    return digits or None


class TonghuashunPlatform(pf.Platform):
    name, label = "tonghuashun", "同花顺"
    capabilities = frozenset({"kline"})

    # 复权类型 → 接口路径里的那个两位数
    _ADJUST = {"qfq": "01", "hfq": "02", "none": "00"}

    def _code(self, request) -> Optional[str]:
        return tonghuashun_code(request.prefixed)

    def supports(self, capability: str, request) -> bool:
        return self._code(request) is not None

    def fetch_kline(self, request):
        import pandas as pd
        import requests

        code = self._code(request)
        segment = self._ADJUST.get(request.adjust, "01")
        end = datetime.datetime.strptime(request.end_date, "%Y-%m-%d").date()
        # 派生列要请求区间之前那个交易日的收盘价，所以起点再往前推 20 天；
        # 跨年时这一推可能多带一个年份，正是要的。
        cutoff = request.requested_start - datetime.timedelta(days=20)
        years = range(cutoff.year, end.year + 1)

        # 不动 trust_env：这个域名要不要走代理由部署环境决定。踩过一次——一开始
        # 写了 trust_env=False 想绕开一个坏掉的系统代理，结果那个环境的本地 DNS 根本
        # 解析不了 d.10jqka.com.cn（getaddrinfo 直接 gaierror），代理才是唯一能通的路，
        # 关掉等于把它彻底断了。别的平台都没有这一句，这个也不该有。
        session = requests.Session()

        # 整个源一次取数的总预算，见 config.KLINE_TONGHUASHUN_BUDGET_SECONDS。
        # 用 monotonic 而不是 time()：量的是流逝时间，NTP 拨一下时钟不该影响它。
        deadline = (
            time.monotonic() + KLINE_TONGHUASHUN_BUDGET_SECONDS
            if KLINE_TONGHUASHUN_BUDGET_SECONDS > 0 else None
        )

        rows = []
        for year in years:
            url = _YEAR_URL.format(base=_BASE, code=code, segment=segment, year=year)
            # 5xx 与「正文不是 JSONP」重试；404 不重试（未上市或缺口，重试无用）。
            for attempt in range(_YEAR_FILE_RETRIES + 1):
                timeout = _REQUEST_TIMEOUT
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining < _MIN_USEFUL_SLICE:
                        # 必须抛，不能拿 rows 凑一份返回：少了中间某一年就是**断裂的
                        # 序列**，而断裂的序列列是齐的、每个值都在合理区间，源"成功"
                        # 返回，涨跌幅跨缺口算、均线全错（SH000688 报成 +20.17% 就是
                        # 这么来的）。抛出去让链路落到腾讯，那里给的是完整序列。
                        raise RuntimeError(
                            f"同花顺取数用尽 {KLINE_TONGHUASHUN_BUDGET_SECONDS:.0f}s "
                            f"总预算（已取 {len(rows)} 行，还缺 {year} 年起）"
                        )
                    # 最后一个请求不许探出预算——否则预算只是个建议。
                    timeout = min(_REQUEST_TIMEOUT, remaining)
                response = session.get(url, headers=_HEADERS, timeout=timeout)
                body = response.text
                transient = response.status_code != 404 and (
                    response.status_code != 200 or not body or "(" not in body
                )
                if not transient or attempt == _YEAR_FILE_RETRIES:
                    break
                logger.debug(
                    "同花顺 %s 年文件 HTTP %s，重试第 %d 次 code=%s",
                    year, response.status_code, attempt + 1, code,
                )
            if response.status_code == 404:
                # 404 分两种，判据是"已经取到过行没有"——years 是升序遍历（旧→新）：
                #
                #   还没取到过行  → 那一年这个标的确实不存在。C马矿 2026-09-01 上市，
                #                  它的 2025 年文件就是 404 空正文。不是错，接着取下一年。
                #   已经取到过行  → 缺口。科创50 从 2020 年就有，它的 2025 年 404 只能是
                #                  取数失败，不可能是"未上市"。
                #
                # 中间年份的 404 静默跳过会得到一条**断裂**的序列，而链路不会回退——
                # 源"成功"了。断裂处的涨跌幅是跨缺口算的：实际发生过 SH000688 报成
                # +20.17%（真实 +2.41%），因为当天前面那一根变成了 1344.07。
                # 均线同时全错（240 日均价 1540 → 1148）。
                if rows:
                    raise RuntimeError(
                        f"同花顺 {year} 年文件 404，但 {rows[0]['日期']} 起已有数据——"
                        "这是缺口不是未上市"
                    )
                logger.debug("同花顺 %s 年文件不存在（未上市）code=%s", year, code)
                continue
            if response.status_code != 200 or not body or "(" not in body:
                # 5xx 或正文不是 JSONP 是取数失败，不是"那年没有"。以前这里 continue：
                # 2026-09-07 实测 2024、2025 两年文件 502 被静默跳过，科创50 只剩 2026 年
                # 164 根，报告少了 240 日五行且无日志；若失败的是当年文件，序列会停在去年、
                # 均线全部算错。抛出去让链路落到下一个源，数据由腾讯补齐。
                raise RuntimeError(
                    f"同花顺 {year} 年文件 HTTP {response.status_code}"
                    + ("" if body and "(" in body else "，正文不是 JSONP")
                    + f"（已重试 {_YEAR_FILE_RETRIES} 次）"
                )
            payload = json.loads(body[body.index("(") + 1: body.rindex(")")])
            rows.extend(self._rows(payload))
        if not rows:
            return None
        frame = pd.DataFrame(rows).sort_values("日期").reset_index(drop=True)
        frame = frame[(frame["日期"] >= cutoff) & (frame["日期"] <= end)]
        if frame.empty:
            return None
        is_index = request.prefixed[2:].startswith(("000", "399", "899"))
        if is_index:
            # 同花顺的单位按标的类型分两套：**指数给股、个股和 ETF 给手**。
            # 下游的单位推断对指数是直接跳过的（指数的"收盘"是点位不是股价，
            # 成交额/点位 算不出股数），所以指数这一档必须在这里换算好，否则成交量
            # 会整整大 100 倍——而两个数都"看着像成交量"，肉眼查不出来。
            # 对照：创业板指 2026-09-04 原始 20,046,251,000 股 = 200,462,510 手，
            # 与东财/同花顺人工核对的 200,462,500 手一致。
            frame = frame.copy()
            frame["成交量"] = frame["成交量"] / 100
        return _finalize_fallback_frame(
            frame.reset_index(drop=True), request.code,
            request.requested_start, self.label,
            is_index=is_index,
        )

    @staticmethod
    def _rows(payload) -> list:
        """把一年的 JSONP 正文拆成标准列的行。"""
        out = []
        for raw in (payload.get("data") or "").split(";"):
            parts = raw.split(",")
            if len(parts) < 7:
                continue
            try:
                day = datetime.datetime.strptime(parts[0], "%Y%m%d").date()
            except ValueError:
                continue
            if not all(parts[1:5]):
                # 盘中当天的占位行：开高低为空、收盘等于昨收、成交额为空，例如
                # ``20260907,,,,1577.36,0,,0.000,,,0``（2026-09-07 科创50，那天线上 6 次
                # 因此整个源报 ValueError 落到腾讯）。当天那根由盘中 bar 补，这里跳过。
                continue
            out.append({
                "日期": day,
                # 注意列序：同花顺是 开-高-低-收，不是别家的 开-收-高-低
                "开盘": float(parts[1]), "最高": float(parts[2]),
                "最低": float(parts[3]), "收盘": float(parts[4]),
                "成交量": _num(parts[5]), "成交额": _num(parts[6]),
                "换手率": _num(parts[7]) / 100 if len(parts) > 7 else 0.0,
            })
        return out


def _num(text: str) -> float:
    """空串当 0：停牌日的成交量、成交额可能是空的，不该让整个源报错。"""
    return float(text) if text else 0.0


pf.register(TonghuashunPlatform())
