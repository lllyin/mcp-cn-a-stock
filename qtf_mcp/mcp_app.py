import datetime
from io import StringIO
from typing import Literal, Dict, List

from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware

from . import research
from .datasource import get_datasource


# --- Output Models for MCP Inspector Schema ---

class BatchReportResponse(BaseModel):
    """批量报表响应模型"""
    symbols_count: int = Field(..., description="成功处理并返回报表的证券标的数量")
    timestamp: str = Field(..., description="报告生成时间 (YYYY-MM-DD HH:MM:SS)")
    reports: Dict[str, str] = Field(..., description="成功生成的报表集合 (键为代码，值为 Markdown 文本)")
    errors: Dict[str, str] = Field(..., description="发生错误的标的信息 (键为代码，值为错误详情)")

# -----------------------------------------------

async def fetch_batch_reports(symbol_str: str, mode: str, host: str) -> BatchReportResponse:
    """批量获取并生成报告的核心驱动程序"""
    # 1. 预处理：分拆并限流（上限4个）
    raw_symbols = [s.strip().upper() for s in symbol_str.split(',') if s.strip()]
    if len(raw_symbols) > 4:
        # TODO: 可以记录一下被截断的信息
        raw_symbols = raw_symbols[:4]
    
    # 2. 准备容器
    output = {
        "symbols_count": len(raw_symbols),
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reports": {},
        "errors": {}
    }

    async def process_item(symbol: str):
        try:
            # 并行拉取基础行情
            raw_data = await research.load_raw_data(symbol, None, host)
            if not raw_data:
                err_msg = f"未找到证券代码 {symbol} 的相关行情数据。"
                output["errors"][symbol] = err_msg
                output["reports"][symbol] = f"Error: {err_msg}"
                return

            buf = StringIO()
            # 根据模式按需构建
            research.build_basic_data(buf, symbol, raw_data)
            await research.build_trading_data(buf, symbol, raw_data)
            
            if mode in ["medium", "full"]:
                research.build_financial_data(buf, symbol, raw_data)
            if mode == "full":
                research.build_technical_data(buf, symbol, raw_data)
            
            output["reports"][symbol] = buf.getvalue()
        except Exception as e:
            err_msg = str(e)
            output["errors"][symbol] = err_msg
            output["reports"][symbol] = f"Error during processing: {err_msg}"

    # 并发执行所有标的的任务
    import asyncio
    await asyncio.gather(*[process_item(s) for s in raw_symbols])
    return BatchReportResponse(**output)


class QtfMCP(FastMCP):

  def streamable_http_app(self) -> Starlette:
    super_app = super().streamable_http_app()
    super_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    return super_app

# Create an MCP server
mcp_app = QtfMCP(
  "CnStock",
  sse_path="/cnstock/sse",
  message_path="/cnstock/messages/",
  streamable_http_path="/cnstock/mcp",
  stateless_http=True,
)
@mcp_app.tool()
async def brief(symbol: str, ctx: Context) -> BatchReportResponse:
  """Get brief information and fund flow for input stock symbol(s) (Batch Supported).
  Includes:
  - basic data
  - trading data (including real-time fund flow)
  
  Args:
    symbol (str): Stock symbol or comma-separated list (up to 4), e.g., "SZ300308,SH000001"

  Returns:
    A BatchReportResponse object containing multiple reports or errors.
  """
  who = ctx.request_context.request.client.host  # type: ignore
  return await fetch_batch_reports(symbol, "brief", who)


@mcp_app.tool()
async def medium(symbol: str, ctx: Context) -> BatchReportResponse:
  """Get medium information for input stock symbol(s) (Batch Supported).
  Includes:
  - basic data
  - trading data (including real-time fund flow)
  - financial data (abstract)

  Args:
    symbol (str): Stock symbol or comma-separated list (up to 4), e.g., "SZ300308,SH000001"

  Returns:
    A BatchReportResponse object containing multiple reports or errors.
  """
  who = ctx.request_context.request.client.host  # type: ignore
  return await fetch_batch_reports(symbol, "medium", who)


@mcp_app.tool()
async def full(symbol: str, ctx: Context) -> BatchReportResponse:
  """Get full information for input stock symbol(s) (Batch Supported).
  Includes:
  - basic data
  - trading data (including real-time fund flow)
  - financial data (comprehensive)
  - technical analysis data (MACD, KDJ, etc.)

  Args:
    symbol (str): Stock symbol or comma-separated list (up to 4), e.g., "SZ300308,SH000001"

  Returns:
    A BatchReportResponse object containing multiple reports or errors.
  """
  who = ctx.request_context.request.client.host  # type: ignore
  return await fetch_batch_reports(symbol, "full", who)


@mcp_app.tool()
async def kline_daily(
  symbol: str,
  date: str,
  adjust: Literal["qfq", "hfq", "none"] = "qfq",
  ctx: Context = None,  # type: ignore
) -> str:
  """获取指定日期的股票日K线数据
  
  Get daily K-line data for a specific date.
  
  Args:
    symbol (str): 股票代码，格式如 "SH600000" 或 "SZ000001"。
                  Stock symbol, must be in the format of "SH600000" or "SZ000001".
    date (str): 查询日期，格式 "YYYY-MM-DD"，如 "2024-12-13"。
                Query date in format "YYYY-MM-DD".
    adjust (str): 复权类型。"qfq"=前复权(默认), "hfq"=后复权, "none"=不复权。
                  Adjustment type: "qfq"=forward adjust(default), "hfq"=backward adjust, "none"=no adjust.
  
  Returns:
    该日期的K线数据，包含开盘价、收盘价、最高价、最低价、成交量、成交额等。
  """
  datasource = get_datasource()
  result = await datasource.fetch_kline_simple(symbol, date, date, adjust)
  
  if result is None or not result.get("data"):
    return f"未找到 {symbol} 在 {date} 的数据。可能是非交易日或股票代码有误。"
  
  data = result["data"][0]
  adjust_name = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}.get(adjust, adjust)
  
  buf = StringIO()
  print(f"# {symbol} {date} 日K线数据 ({adjust_name})", file=buf)
  print("", file=buf)
  print(f"- 开盘价: {data['开盘']:.2f}", file=buf)
  print(f"- 收盘价: {data['收盘']:.2f}", file=buf)
  print(f"- 最高价: {data['最高']:.2f}", file=buf)
  print(f"- 最低价: {data['最低']:.2f}", file=buf)
  print(f"- 成交量: {data['成交量']:,}", file=buf)
  print(f"- 成交额: {data['成交额']:,.2f}", file=buf)
  print(f"- 涨跌幅: {data['涨跌幅']:.2f}%", file=buf)
  print(f"- 涨跌额: {data['涨跌额']:.2f}", file=buf)
  print(f"- 振幅: {data['振幅']:.2f}%", file=buf)
  print(f"- 换手率: {data['换手率']:.2f}%", file=buf)
  
  return buf.getvalue()


@mcp_app.tool()
async def kline_range(
  symbol: str,
  start_date: str,
  end_date: str,
  adjust: Literal["qfq", "hfq", "none"] = "qfq",
  ctx: Context = None,  # type: ignore
) -> str:
  """获取指定日期区间的股票日K线数据
  
  Get daily K-line data for a date range.
  
  Args:
    symbol (str): 股票代码，格式如 "SH600000" 或 "SZ000001"。
                  Stock symbol, must be in the format of "SH600000" or "SZ000001".
    start_date (str): 开始日期，格式 "YYYY-MM-DD"，如 "2024-12-01"。
                      Start date in format "YYYY-MM-DD".
    end_date (str): 结束日期，格式 "YYYY-MM-DD"，如 "2024-12-13"。
                    End date in format "YYYY-MM-DD".
    adjust (str): 复权类型。"qfq"=前复权(默认), "hfq"=后复权, "none"=不复权。
                  Adjustment type: "qfq"=forward adjust(default), "hfq"=backward adjust, "none"=no adjust.
  
  Returns:
    日期区间内的K线数据表格，包含每日的开高低收、成交量、涨跌幅等。
  """
  datasource = get_datasource()
  result = await datasource.fetch_kline_simple(symbol, start_date, end_date, adjust)
  
  if result is None or not result.get("data"):
    return f"未找到 {symbol} 在 {start_date} 至 {end_date} 期间的数据。"
  
  data_list = result["data"]
  adjust_name = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}.get(adjust, adjust)
  
  buf = StringIO()
  print(f"# {symbol} K线数据 ({start_date} 至 {end_date}, {adjust_name})", file=buf)
  print("", file=buf)
  print(f"共 {len(data_list)} 个交易日", file=buf)
  print("", file=buf)
  
  # 表格头
  print("| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 涨跌幅 |", file=buf)
  print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |", file=buf)
  
  # 表格内容
  for item in data_list:
    print(
      f"| {item['日期']} | {item['开盘']:.2f} | {item['收盘']:.2f} | "
      f"{item['最高']:.2f} | {item['最低']:.2f} | {item['成交量']:,} | "
      f"{item['涨跌幅']:.2f}% |",
      file=buf
    )
  
  return buf.getvalue()
