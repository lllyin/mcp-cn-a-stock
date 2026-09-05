"""申万宏源行业分类。

只提供一件事：行业名 → 层级。它不给行情、不给资金流，存在的理由是东财的板块名单
把一棵树摊平了，而层级信息只有分类标准本身才有（细节见 sector_taxonomy.py）。

只覆盖 ``industry``。概念板块和地域板块没有分级，申万也不分它们——``supports()``
直接返回 False，连请求都不发。
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import platform as pf
from ..sector_taxonomy import SectorTaxonomy

logger = logging.getLogger("finmcp")

#: 只取一级和二级。三级 ``sw_index_third_info`` 是 HTML 抓取，实测 4 次成 2 次，
#: 而排名只需要一个干净的层——为三级引入一个一半会失败的依赖不划算。
_LEVELS = ((1, "sw_index_first_info"), (2, "sw_index_second_info"))


class ShenwanPlatform(pf.Platform):
    name, label = "shenwan", "申万宏源"
    capabilities = frozenset({"sector_taxonomy"})

    def supports(self, capability: str, request) -> bool:
        return getattr(request, "sector_type", "industry") == "industry"

    def fetch_sector_taxonomy(self, request) -> Optional[SectorTaxonomy]:
        import akshare as ak

        levels: dict = {}
        for level, function in _LEVELS:
            fetch = getattr(ak, function, None)
            if fetch is None:
                continue
            try:
                frame = fetch()
            except Exception as error:
                # 少一级不算失败：一级拿到了就够排名用。二级挂了只是少标几个板块的
                # 层级，比整份分级表拿不到强。
                logger.warning("申万%s级行业分类取数失败: %s", level, error)
                continue
            if frame is None or frame.empty or "行业名称" not in frame.columns:
                continue
            for name in frame["行业名称"]:
                text = str(name).strip()
                # 先来的层级优先：一个名字只可能属于一级，重复说明上游有重名，
                # 这时信更粗的那一级——排名要的就是粗粒度。
                if text and text not in levels:
                    levels[text] = level

        if not levels:
            return None
        return SectorTaxonomy(levels=levels, scheme="shenwan", source=self.name)


pf.register(ShenwanPlatform())
