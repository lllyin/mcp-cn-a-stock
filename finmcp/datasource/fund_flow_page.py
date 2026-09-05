"""解析东财个股资金流向页面 data.eastmoney.com/zjlx/<code>.html。

纯解析：输入页面 HTML，输出结构化资金流向，不联网、不启浏览器。这样它能被单元
测试完全覆盖，也能给 Playwright 兜底路径复用。

这个页面同时带今日和历史两部分，而现有的 realtime_ff.py 只用
``td[data-field="f62"]`` 之类的选择器取了今日的 5 组字段，历史表整个没用。当
东财的资金流向接口不可用时（2026-09-03 push2his 的 fflow 端点对本机出口 IP
连续拒绝了半小时以上），一次页面加载就能把两部分都拿回来。

页面上的数值是预格式化的（``1.59亿`` / ``-4725.87万`` / ``3.37%``），比接口返
回的原始值精度低。但报告本身就按同样的两位小数渲染，所以往返无损：
``1.59亿`` -> 1.59e8 -> ``1.59亿``，``-4725.87万`` -> -47258700 -> ``-4725.87万``。
边界也是安全的，``9999.99万`` 仍小于 1e8，不会被渲染成 ``1.00亿``。

输出的列名与 AkShare ``stock_individual_fund_flow`` 保持一致，好让
``_build_fund_flow_history`` 不加改动地消费，兜底与主源走同一条转换逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional

# 历史表的列序，与页面上两行表头展开后的 13 列一一对应。
HISTORY_COLUMNS = (
    "日期",
    "收盘价",
    "涨跌幅",
    "主力净流入-净额",
    "主力净流入-净占比",
    "超大单净流入-净额",
    "超大单净流入-净占比",
    "大单净流入-净额",
    "大单净流入-净占比",
    "中单净流入-净额",
    "中单净流入-净占比",
    "小单净流入-净额",
    "小单净流入-净占比",
)

# 今日一栏的 data-field 编号 -> 列名。编号沿用 realtime_ff.py 已依赖的那组，
# 两处指向同一张表，改动时必须一起改。
TODAY_FIELDS = {
    "f62": "主力净流入-净额",
    "f184": "主力净流入-净占比",
    "f66": "超大单净流入-净额",
    "f69": "超大单净流入-净占比",
    "f72": "大单净流入-净额",
    "f75": "大单净流入-净占比",
    "f78": "中单净流入-净额",
    "f81": "中单净流入-净占比",
    "f84": "小单净流入-净额",
    "f87": "小单净流入-净占比",
}

# 历史表所在容器。页面上还有别的 dataview，所以按 id 而不是 class 定位。
HISTORY_TABLE_ID = "table_ls"

# 页头那块实时行情（ul.hqlist）的元素 id。它比今日资金流那一栏多一份行情，
# 缺开盘/最高/最低，所以拼不出完整 K 线 bar，但可以做次级兜底和交叉验证。
QUOTE_FIELD_IDS = {
    "newPrice": "最新价",
    "zd": "涨跌",
    "zdf": "涨跌幅",
    "hs": "换手率",
    "sum": "总手",
    "totalPrice": "成交额",
}

# 风控滑块被实例化时页面上出现的痕迹。用模态框自己的 iframe 而不是
# ``websitecaptcha/build/popwscpc.js`` —— 后者是库，正常页面也会加载，只有这个
# iframe 出现才说明验证已经弹出来了。
# 实测 2026-09-04 15:50 被拒时的页面：<div class="popwscps_d"> 里挂着
# <iframe class="popwscps_d_iframe" src=".../websitecaptcha/slidervalid">，
# 同时 checkuser / Titan/api/captcha/get / icon_slide.png 全部 200。
_CAPTCHA_MARKERS = ("popwscps_d_iframe", "websitecaptcha/slidervalid")

_PLACEHOLDERS = {"", "-", "--", "—", "常规"}
_TITLE_RE = re.compile(r"^(?P<name>.*?)[（(](?P<code>\d{6})[)）]")
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FundFlowPageError(ValueError):
    """页面结构与预期不符，调用方应当把这次兜底视为取不到数据。"""


@dataclass
class FundFlowRow:
    """历史表的一行。净额单位为元，净占比与涨跌幅为百分数（3.37 表示 3.37%）。"""

    date: str
    close: Optional[float]
    pct_chg: Optional[float]
    # 顺序：主力、超大单、大单、中单、小单
    amounts: tuple = ()
    ratios: tuple = ()

    def as_record(self) -> dict:
        """转成与 AkShare 同列名的一行。"""
        record = {"日期": self.date, "收盘价": self.close, "涨跌幅": self.pct_chg}
        names = ("主力", "超大单", "大单", "中单", "小单")
        for name, amount, ratio in zip(names, self.amounts, self.ratios):
            record[f"{name}净流入-净额"] = amount
            record[f"{name}净流入-净占比"] = ratio
        return record


@dataclass
class FundFlowPage:
    """一次页面解析的结果。"""

    name: str = ""
    code: str = ""
    # 页面上第一个 .title 的原样文本（如 "三环集团(300408)"）。realtime_ff 把它
    # 直接当"标的名称"输出，保留原文才能让输出逐字不变。
    title_text: str = ""
    # 今日一栏；页面未渲染该块时为 None，调用方不应把它当成"今日为零"。
    today: Optional[dict] = None
    # 历史行，按日期升序。代码里凡是取"最新"的地方都用 [-1]，与内部数据集一致。
    history: list = field(default_factory=list)
    # 今日各字段的原样文本，供需要逐字一致输出的调用方使用。
    today_text: dict = field(default_factory=dict)
    # 页头实时行情的原样文本，键是中文字段名。
    quote_text: dict = field(default_factory=dict)
    # 页面上出现了风控滑块。只用来解释"已经失败了"，绝不用来判定失败：万一正常
    # 页面也带这个痕迹，误判会把一份好数据丢掉。
    captcha_present: bool = False

    @property
    def has_quote(self) -> bool:
        """页头行情是否有最新价。没有价格的行情对调用方没有意义。"""
        return parse_price(self.quote_text.get("最新价", "")) is not None

    @property
    def has_today(self) -> bool:
        """今日一栏是否真的有值。

        字典存在不等于有数据：占位符会以 None 存进来，十档全是 None 时字典
        依然非空。接口被拒时页面就是这个样子——单元格在，值是空的。停牌和
        开盘前也一样，所以这里只回答"有没有值"，是不是被拦截由调用方结合
        请求失败情况判断。
        """
        return bool(self.today) and any(v is not None for v in self.today.values())

    def history_records(self) -> list:
        return [row.as_record() for row in self.history]


def parse_amount(text: str) -> Optional[float]:
    """把 ``1.59亿`` / ``-4725.87万`` 解析成元。占位符返回 None。

    None 与 0.0 必须区分：停牌或非交易时段页面上是占位符，把它当成 0 会让报告
    声称"主力净流入 0 元"，而那是没有数据，不是没有资金流动。
    """
    cleaned = _clean(text)
    if cleaned is None:
        return None
    cleaned = cleaned.replace(",", "")
    for suffix, scale in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4), ("元", 1.0)):
        if cleaned.endswith(suffix):
            body = cleaned[: -len(suffix)]
            if not _NUMBER_RE.match(body):
                return None
            return float(body) * scale
    if not _NUMBER_RE.match(cleaned):
        return None
    return float(cleaned)


def parse_percent(text: str) -> Optional[float]:
    """把 ``3.37%`` 解析成 3.37。占位符返回 None。

    保留百分数而不是折算成小数，是为了对齐 AkShare 那一侧的列，让下游统一乘
    0.01，兜底与主源不出现两套量纲。
    """
    cleaned = _clean(text)
    if cleaned is None:
        return None
    cleaned = cleaned.replace(",", "").rstrip("%")
    if not _NUMBER_RE.match(cleaned):
        return None
    return float(cleaned)


def parse_price(text: str) -> Optional[float]:
    """把收盘价解析成浮点数。占位符返回 None。"""
    cleaned = _clean(text)
    if cleaned is None:
        return None
    cleaned = cleaned.replace(",", "")
    if not _NUMBER_RE.match(cleaned):
        return None
    return float(cleaned)


def _clean(text) -> Optional[str]:
    if text is None:
        return None
    cleaned = str(text).replace("\xa0", " ").strip()
    return None if cleaned in _PLACEHOLDERS else cleaned


class _FundFlowHTMLParser(HTMLParser):
    """按标签流提取标题、今日字段和历史表。

    用标准库而不是 lxml/bs4：两者只是 AkShare 的间接依赖，直接使用等于新增一
    个未声明的依赖，而这里要处理的只是一张结构固定的表。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_text = ""
        self.today_text: dict = {}
        self.rows: list = []

        self._title_depth = 0
        self._title_parts: list = []
        # 进入 div#table_ls 后按 div 深度计数，才知道什么时候离开这个容器。
        self._history_depth = 0
        self._in_history_body = False
        self._row_cells: Optional[list] = None
        self._cell_parts: Optional[list] = None
        self._today_field: Optional[str] = None
        self._today_parts: list = []
        self.quote_text: dict = {}
        # 行情字段的值可能嵌套在带颜色的 span 里，所以按同名标签深度配对，
        # 不能见到第一个结束标签就收工。
        self._quote_field: Optional[str] = None
        self._quote_tag: Optional[str] = None
        self._quote_depth = 0
        self._quote_parts: list = []

    # -- 标签 -------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)

        if tag == "div":
            if self._history_depth:
                self._history_depth += 1
            elif attributes.get("id") == HISTORY_TABLE_ID:
                self._history_depth = 1
            if self._title_depth:
                self._title_depth += 1
            elif _has_class(attributes, "title"):
                self._title_depth = 1
                self._title_parts = []
            return

        if tag == "tbody" and self._history_depth:
            self._in_history_body = True
            return

        if tag == "tr" and self._in_history_body:
            self._row_cells = []
            return

        if self._quote_field is not None and tag == self._quote_tag:
            self._quote_depth += 1
        elif attributes.get("id") in QUOTE_FIELD_IDS and self._quote_field is None:
            self._quote_field = QUOTE_FIELD_IDS[attributes["id"]]
            self._quote_tag = tag
            self._quote_depth = 1
            self._quote_parts = []

        if tag == "td":
            field_id = attributes.get("data-field")
            if field_id in TODAY_FIELDS:
                self._today_field = field_id
                self._today_parts = []
            if self._row_cells is not None:
                self._cell_parts = []
            return

    def handle_endtag(self, tag: str) -> None:
        if self._quote_field is not None and tag == self._quote_tag:
            self._quote_depth -= 1
            if self._quote_depth == 0:
                self.quote_text.setdefault(
                    self._quote_field, "".join(self._quote_parts).strip()
                )
                self._quote_field = None
                self._quote_tag = None

        if tag == "div":
            if self._history_depth:
                self._history_depth -= 1
                if self._history_depth == 0:
                    self._in_history_body = False
            if self._title_depth:
                self._title_depth -= 1
                if self._title_depth == 0 and not self.title_text:
                    self.title_text = "".join(self._title_parts).strip()
            return

        if tag == "td":
            if self._today_field is not None:
                self.today_text.setdefault(
                    self._today_field, "".join(self._today_parts).strip()
                )
                self._today_field = None
            if self._cell_parts is not None and self._row_cells is not None:
                self._row_cells.append("".join(self._cell_parts).strip())
                self._cell_parts = None
            return

        if tag == "tr" and self._row_cells is not None:
            if self._row_cells:
                self.rows.append(self._row_cells)
            self._row_cells = None
            return

        if tag == "tbody" and self._in_history_body:
            self._in_history_body = False

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._today_field is not None:
            self._today_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        if self._quote_field is not None:
            self._quote_parts.append(data)


def _has_class(attributes: dict, wanted: str) -> bool:
    return wanted in (attributes.get("class") or "").split()


def parse_fund_flow_page(html: str) -> FundFlowPage:
    """把资金流向页面解析成结构化结果。

    只在页面完全没有可用数据时抛 FundFlowPageError；表里有个别占位符是正常的
    （停牌、非交易时段），那些字段留 None 交给调用方判断。
    """
    if not html or not html.strip():
        raise FundFlowPageError("页面为空")

    parser = _FundFlowHTMLParser()
    parser.feed(html)
    parser.close()

    page = FundFlowPage(
        title_text=parser.title_text,
        captcha_present=any(marker in html for marker in _CAPTCHA_MARKERS),
    )
    match = _TITLE_RE.match(parser.title_text)
    if match:
        page.name = match.group("name").strip()
        page.code = match.group("code")

    page.quote_text = dict(parser.quote_text)
    page.today_text = dict(parser.today_text)
    if parser.today_text:
        today = {}
        for field_id, column in TODAY_FIELDS.items():
            raw = parser.today_text.get(field_id)
            if raw is None:
                continue
            today[column] = (
                parse_percent(raw) if column.endswith("净占比") else parse_amount(raw)
            )
        page.today = today or None

    expected = len(HISTORY_COLUMNS)
    for cells in parser.rows:
        # 行宽不符的一律跳过：页面偶尔混入"暂无数据"之类的提示行，宁可少一行，
        # 也不能按位错位地把净占比当净额填进去。
        if len(cells) != expected or not _DATE_RE.match(cells[0]):
            continue
        page.history.append(
            FundFlowRow(
                date=cells[0],
                close=parse_price(cells[1]),
                pct_chg=parse_percent(cells[2]),
                amounts=tuple(parse_amount(cells[i]) for i in range(3, expected, 2)),
                ratios=tuple(parse_percent(cells[i]) for i in range(4, expected, 2)),
            )
        )

    if not page.history and page.today is None and not page.quote_text:
        raise FundFlowPageError("页面既无今日数据也无历史表")

    # 页面是倒序（最新在前），转成升序对齐内部数据集：取最新一律用 [-1]。
    page.history.sort(key=lambda row: row.date)
    return page
