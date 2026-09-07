"""申万宏源研究所官网的行业分类。

和 shenwan.py 是同一套分类标准，差别只在上游：shenwan 经 AkShare 抓乐咕乐股的 HTML，
这里直接调申万宏源研究所自己的 JSON 接口
``www.swsresearch.com/institute-sw/api/index_publish/current/``。

它存在的理由：乐咕乐股前面是阿里云 WAF，对机房 IP 回 302 跳到 ``/human-challenge``
人机验证页（2026-09-06 实测：**同一时刻**机房 IP 拿到验证页、家用宽带拿到 200，
所以这一维的可达性取决于出口 IP 的类型，不是时间）。AkShare 跟着跳转拿到验证页、
找不到分类表，报 ``'NoneType' object has no attribute 'find_all'``，两级全挂，板块资金流
只能整棵树混排、父子同榜。同一段代码一台机器有分级一台没有，所以要第二个源。

## 实测（2026-09-06）

- ``indextype=一级行业``：31 个，与乐咕乐股同名 31/31，与东财板块名对上 31/31
- ``indextype=二级行业``：124 个，与乐咕乐股同名 124/124；东财 496 个板块里对上 120 个，
  乐咕对上 127 个——差的 7 个是体育Ⅱ、其他家电Ⅱ、农业综合Ⅱ、医疗美容、旅游零售Ⅱ、
  林业Ⅱ、油气开采Ⅱ，官网名单里没有这几个指数
- ``page_size=200`` 接口照单全收，两级各一个请求，5 KB + 20 KB，各约 1.2s

## TLS

它的服务器只发叶子证书、不发中间证书（``openssl s_client -showcerts`` 只见 1 张）。
系统 curl 靠 AIA 自己补链能过，Python 的 certifi 不补链，校验必失败。这里先按正常校验
取，只在 SSLError 时改为不校验再取一次——他们哪天把链补齐，校验就自动回来了。
AkShare 的 ``index_realtime_sw`` 是一律不校验，这里比它多守一道。
"""

from __future__ import annotations

import logging
import warnings
from typing import Optional

from .. import platform as pf
from ..sector_taxonomy import SectorTaxonomy

logger = logging.getLogger("finmcp")

_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/current/"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"),
}
#: 只取一级和二级，和 shenwan.py 同一条判据：排名只需要一个干净的层。
_LEVELS = ((1, "一级行业"), (2, "二级行业"))
#: 一页 200 够把二级的 124 个一次拿完。翻页逻辑仍然保留——接口哪天把页大小压回 50，
#: 少掉的是名单的后半段，排名会安静地少一批板块。
_PAGE_SIZE = 200
_MAX_PAGES = 10
_TIMEOUT = 15


def _get_json(params: dict) -> dict:
    """发一次请求。单拎出来是为了测试能不联网注入响应。"""
    import requests

    try:
        response = requests.get(_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.exceptions.SSLError as error:
        logger.debug("swsresearch 证书校验失败（%s），改为不校验重取", str(error)[:120])
        from urllib3.exceptions import InsecureRequestWarning

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            response = requests.get(
                _URL, params=params, headers=_HEADERS, timeout=_TIMEOUT, verify=False)
    response.raise_for_status()
    return response.json()


def _names(indextype: str) -> list:
    """某一级的全部指数名。按 ``count`` 翻页，翻够或翻到空页就停。"""
    names: list = []
    for page in range(1, _MAX_PAGES + 1):
        payload = _get_json({"page": page, "page_size": _PAGE_SIZE, "indextype": indextype})
        data = (payload or {}).get("data") or {}
        results = data.get("results") or []
        names.extend(str(item.get("swindexname") or "").strip() for item in results)
        if not results or len(names) >= int(data.get("count") or 0):
            break
    return names


class SwsResearchPlatform(pf.Platform):
    name, label = "swsresearch", "申万宏源研究所"
    capabilities = frozenset({"sector_taxonomy"})

    def supports(self, capability: str, request) -> bool:
        # 概念和地域没有分级，申万也不分它们——连请求都不发。
        return getattr(request, "sector_type", "industry") == "industry"

    def fetch_sector_taxonomy(self, request) -> Optional[SectorTaxonomy]:
        levels: dict = {}
        for level, indextype in _LEVELS:
            try:
                names = _names(indextype)
            except Exception as error:
                # 少一级不算失败，和 shenwan.py 同一条规则：一级到了就够排名用，
                # 缺的那级由 sector_taxonomy._merge 找下一个源补。
                logger.warning("申万宏源研究所%s级行业分类取数失败: %s", level, error)
                continue
            for text in names:
                # 先来的层级优先：一个名字只可能属于一级，重复说明上游有重名，信更粗的。
                if text and text not in levels:
                    levels[text] = level

        if not levels:
            return None
        return SectorTaxonomy(levels=levels, scheme="shenwan", source=self.name)


pf.register(SwsResearchPlatform())
