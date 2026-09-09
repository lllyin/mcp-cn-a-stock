"""把 ``log_digest`` 的结构化数据渲染成一屏 Markdown。``health`` 工具的输出层。

解析和渲染分开:``log_digest`` 只管从日志里读出事实,这里只管怎么摆给人看。加一个
判据不用碰解析,换一种排版不用碰正则。
"""

from __future__ import annotations

from .log_digest import EVENTS, MIN_HOUR_SAMPLES

#: 结论的判据。写成表而不是散在渲染代码里——阈值要能一眼看全、一处改完。
#: 取值理由：95% 是"少了一整维"的量级（一个 17 维的报告少一维就是 94%）；
#: 60s 是调用方常见的超时量级，超过它使用者会先看到超时而不是数据；
#: +50% 是趋势里 p90 的变化，小于它多半是上游抖动。
AVAILABILITY_BAD = 0.50
AVAILABILITY_WARN = 0.95
SLOW_SECONDS = 60.0
TREND_WARN_PCT = 50.0


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _sec(value) -> str:
    return "—" if value is None else f"{value:.2f}s"


def _verdict(data: dict) -> tuple:
    """返回(图标, 一句话)。规则见模块顶部的四个常量,不让渲染自由发挥。

    **没有数据不等于没有问题。** 读不到日志时各项统计天然全是 0，照常算下去会得出
    "一切正常"——一块什么都没接的大屏亮着绿灯，比没有这块屏更糟。而这不是假想：
    ``LOG_FILE`` 的默认值按包的位置推，发布形态安装时它指向 site-packages 下并不存在
    的路径，没经 start.sh 导入变量就正好落进来。所以先判有没有读到东西。
    """
    window = data.get("window") or {}
    if not window.get("files"):
        return "❓", (f"读不到日志文件 {window.get('name') or '(未配置)'}"
                     "，无法判断——请检查 LOG_FILE")
    # 耗时也要算进"有没有数据"：标的全部失败时 symbols 是空的，但 Data task 那些行
    # 还在，说明服务确实在干活，该照常给结论。
    any_latency = any(entry["stats"] for entry in data["latency"].values())
    if not (data["symbols"] or data["events"] or data["failed_sources"] or any_latency):
        return "❓", "窗口内没有任何调用记录，没有可判断的依据"

    # 按**对数据的影响**从重到轻排，不按代码顺序。踩过：一次数据 100% 完整的运行，
    # 头条写的是"fund_flow 源失败 38 次"（兜底已经补上了），而同一份报告里
    # "指数 K 线换源、成交量口径变了 16 次"只出现在最底下的事件表里——真正让数字
    # 变了意思的那个被埋了，被兜住的那个上了头条。
    problems: list = []
    events, measured = data["events"], data["availability"].get("method") == "measured"

    if (crashes := events.get("session_crash", {}).get("count")):
        problems.append(("bad", f"会话崩溃 {crashes} 次"))

    rate = (data["availability"] or {}).get("rate")
    if rate is not None and rate < AVAILABILITY_BAD:
        problems.append(("bad", f"可用率只有 {_pct(rate)}"))
    elif rate is not None and rate < AVAILABILITY_WARN:
        problems.append(("warn", f"可用率 {_pct(rate)}"))

    if data["missing"]:
        top = data["missing"][0]
        problems.append(("warn", f"{top['dimension']}缺了 {top['count']} 次"))

    # 口径变了要进结论。数据还在、可用率照样满分，但同一个字段换了含义——按 §一
    # 这比"取不到"更难发现，因为报告上看不出来。
    if (caliber := events.get("caliber_change", {}).get("count")):
        problems.append(("warn", f"指数 K 线换源 {caliber} 次，成交量口径变了"))

    for name, entry in data["latency"].items():
        if entry["stats"] and entry["stats"]["max"] > SLOW_SECONDS:
            problems.append(("warn", f"{name}最慢 {entry['stats']['max']:.0f}s"))
            break
    for name, entry in data["latency"].items():
        change = (entry["trend"] or {}).get("change_pct")
        if change is not None and change > TREND_WARN_PCT:
            problems.append(("warn", f"{name} p90 比上一小时 {change:+.0f}%"))
            break

    # 源失败排最后，而且要说清楚兜住了没有。维度齐全时它是"值得看"，不是"出事了"。
    if data.get("failed_sources"):
        source, count = max(data["failed_sources"].items(), key=lambda x: x[1])
        covered = measured and not data["missing"]
        problems.append(("warn", f"{source} 源失败 {count} 次"
                                 + ("（数据已由兜底补齐）" if covered else "")))

    if problems:
        icon = "❌" if any(p[0] == "bad" for p in problems) else "⚠️"
        return icon, "；".join(text for _, text in problems)

    # 数据齐、也没变口径，但期间换过源就不该写"一切正常"——下面事件表里明明列着
    # 通道冷却和熔断，两句并排看是自相矛盾。也不能升成 ⚠️：网关关闭时浏览器兜底
    # 是稳态，天天亮黄灯的告警等于没有告警。所以留 ✅，把次数说出来。
    switches = sum(events.get(name, {}).get("count", 0)
                   for name in ("impersonate_cooldown", "source_breaker_open"))
    if switches:
        return "✅", f"数据完整；期间换过 {switches} 次源，都兜住了（见事件）"
    return "✅", "一切正常"


def _dimension_table(data: dict) -> list:
    """每一维单独的"拿到 / 该拿到"。

    总可用率是一个把所有维度拌在一起的数：19 维满分、1 维全挂，加起来还有 95%，
    看着没事。要回答"资金流向到底缺不缺"，只能一维一维地看。

    分母按**工具和标的类别**算，不是按调用次数：``brief`` 不要求历史资金流向、
    ETF 没有财务报表、指数没有市值——那些不进分母（见 report_contract）。
    所以同一维在不同工具下分母不同，这是对的。

    降级算没拿到；钉日期查询没有"实时"资金流那种正当缺席，在 log_digest 里就已经
    摘出分母了。
    """
    rows = data.get("dimensions") or []
    if not rows:
        return []
    out = ["## 各维度可用率", "",
           "分母是**这些调用本该有这一维几次**，不是调用了几次：brief 不要求历史资金流向、"
           "ETF 没有财务报表，那些不进分母。段落在但写着「暂无…」算没拿到。", ""]

    # 满分的那些只给一行汇总。二十行 100% 会把真正有问题的那一行挤到屏幕外，而大屏
    # 的用处就是**一眼看到问题**。汇总里保留分母范围和按源的分布，"有没有缺"这个
    # 问题照样答得上。
    bad = sorted((r for r in rows if r["rate"] < 1), key=lambda r: (r["rate"], r["dimension"]))
    good = [r for r in rows if r["rate"] >= 1]
    if bad:
        out += ["| 维度 | 上游源 | 该有 | 拿到 | 缺失 | 降级 | 可用率 | 涉及标的 |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
        for row in bad:
            symbols = "、".join(row["symbols"][:4]) or "—"
            if len(row["symbols"]) > 4:
                symbols += f" 等 {len(row['symbols'])} 个"
            out.append(f"| {row['dimension']} | {row['source']} | {row['expected']} "
                       f"| {row['got']} | {row['missing'] or '—'} | {row['degraded'] or '—'} "
                       f"| **{_pct(row['rate'])}** | {symbols} |")
        out.append("")
    if good:
        by_source: dict = {}
        for row in good:
            by_source.setdefault(row["source"], []).append(row["dimension"])
        spread = sorted(r["expected"] for r in good)
        span = f"{spread[0]}" if spread[0] == spread[-1] else f"{spread[0]}–{spread[-1]}"
        detail = "、".join(f"{src} {len(names)} 维" for src, names in sorted(by_source.items()))
        out += [f"**{len(good)} 维全部拿到**（各该有 {span} 次）：{detail}", ""]
    return out


def _hourly_table(data: dict) -> list:
    """逐小时可用率。

    总可用率只回答"缺不缺"，逐小时回答**什么时候开始缺的**——一小时前坏过、现在
    已经好了，和正在坏，要采取的行动完全不同，而一个合起来的百分比把这两件事写成
    同一个数。

    新的排前面：读者最先要知道的是"现在"。
    """
    rows = data.get("hours") or []
    if not rows:
        return []
    out = ["## 逐小时可用率", "",
           "| 小时 | 调用 | 该有维度 | 拿到 | 可用率 | 缺了什么 |",
           "| --- | ---: | ---: | ---: | ---: | --- |"]
    for row in rows:
        missing = "、".join(f"{k}×{v}" for k, v in row["missing"].items()) or "—"
        rate = _pct(row["rate"])
        out.append(f"| {row['hour'][11:]}:00 | {row['symbols']} | {row['expected']} "
                   f"| {row['present']} "
                   f"| {'**' + rate + '**' if (row['rate'] or 1) < 1 else rate} | {missing} |")
    return out + [""]


def render(data: dict) -> str:
    """一屏 Markdown。段落顺序按"先结论、再细节"排,读者从上往下越读越细。"""
    window, availability = data["window"], data["availability"]
    out = ["# 服务健康　" + f"{window['from'] or '—'} → {window['to'] or '—'}", ""]

    version = window.get("version") or "—"
    line = f"版本 {version}"
    if window.get("fingerprint"):
        line += f"，渲染指纹 {window['fingerprint']}"
    if window.get("restarts"):
        line += f"，窗口内重启 {window['restarts']} 次"
    # 只写文件名，不写路径：这份报告会发给 MCP 调用方，绝对路径会把服务器的目录
    # 结构一起带出去。读没读错文件由上面那行的 from → to 时间戳露出来——读到过期
    # 或别的实例的日志，窗口的结束时刻就对不上刚才那次调用。
    if window.get("name"):
        line += f"，数据来自 {window['name']}"
    out += [line, ""]

    icon, summary = _verdict(data)
    out += [f"## {icon} {summary}", ""]

    slowest = max((e["stats"]["max"], name) for name, e in data["latency"].items()
                  if e["stats"]) if any(e["stats"] for e in data["latency"].values()) else None
    top_missing = data["missing"][0] if data["missing"] else None
    # 只在**没有维度级明细时**（旧日志缺 present= 字段）退回源级，那时显示"无"是在
    # 撒谎。有维度级数据而且一处不缺，就该写"无"——原先的条件是"没有缺失就退回源级"，
    # 于是一次 1572/1572 全绿的运行在这一格写着"fund_flow（源）38 次"，
    # 而那 38 次已经被兜底补上了。
    if not top_missing and data["availability"].get("method") != "measured" \
            and data.get("failed_sources"):
        source, count = max(data["failed_sources"].items(), key=lambda x: x[1])
        top_missing = {"dimension": f"{source}（源）", "count": count}
    out += [
        "| 可用率 | 调用 | 缺失最多 | 最慢一次 |",
        "| --- | --- | --- | --- |",
        f"| **{_pct(availability.get('rate'))}** "
        f"| {len(data['symbols'])} 个标的 "
        f"| {top_missing['dimension'] + ' ' + str(top_missing['count']) + ' 次' if top_missing else '无'} "
        f"| {f'{slowest[0]:.1f}s（{slowest[1]}）' if slowest else '—'} |",
        "",
    ]

    # 可用率
    out += ["## 可用率", ""]
    if availability.get("expected"):
        out.append(f"{availability['present']} / {availability['expected']} 个维度"
                   f"（{'实测' if availability['method'] == 'measured' else '按上游失败推算'}）")
    else:
        out.append("窗口内没有完成的标的，算不出可用率。")
    out.append("")

    out += _dimension_table(data)
    out += _hourly_table(data)

    # 缺失明细
    if not data["missing"] and data.get("failed_sources"):
        # 两种情形共用这一段，说法要分开：维度级数据在、且全都拿到了，说明源失败被
        # 兜底救回来了——那是好消息，不能写成"没有维度级字段"。
        covered = data["availability"].get("method") == "measured"
        lead = ("维度都拿到了，但下面这些源失败过——兜底把数据补上了，源本身仍然要看："
                if covered else "只有源级线索（这份日志没有维度级字段）：")
        out += ["## 缺了什么", "", lead, "",
                "| 上游源 | 失败次数 |", "| --- | ---: |"]
        for source, count in sorted(data["failed_sources"].items(), key=lambda x: -x[1]):
            out.append(f"| {source} | {count} |")
        out.append("")
    if data["missing"]:
        out += ["## 缺了什么", "",
                "| 维度 | 次数 | 涉及标的 | 最近一次 |", "| --- | ---: | --- | --- |"]
        for row in data["missing"][:10]:
            names = "、".join(row["symbols"][:6])
            if len(row["symbols"]) > 6:
                names += f" 等 {len(row['symbols'])} 个"
            out.append(f"| {row['dimension']} | {row['count']} | {names} | {row['last_at']} |")
        out.append("")
    if data.get("degraded"):
        out += ["**渲染出来但没有值**：" + "、".join(
            f"{k}×{v}" for k, v in sorted(data["degraded"].items(), key=lambda x: -x[1])), ""]
    if data.get("not_applicable"):
        # 单列，别混进上面那行。钉日期查询没有"实时"资金流是正当缺席，写成"没有值"
        # 会让人去查一个不存在的故障。
        out += ["**正当缺席（不计入分母）**：" + "、".join(
            f"{k}×{v}" for k, v in sorted(data["not_applicable"].items(), key=lambda x: -x[1])), ""]

    # 耗时
    staged = [(name, e) for name, e in data["latency"].items() if e["stats"]]
    if not staged:
        # 一行数据都没有时别画个空表。只有表头的表比不画更糟——读者会以为渲染坏了。
        out += ["## 耗时", "", "窗口内没有取数记录。"]
    else:
        out += ["## 耗时", "",
                "| 阶段 | 次数 | 平均 | p50 | p90 | p95 | 最大 | 本小时 vs 上一小时 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, entry in staged:
        stats, trend = entry["stats"], entry["trend"]
        if trend is None:
            change = "—"
        elif trend["change_pct"] is None:
            change = "基线太小"
        else:
            mark = " ⚠️" if trend["change_pct"] > TREND_WARN_PCT else ""
            change = f"{trend['change_pct']:+.0f}%{mark}"
            if trend["cross_phase"]:
                change += "（跨时段）"
        out.append(f"| {name} | {stats['n']} | {_sec(stats['avg'])} | {_sec(stats['p50'])} "
                   f"| {_sec(stats['p90'])} | {_sec(stats['p95'])} | **{_sec(stats['max'])}** "
                   f"| {change} |")
    trends = [e["trend"] for e in data["latency"].values() if e["trend"]]
    if trends:
        hour, previous = trends[0]["hour"][11:], trends[0]["previous"][11:]
        note = (f"> 最后一列 = **{hour}:00 这一小时的 p90 比 {previous}:00 那一小时**。"
                f"两个小时各自的样本都得够 {MIN_HOUR_SAMPLES} 次才给这个数，"
                f"不够就写「—」；上一小时一次调用都没有也不比。")
        if any(t["cross_phase"] for t in trends):
            note += ("标了「跨时段」的那几行，两个小时分属盘中和收盘后——"
                     "两边走的不是一条路，那个涨跌多半是换了时段而不是变慢了。")
        out += ["", note]
    out.append("")

    # 最慢的几次
    slow = sorted(data["symbols"], key=lambda s: -s["total"])[:3]
    if slow and slow[0]["total"] > SLOW_SECONDS / 6:
        out += ["**最慢的几次**", "",
                "| 时刻 | 工具 | 标的 | 耗时 |", "| --- | --- | --- | ---: |"]
        for row in slow:
            out.append(f"| {row['at'][11:]} | {row['tool']} | {row['symbol']} | {row['total']:.2f}s |")
        out.append("")

    # 事件
    active = {k: v for k, v in data["events"].items() if v["count"]}
    if active:
        out += ["## 事件", "", "| 事件 | 次数 | 时段 | 说明 |", "| --- | ---: | --- | --- |"]
        for kind, entry in sorted(active.items(), key=lambda x: -x[1]["count"]):
            span = (f"{entry['first'][11:]} → {entry['last'][11:]}"
                    if entry["first"] != entry["last"] else entry["first"][11:])
            detail = entry["detail"] or EVENTS.get(kind, (None, kind))[1]
            if entry.get("items"):
                top = "、".join(f"{k}×{v}" for k, v in
                               sorted(entry["items"].items(), key=lambda x: -x[1])[:4])
                detail += f"（{top}）"
            out.append(f"| {kind} | {entry['count']} | {span} | {detail} |")
        out.append("")

    # 局限：这一节不能省，读的人得知道这份数字看不见什么
    notes = []
    # 一次调用都没有时 method 也是 inferred，但那时说"按上游失败推算"是无稽之谈——
    # 没有东西可推。这种情况由下面那条"窗口内没有报告类调用"讲清楚。
    if availability.get("method") == "inferred" and data["symbols"]:
        notes.append("可用率是**按上游失败推算**的，不是实测——这份日志还没有 `present=` 字段。"
                     "源成功返回但字段为空的那一类看不见。")
    if window.get("truncated"):
        notes.append("日志超过读取上限，只看了尾部，更早的记录没算进来。")
    if not data["symbols"]:
        notes.append("窗口内没有报告类调用，耗时和可用率都是空的。")
    if notes:
        out += ["## 说明", ""] + [f"- {n}" for n in notes]
    return "\n".join(out).rstrip() + "\n"


__all__ = ["AVAILABILITY_BAD", "AVAILABILITY_WARN", "SLOW_SECONDS", "TREND_WARN_PCT", "render"]
