"""市场云图的两种输出：``json``（基础数据）和 ``markdown``（给人看的一屏）。

取数和输出分开：``market_map_source`` 只管从上游读出事实，这里只管怎么摆。
换一种排版不用碰取数，加一个源不用碰这里。

两种格式提供相同的逐股基础数据。行业按成员主力净流入合计或加权涨跌筛选，成员按
涨跌幅排序；行业内的成员再按 ``stocks_per_sector`` 取前 N 只（按涨跌幅），资金流
合计与覆盖数仍按行业的**全部**成员计算。Markdown 表下注明资金流合计及覆盖数。
布局、面积和配色由调用方决定。
"""

from __future__ import annotations

import json

from .datasource.market_map_source import BOARDS

FORMATS = ("json", "markdown")
RANK_FIELDS = ("main_net", "change_pct")

#: 行业内默认列出的个股数。行业少则几只、多则一两百只，默认全列时响应体由最大的
#: 几个行业决定；默认 20 在保留每端强弱代表和控体积之间取平衡。给 0 取消裁剪。
DEFAULT_STOCKS_PER_SECTOR = 20

#: 板块代码 → 中文名。取数层那张表是唯一的一份，这里不另存。
_BOARD_NAMES = {code: label for code, (label, _) in BOARDS.items()}

def _pct(value) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def group(stocks) -> list:
    """按行业分组并算出**排序用**的聚合值。

    这些聚合值 ``json`` 格式不返回（调用方自己能算），但裁剪和 ``markdown`` 都要用它们
    排序——所以算在这一层，而不是让取数层多返回几个字段。
    """
    buckets: dict = {}
    for stock in stocks:
        buckets.setdefault(stock.sector, []).append(stock)
    out = []
    for sector, members in buckets.items():
        size = sum(s.size or 0.0 for s in members)
        priced = [s for s in members if s.size is not None and s.change_pct is not None]
        weighted_size = sum(s.size for s in priced)
        weighted = sum(s.size * s.change_pct for s in priced)
        out.append({
            "sector": sector,
            "members": members,
            "size": size,
            # 仅供行业排序，不输出为基础数据。
            "change_pct": (weighted / weighted_size) if weighted_size else None,
            "main_net": (sum(s.main_net for s in members if s.main_net is not None)
                         if any(s.main_net is not None for s in members) else None),
            "known_main_net": sum(s.main_net for s in members if s.main_net is not None),
            "flow_missing": sum(s.main_net is None for s in members),
        })
    return out


def select(groups: list, sectors, rank_by: str = "change_pct") -> tuple:
    """裁剪。返回(要保留的组, 说明这一次是怎么裁的)。

    ``sectors`` 给数字 N 就取指定指标的前 N 与后 N 个行业，缺指标的不参与排名。

    裁剪省的是响应体，**一分请求都不省**——要知道哪些行业在头尾，得先把整个板块取
    回来聚合。所以别指望它降低上游压力；那件事靠 ``board`` 筛选和缓存。
    """
    ranked = sorted(groups, key=lambda g: (g[rank_by] is None, -(g[rank_by] or 0), g["sector"]))
    if sectors == "all" or sectors is None or sectors >= len(ranked) * 2:
        return ranked, {"sectors_total": len(ranked), "sectors_returned": len(ranked),
                        "ranked_by": None}
    # 没有涨跌幅的组不进两端。退市股整组都是 None，而它们被排序键甩到末尾，
    # 于是 ranked[-N:] 会把"退市"当成跌得最多的那批捞上来——实测出现过。
    # sectors_total 必须是**真实总数**，在过滤之前定下来：读者拿它判断略掉了多少，
    # 用过滤后的数当分母就把"有几个行业没排名"这件事也一起藏了。
    total = len(ranked)
    ranked = [g for g in ranked if g[rank_by] is not None]
    if not ranked:
        return [], {"sectors_total": total, "sectors_returned": 0, "ranked_by": None}
    # 按下标切、不按值去重：这些字典里装着几百个成员对象，``g not in head``
    # 会逐个深比较，130 个行业下就是白烧一遍 CPU。
    cut = min(sectors, len(ranked) // 2 + len(ranked) % 2)
    kept = ranked[:cut] + ranked[max(cut, len(ranked) - sectors):]
    return kept, {"sectors_total": total, "sectors_returned": len(kept),
                  "ranked_by": "member_main_net_sum" if rank_by == "main_net" else "size_weighted_change_pct"}


def stock_order(stock):
    return (stock.change_pct is None, -(stock.change_pct or 0), stock.symbol)


def listed_members(g: dict, stocks_per_sector) -> list:
    """行业内要列出的成员：按涨跌幅降序，取前 ``stocks_per_sector`` 只。

    给 0、"all"、None 或超过成员数时全列。裁剪只影响列出谁——资金流合计等聚合值
    仍按全部成员算，合计少算和"没列全"是两件事，混在一起读者会拿小合计当全行业。
    """
    members = sorted(g["members"], key=stock_order)
    if stocks_per_sector in (None, "all", 0) or stocks_per_sector >= len(members):
        return members
    return members[:stocks_per_sector]


def as_json(market_map, groups: list, trim: dict, stocks_per_sector) -> str:
    """基础数据。派生值一律不放，样式更不放。"""
    payload = {
        "board": market_map.board,
        "board_name": _BOARD_NAMES.get(market_map.board, market_map.board),
        "as_of": market_map.as_of.isoformat() if market_map.as_of else None,
        # 加权字段必须声明：不说的话，调用方无法复现行业涨跌排序。
        "weight_by": market_map.size_field,
        "source": market_map.source,
        "upstream_total": market_map.upstream_total,
        "returned": sum(len(listed_members(g, stocks_per_sector)) for g in groups),
        "complete": market_map.complete,
        "stock_order": "change_pct_desc",
        # 行业内的个股裁剪口径：0 表示全列。和行业裁剪一样要声明出来，不然调用方
        # 不知道 stocks 是不是全集，拿去算行业内占比就错了。
        "stocks_per_sector": stocks_per_sector if stocks_per_sector not in (None, "all") else 0,
        "units": {"last": "CNY", "float_cap": "CNY", "amount_yuan": "CNY",
                  "main_net": "CNY", "change_pct": "percent", "main_pct": "percent"},
        "sectors_total": trim["sectors_total"],
        "sectors_returned": trim["sectors_returned"],
        "ranked_by": trim["ranked_by"],
        "sectors": [
            {"sector": g["sector"],
             "stocks_total": len(g["members"]),
             "stocks_omitted": max(0, len(g["members"]) - len(listed_members(g, stocks_per_sector))),
             "stocks": [{"symbol": s.symbol, "name": s.name,
                         "change_pct": s.change_pct, "last": s.last,
                         "float_cap": s.float_cap, "amount_yuan": s.amount_yuan,
                         "main_net": s.main_net, "main_pct": s.main_pct}
                        for s in listed_members(g, stocks_per_sector)]}
            for g in groups
        ],
        "warnings": list(market_map.warnings),
    }
    # 紧凑分隔符：这一路是给程序读的，缩进会让体积涨两三倍，而体积正是这个格式
    # 存在的理由（全 A 全量 5911 只，紧凑约 460 KiB，带缩进就上兆了）。
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def as_markdown(market_map, groups: list, trim: dict, stocks_per_sector) -> str:
    """按行业列逐股基础数据，与 JSON 使用同一成员集合。"""
    name = _BOARD_NAMES.get(market_map.board, market_map.board)
    total = sum(len(listed_members(g, stocks_per_sector)) for g in groups)
    # 裁剪过就写清"多少只里的多少只"：只写 44 只会被读成"科创板只有 44 只"。
    scope = (f"{total} 只（共 {market_map.upstream_total} 只）"
             if total < market_map.upstream_total else f"{total} 只")
    out = [f"# 市场云图　{name}　{scope}", ""]

    meta = [f"数据日期 {market_map.as_of or '—'}",
            f"行业涨跌加权 {'流通市值' if market_map.size_field == 'float_cap' else '成交额'}",
            f"来源 {market_map.source or '—'}"]
    if trim["ranked_by"]:
        ends = "资金净流入/流出" if trim["ranked_by"] == "member_main_net_sum" else "涨跌"
        meta.append(f"{trim['sectors_total']} 个行业里取了{ends}两端共 "
                    f"{trim['sectors_returned']} 个")
    if not market_map.complete:
        meta.append(f"**不完整**：上游称 {market_map.upstream_total} 只，实得 {len(market_map.stocks)}")
    out += ["> " + " · ".join(meta), ""]

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    def money(value):
        return "—" if value is None else f"{value:,.2f}"
    for g in groups:
        out += [f"### {cell(g['sector'])}", "",
                "| 代码 | 名称 | 最新价（元） | 涨跌幅 | 流通市值（元） | 成交额（元） | 主力净流入（元） | 主力净占比 |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        shown = listed_members(g, stocks_per_sector)
        for s in shown:
            out.append(f"| {cell(s.symbol)} | {cell(s.name)} | {money(s.last)} "
                       f"| {_pct(s.change_pct)} | {money(s.float_cap)} | {money(s.amount_yuan)} "
                       f"| {money(s.main_net)} | {_pct(s.main_pct)} |")
        if len(shown) < len(g["members"]):
            # 和行业的"共 N 只"同一个道理：不写清"列了几只里的几只"，读者会以为
            # 这个行业就只有这几只。
            out += ["", f"> 按涨跌幅列出前 {len(shown)} 只，共 {len(g['members'])} 只；"
                        "其余未列出。"]
        missing = g["flow_missing"]
        label = "已知成员主力净流入小计" if missing else "成员主力净流入合计"
        out += ["", f"> {label}：{money(g['known_main_net']) if missing < len(g['members']) else '—'} 元"
                f"；资金流覆盖 {len(g['members']) - missing}/{len(g['members'])} 只。", ""]

    notes = ["仅返回上游基础数据；— 表示缺值，不等于 0。"]
    if trim["ranked_by"]:
        basis = "成员主力净流入合计" if trim["ranked_by"] == "member_main_net_sum" else "所选数据项加权涨跌"
        notes.append(f"行业按{basis}选取两端，个股按涨跌幅降序；未列出中间行业，不能作为全市场占比的分母。")
    if stocks_per_sector not in (None, "all") and stocks_per_sector != 0:
        notes.append(f"每行业按涨跌幅最多列出 {stocks_per_sector} 只（0 全列）；"
                     "资金流合计覆盖行业全部成员，不受个股裁剪影响。")
    notes.append("资金流合计仅覆盖所选市场内的行业成员；主力净流入是上游的大单分类指标，不代表全部资金。")
    for warning in market_map.warnings:
        notes.append(f"⚠️ {warning}")
    quoted: list = []
    for note in notes:
        if quoted:
            quoted.append(">")
        quoted.append(f"> {note}")
    return "\n".join(out + [""] + quoted).rstrip() + "\n"


def render(market_map, *, fmt: str = "markdown", sectors=10, rank_by: str = "main_net",
           stocks_per_sector=DEFAULT_STOCKS_PER_SECTOR) -> str:
    if fmt not in FORMATS:
        raise ValueError("fmt 必须是 json/markdown 之一")
    if rank_by not in RANK_FIELDS:
        raise ValueError("rank_by 必须是 main_net/change_pct 之一")
    groups = group(market_map.stocks)
    kept, trim = select(groups, sectors, rank_by)
    excluded = sum(g[rank_by] is None for g in groups)
    partial = (sum(0 < g["flow_missing"] < len(g["members"]) for g in groups)
               if rank_by == "main_net" else 0)
    if excluded and sectors != "all":
        import dataclasses
        market_map = dataclasses.replace(market_map, warnings=(*market_map.warnings,
            f"{excluded} 个行业缺少完整排序数据，未参与排名；sectors=0 可查看全部成员。"))
    if partial:
        import dataclasses
        market_map = dataclasses.replace(market_map, warnings=(*market_map.warnings,
            f"{partial} 个行业的个股资金流不完整，按已知成员小计参与排名；详见各股字段和覆盖数。"))
    if fmt == "markdown":
        return as_markdown(market_map, kept, trim, stocks_per_sector)
    return as_json(market_map, kept, trim, stocks_per_sector)


__all__ = ["DEFAULT_STOCKS_PER_SECTOR", "FORMATS", "as_json", "as_markdown",
           "group", "listed_members", "render", "select"]
