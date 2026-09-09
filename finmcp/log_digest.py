"""把服务自己的日志读成结构化的健康数据。``health`` 工具的取数层。

**不发任何上游请求。** 只读本进程写下的日志,所以零积分、零上游压力、随时可调,
也不会因为"查健康"反过来把服务打挂。

## 时间窗怎么切

``start.sh`` 每次启动都 ``mv cn-stock-mcp.log cn-stock-mcp.log.bak``,所以当前那个
文件天然就是"本次启动至今"——``since=startup`` 不需要在文件里找启动标记,读它就是。
要跨重启(``today``)才需要把 ``.bak`` 也读进来。

## 可用率的分母

分母不是"调用次数",是**维度实例数**:每个完成的标的按 ``report_contract`` 算出它
在那个工具下应该有哪些维度,ETF 没有财务、指数没有市值不进分母。分子由渲染时写下的
``present=N/M`` 直接给出——那是实测的,不是按上游失败反推的。

老日志没有 ``present=`` 字段时退回反推(``incomplete_sources=<源>`` → 该源名下的
维度),此时 ``availability.method`` 是 ``inferred``,读的人要知道它看不见"源成功
返回但字段为空"那一类。
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .report_contract import (
    CONTRACT, DEGRADED_DIMENSION, NOT_APPLICABLE_MARKERS, expected)

#: 一次最多读多少字节。日志一天能到几 MB，长期跑更大；这个工具自己绝不能变成
#: 性能问题。超了就从尾部截断并在 warnings 里说明。
MAX_BYTES = 8 * 1024 * 1024

_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

_RE_SYMBOL = re.compile(
    _TS + r".*Finished symbol request_id=(\S+) tool=(\S+) symbol=(\S+) "
    r"raw_data=([\d.]+)s render=([\d.]+)s total=([\d.]+)s chars=(\d+)"
    r"(?: present=(\d+)/(\d+) missing=(\S*) degraded=(\S*))?")
_RE_TASK = re.compile(_TS + r".*Data task (\S+) .*?symbol=(\S+) .*?service=([\d.]+)s")
_RE_BATCH = re.compile(_TS + r".*Batch query released.*? total=([\d.]+)s")
_RE_PAGE = re.compile(_TS + r".*Realtime fund flow page .*?symbol=(\S+) .*?service=([\d.]+)s")
_RE_SKIPPED = re.compile(_TS + r".*Report cache skipped.*?symbol=(\S+) incomplete_sources=(\S+)")
_RE_VERSION = re.compile(_TS + r".*cn-stock-mcp version=(\S+)")
_RE_RENDER = re.compile(r"render=([0-9a-f]{12})")

#: 离散事件。key 是稳定的事件名（对外可见，别随便改），value 是匹配和一句人话。
EVENTS = {
    "impersonate_cooldown": (
        re.compile(_TS + r".*suspending impersonation"),
        "伪装通道暂停，期间东财源整体跳过"),
    "source_breaker_open": (
        re.compile(_TS + r".*Source breaker opened source=(\S+)"),
        "上游源熔断，改走备用源"),
    "caliber_change": (
        re.compile(_TS + r".*指数 (\d{6}) 的 K 线来自"),
        "指数 K 线落到备用源，成交量口径变了，报告里看不出来"),
    "tonghuashun_5xx": (
        re.compile(_TS + r".*同花顺取kline失败"),
        "同花顺年份文件取不到"),
    "session_crash": (
        re.compile(_TS + r".*ERROR Stateless session crashed"),
        "会话崩溃，多半是调用方超时断开"),
}

#: 阶段名 → 日志里的 Data task 名字。对外用中文，别把内部函数名漏给使用者。
STAGES = {
    "K 线取数": "_fetch_kline_sync",
    "资金流取数": "_fetch_fund_flow_cached",
    "实时行情": "_fetch_realtime_from_cache",
    "财务取数": "_fetch_finance_sync",
}


#: 逐小时比 p90 时，**每个小时**要的最小样本量。低于它只报当前值，不报涨跌：
#: 样本少于十来个时 p90 基本就是最大值，拿两个最大值比涨跌是在比噪音。
#: 按小时分桶之后这个门槛不能沿用"窗口对半"时代的 30——低频服务一小时不见得有
#: 三十次调用，那样每一行都会写"样本不足"，等于这一列不存在。
MIN_HOUR_SAMPLES = 10
#: 基线 p90 低于这个数就不给百分比。多数是缓存命中的阶段 p90 本来就接近 0，
#: 除出来的 ±100% 没有意义。
_TREND_MIN_P90 = 0.5


@dataclass
class Series:
    """一组带时刻的耗时样本。平均和分位数都要——它们看的是两件事。

    平均被长尾拉高、分位数把长尾砍掉。只看 p50 会以为很健康，只看平均会以为普遍慢；
    两个并排才看得出"多数很快、少数极慢"这个形状。今天 K 线取数 avg 4.76s 而
    p50 只有 2.72s，差的那一倍就是 89 秒那条长尾——正好指向"该给它加个总预算"。
    """

    values: list = field(default_factory=list)     # [(时刻, 秒数)]

    def _stats(self, xs: list) -> Optional[dict]:
        if not xs:
            return None
        xs = sorted(xs)
        pick = lambda q: xs[min(len(xs) - 1, int(len(xs) * q))]   # noqa: E731
        return {"n": len(xs), "avg": sum(xs) / len(xs), "p50": pick(0.5),
                "p90": pick(0.9), "p95": pick(0.95), "max": xs[-1]}

    def stats(self) -> Optional[dict]:
        return self._stats([v for _, v in self.values])

    def by_hour(self) -> dict:
        """按整点小时分桶，键是 ``YYYY-MM-DD HH``。"""
        buckets: dict = defaultdict(list)
        for stamp, value in self.values:
            buckets[stamp[:13]].append(value)
        return buckets

    def trend(self, epoch_of=None) -> Optional[dict]:
        """**本小时 vs 上一个小时**，比的是 p90。

        用整点小时分桶，因为小时边界是绝对的：读者不用问"切分点在哪"，换个查询窗口
        同一份数据也算得出同一个数。

        原先是"窗口对半分"，那个设计有两处站不住：切分点取决于日志跨度，每个阶段
        各按自己的样本时间算，同一列里其实是七个不同的切分点；改一下 ``since``
        同一批数据就能得出不同的涨跌。也踩过更糟的两版——按样本数对半（密集采样那
        一段缓存全热、天然快，算出 +251% 的假性能下降），和跨市场时段直接对半
        （+98% ~ +787%，其实只是盘中和收盘后走的不是一条路）。

        ``上一个小时``取的是**相邻**的那个整点。中间那小时没有调用就不比——拿两小时
        前的数当"上一小时"是在编。

        两个小时都要够 ``MIN_HOUR_SAMPLES``：样本少于它时 p90 基本就是最大值，
        拿两个最大值比涨跌是在比噪音。

        ``epoch_of`` 只用来**标注**，不再用来拒绝比较：小时边界和市场时段边界不重合
        （15:00 那一桶前半是盘中、后半是收盘整理），按纪元筛桶会把桶自己切碎。跨了
        时段的比较照样给数，但标出来，让读者知道那个涨跌可能只是换了时段。
        """
        buckets = self.by_hour()
        if len(buckets) < 2:
            return None
        hour = max(buckets)
        previous = (dt.datetime.strptime(hour, "%Y-%m-%d %H")
                    - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H")
        if previous not in buckets:
            return None
        current, baseline = buckets[hour], buckets[previous]
        if min(len(current), len(baseline)) < MIN_HOUR_SAMPLES:
            return None
        after, before = self._stats(current), self._stats(baseline)
        epochs = None
        if epoch_of is not None:
            # 每桶取一个代表时刻问纪元就够了：只是为了标注跨没跨时段。
            ordered = sorted(self.values)
            pick = {h: next(t for t, _ in ordered if t.startswith(h))
                    for h in (previous, hour)}
            epochs = (epoch_of(pick[previous]), epoch_of(pick[hour]))
        result = {"baseline": before, "current": after,
                  "hour": hour, "previous": previous, "change_pct": None,
                  "cross_phase": bool(epochs and epochs[0] != epochs[1]),
                  "epochs": epochs}
        # 基线 p90 太小时不给百分比：财务取数多数是缓存命中、p90 本来就是 0，
        # 除出来的 ±100% 没有意义。
        if before["p90"] >= _TREND_MIN_P90:
            result["change_pct"] = (after["p90"] - before["p90"]) / before["p90"] * 100
        return result


def log_files(log_file: str, since: str) -> list:
    """按时间顺序返回要读的文件。当前那个天然就是本次启动至今。

    收的是**文件路径**不是目录：发布形态安装时包在 site-packages 里而日志写在仓库下，
    服务自己推不出来，由 start.sh 把 LOG_FILE 导进来（见 config.LOG_FILE）。
    """
    previous = log_file + ".bak"
    if since == "startup":
        return [log_file] if os.path.exists(log_file) else []
    return [p for p in (previous, log_file) if os.path.exists(p)]


def _read(path: str) -> tuple:
    """读尾部至多 MAX_BYTES。返回（文本, 是否截断）。"""
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        if size > MAX_BYTES:
            handle.seek(size - MAX_BYTES)
            handle.readline()          # 丢掉被切半的那行
            return handle.read(), True
        return handle.read(), False


def resolve_since(since: str, now: dt.datetime) -> Optional[dt.datetime]:
    """把 since 参数换成一个起始时刻。``startup`` 返回 None（整个当前文件都算）。"""
    since = (since or "startup").strip().lower()
    if since in ("startup", ""):
        return None
    if since == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if since == "epoch":
        from .market_session import phase_and_epoch          # 避免顶层循环导入
        # 纪元名形如 closed-2026-09-07 / live-2026-09-08，只取日期部分当下界；
        # 纪元内的精确起点由 market_session 决定，这里不重算，宁可多算一点。
        _, epoch = phase_and_epoch(now)
        day = epoch.rsplit("-", 3)[-3:]
        try:
            return dt.datetime.strptime("-".join(day), "%Y-%m-%d")
        except ValueError:
            return None
    match = re.fullmatch(r"(\d+)([mh])", since)
    if match:
        amount = int(match.group(1))
        delta = dt.timedelta(minutes=amount) if match.group(2) == "m" else dt.timedelta(hours=amount)
        return now - delta
    try:
        return dt.datetime.strptime(since, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def digest(log_file: str, *, since: str = "startup", symbol: str = "",
           now: Optional[dt.datetime] = None) -> dict:
    """扫日志，产出 health 要的全部结构化数据。"""
    now = now or dt.datetime.now()
    floor = resolve_since(since, now)
    wanted = {s.strip().upper() for s in symbol.split(",") if s.strip()}

    paths, truncated, text = log_files(log_file, since), False, []
    for path in paths:
        body, cut = _read(path)
        truncated = truncated or cut
        text.append(body)
    lines = "\n".join(text).splitlines()

    first_at = last_at = None
    restarts = 0
    version = fingerprint = ""
    symbols: list = []
    stages = defaultdict(Series)
    batches, pages = Series(), Series()
    missing_counter: Counter = Counter()
    missing_symbols = defaultdict(set)
    missing_last: dict = {}
    degraded_counter: Counter = Counter()
    not_applicable_counter: Counter = Counter()
    inferred_sources: Counter = Counter()
    # 单维可用率的分子分母。分母不是"调用了几次"，而是**这次调用本该有这一维几次**——
    # brief 不要求历史资金流向、ETF 没有财务报表，那些不进分母（见 report_contract）。
    dim_expected: Counter = Counter()
    dim_missing: Counter = Counter()
    dim_degraded: Counter = Counter()
    dim_symbols = defaultdict(set)
    dim_source: dict = {}
    events = defaultdict(lambda: {"count": 0, "first": None, "last": None, "detail": "", "items": Counter()})

    for line in lines:
        stamp = line[:19]
        if len(stamp) != 19 or stamp[4] != "-":
            continue
        if floor is not None and stamp < floor.strftime("%Y-%m-%d %H:%M:%S"):
            continue
        first_at = first_at or stamp
        last_at = stamp

        if (m := _RE_VERSION.search(line)):
            restarts += 1
            version = m.group(2)
            continue
        if not fingerprint and (m := _RE_RENDER.search(line)):
            fingerprint = m.group(1)

        if (m := _RE_SYMBOL.search(line)):
            sym = m.group(4)
            if wanted and sym.upper() not in wanted:
                continue
            present = int(m.group(9)) if m.group(9) else None
            total = int(m.group(10)) if m.group(10) else None
            miss = [x for x in (m.group(11) or "").split(",") if x and x != "-"]
            degraded = [x for x in (m.group(12) or "").split(",") if x and x != "-"]
            symbols.append({"at": m.group(1), "tool": m.group(3), "symbol": sym,
                            "total": float(m.group(7)), "present": present,
                            "expected": total, "missing": miss})
            for name in miss:
                missing_counter[name] += 1
                missing_symbols[name].add(sym)
                missing_last[name] = m.group(1)
            for name in degraded:
                if name in NOT_APPLICABLE_MARKERS:
                    not_applicable_counter[name] += 1     # 正当缺席，不是降级
                else:
                    degraded_counter[name] += 1
            # 只有带 present= 的行才算进单维分母。老日志没有这个字段，不知道它缺了
            # 什么，按"零缺失"计入会把可用率抬高——宁可样本少，不要虚高。
            if total is not None:
                # 正当缺席的先摘掉：钉日期的查询没有"实时"资金流可言，进了分母就是
                # 拿一批重放把可用率打下去，而什么都没坏。
                skip = {NOT_APPLICABLE_MARKERS[x] for x in degraded
                        if x in NOT_APPLICABLE_MARKERS}
                for dimension in expected(m.group(3), sym):
                    if dimension.name in skip:
                        continue
                    dim_expected[dimension.name] += 1
                    dim_source[dimension.name] = dimension.source
                for name in miss:
                    dim_missing[name] += 1
                    dim_symbols[name].add(sym)
                # 一份报告里同一维可能触发不止一句降级提示，按维度去重后再计数。
                for name in {DEGRADED_DIMENSION[x] for x in degraded
                             if x in DEGRADED_DIMENSION}:
                    dim_degraded[name] += 1
                    dim_symbols[name].add(sym)
            continue

        if (m := _RE_SKIPPED.search(line)):
            if not (wanted and m.group(2).upper() not in wanted):
                for source in m.group(3).split(","):
                    inferred_sources[source] += 1
            continue
        if (m := _RE_TASK.search(line)):
            if not (wanted and m.group(3).upper() not in wanted):
                stages[m.group(2)].values.append((m.group(1), float(m.group(4))))
            continue
        if (m := _RE_BATCH.search(line)):
            # 一批覆盖多个标的，按单个标的过滤时把它算进来是错的——那条耗时里
            # 有别的标的的份。过滤模式下整批这一行直接不收。
            if not wanted:
                batches.values.append((m.group(1), float(m.group(2))))
            continue
        if (m := _RE_PAGE.search(line)):
            # 这一行里的 symbol 是裸六位码（399001），没有市场前缀。
            if not (wanted and not any(w.endswith(m.group(2)) for w in wanted)):
                pages.values.append((m.group(1), float(m.group(3))))
            continue

        for kind, (pattern, detail) in EVENTS.items():
            if (m := pattern.search(line)):
                slot = events[kind]
                slot["count"] += 1
                slot["first"] = slot["first"] or m.group(1)
                slot["last"] = m.group(1)
                slot["detail"] = detail
                if m.lastindex and m.lastindex >= 2:
                    slot["items"][m.group(2)] += 1
                break

    return {
        "window": {"since": since, "from": first_at, "to": last_at,
                   "restarts": max(0, restarts - 1) if since != "startup" else 0,
                   "version": version, "fingerprint": fingerprint,
                   "files": [os.path.basename(p) for p in paths],
                   # 只留文件名。这份报告会发给 MCP 调用方，绝对路径会把服务器的
                   # 目录结构一起带出去。想知道读的是不是对的那个文件，看窗口的
                   # from → to：读到过期或别的实例的日志，时间戳就对不上。
                   "name": os.path.basename(log_file), "truncated": truncated},
        "symbols": symbols,
        "availability": _availability(symbols, inferred_sources),
        "missing": [{"dimension": name, "count": count,
                     "symbols": sorted(missing_symbols[name]),
                     "last_at": missing_last.get(name)}
                    for name, count in missing_counter.most_common()],
        "degraded": dict(degraded_counter),
        "not_applicable": dict(not_applicable_counter),
        # 每一维单独的"拿到 / 该拿到"。降级算没拿到——问的是返回了数据没有，
        # 一个写着"暂无…"的空段落，对使用者和整段消失是一回事。
        # 逐小时可用率。总数只回答"缺不缺"，逐小时回答"什么时候开始缺的"——
        # 一段时间前坏过、现在已经好了，和正在坏，要采取的行动完全不同。
        "hours": _by_hour(symbols),
        "dimensions": [
            {"dimension": name, "source": dim_source.get(name, "-"),
             "expected": count,
             "missing": dim_missing.get(name, 0),
             "degraded": dim_degraded.get(name, 0),
             "got": count - dim_missing.get(name, 0) - dim_degraded.get(name, 0),
             "rate": (count - dim_missing.get(name, 0) - dim_degraded.get(name, 0)) / count,
             "symbols": sorted(dim_symbols.get(name, ()))}
            for name, count in dim_expected.items()
        ],
        # 源级失败次数。有 present= 时它是旁证，没有时它是唯一的缺失线索。
        "failed_sources": dict(inferred_sources),
        "latency": _latency(batches, Series([(s["at"], s["total"]) for s in symbols]),
                            stages, pages),
        "events": {k: {kk: (dict(vv) if isinstance(vv, Counter) else vv)
                       for kk, vv in v.items()} for k, v in events.items()},
    }


def _epoch_of(stamp: str) -> str:
    """时刻 → 市场纪元名。趋势对比只在同一个纪元内做，见 Series.trend。"""
    from .market_session import phase_and_epoch

    try:
        _, epoch = phase_and_epoch(dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        return epoch
    except Exception:                                  # noqa: BLE001 - 解析不了就不分组
        return ""


def _latency(batches: Series, per_symbol: Series, stages: dict, pages: Series) -> dict:
    """每个阶段的分布 + 趋势。顺序是从外到内：整批 → 单标的 → 各取数阶段，
    这样一眼看得出瓶颈在哪一层。"""
    series = {"整批总计": batches, "单标的总计": per_symbol}
    series.update({label: stages[task] for label, task in STAGES.items()})
    series["资金流页面"] = pages
    return {name: {"stats": s.stats(), "trend": s.trend(_epoch_of)}
            for name, s in series.items()}


def _by_hour(symbols: list) -> list:
    """按整点小时汇总可用率，新的排前面。

    只算带 ``present=`` 的行：老日志不知道它缺了什么，按"零缺失"计入会把某个小时
    抬成满分。那种情况整节留空，由"说明"里讲清楚。
    """
    buckets: dict = {}
    for item in symbols:
        if not item.get("expected"):
            continue
        row = buckets.setdefault(item["at"][:13], {
            "hour": item["at"][:13], "symbols": 0, "expected": 0, "present": 0,
            "missing": Counter()})
        row["symbols"] += 1
        row["expected"] += item["expected"]
        row["present"] += item["present"]
        for name in item.get("missing") or ():
            row["missing"][name] += 1
    out = []
    for row in sorted(buckets.values(), key=lambda r: r["hour"], reverse=True):
        row["rate"] = row["present"] / row["expected"] if row["expected"] else None
        row["missing"] = dict(row["missing"].most_common())
        out.append(row)
    return out


def _availability(symbols: list, inferred_sources: Counter) -> dict:
    """有 present= 就用实测的，没有就按上游失败反推，并把方法标出来。"""
    measured = [s for s in symbols if s["expected"]]
    if measured:
        expected_total = sum(s["expected"] for s in measured)
        present_total = sum(s["present"] for s in measured)
        return {"method": "measured", "expected": expected_total,
                "present": present_total, "missing": expected_total - present_total,
                "rate": present_total / expected_total if expected_total else None,
                "coverage": len(measured) / len(symbols) if symbols else 0.0}
    # 老日志：只能按源失败推。一个源失败带走它名下的全部维度。
    per_source = defaultdict(int)
    for tool_dims in CONTRACT.values():
        for dimension in tool_dims:
            per_source[dimension.source] += 1
    inferred_missing = sum(count * per_source.get(source, 1)
                           for source, count in inferred_sources.items())
    expected_total = sum(len(expected(s["tool"], s["symbol"])) for s in symbols)
    return {"method": "inferred", "expected": expected_total,
            "present": max(0, expected_total - inferred_missing),
            "missing": min(expected_total, inferred_missing),
            "rate": (max(0, expected_total - inferred_missing) / expected_total)
                    if expected_total else None,
            "coverage": 0.0}


__all__ = ["EVENTS", "MAX_BYTES", "STAGES", "Series", "digest", "log_files", "resolve_since"]
