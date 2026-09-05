"""
A股数据 MCP 服务入口

支持多种数据源（默认使用 AkShare）
"""

from dotenv import load_dotenv

load_dotenv(override=True)

import logging
import os
import warnings
from importlib.metadata import PackageNotFoundError, version as package_version

# AkShare 的若干接口用 tqdm 画进度条，写的是 stderr，而 start.sh 把 stderr 并进
# 日志文件。于是日志里混着 "0%|          | 0/3 [00:00<?, ?it/s]" 和光标控制字符，
# grep 出来的行经常被进度条截断。tqdm 4.66 起认这个环境变量；用 setdefault，
# 需要看进度条的人仍可以显式置 0。必须在 akshare 被导入之前设。
os.environ.setdefault("TQDM_DISABLE", "1")

# stateless_http 每个请求新建一次会话，MCP SDK 不显式关闭 anyio 的内存流，靠 GC 回收，
# 于是每个请求在 __del__ 里丢一条 ResourceWarning。实测一次 109 次调用的运行里，这些
# 警告占了日志的一半（759/1527 行），把真正的 WARNING 淹没。只静音这一条已知噪声，
# 其余 ResourceWarning 保持可见。
warnings.filterwarnings(
    "ignore",
    message=r"Unclosed <MemoryObject(Receive|Send)Stream",
    category=ResourceWarning,
)

logging.basicConfig(level=logging.WARN, format="%(asctime)s %(levelname)s %(message)s")

# urllib3 在每次重试上打一条 WARNING，带完整 URL。上游拒绝一个端点时，一次四标的
# 的请求就能刷出 20 多条，而每条重试链最后都有我们自己那条 "获取K线数据失败 ..."
# 的汇总，同样带 URL 和错误——重试过程本身没有多余信息，只是把汇总淹掉。留 ERROR
# 级，连接池真正出事时仍然可见。
logging.getLogger("urllib3").setLevel(logging.ERROR)

logger = logging.getLogger("finmcp")
logger.setLevel(logging.DEBUG)

import click

from finmcp import __version__, mcp_app
from finmcp.datasource.http_channel import describe_installed_channel
from finmcp.symbols import load_symbols


def log_application_version() -> None:
    logger.info("cn-stock-mcp version=%s", __version__)


def log_http_channel() -> None:
    """Report which outbound HTTP channel the process actually installed."""
    logger.info("HTTP channel %s", describe_installed_channel())


def log_market_data_versions() -> None:
    versions = {}
    for package_name in ("akshare", "efinance"):
        try:
            versions[package_name] = package_version(package_name)
        except PackageNotFoundError:
            versions[package_name] = "unknown"
    logger.info(
        "Market data library versions: akshare=%s efinance=%s",
        versions["akshare"],
        versions["efinance"],
    )


@click.command()
@click.option("--port", default=8686, help="Port to listen on for SSE")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "http"], case_sensitive=False),
    default="http",
    help="Transport type",
)
def main(port: int, transport: str) -> int:
    """启动 A股数据 MCP 服务"""
    log_application_version()
    log_market_data_versions()
    log_http_channel()
    load_symbols()
    if transport == "http":
        transport = "streamable-http"
    mcp_app.settings.port = port
    mcp_app.settings.log_level = "WARNING"
    logger.info(f"Starting MCP app on port {port} with transport {transport}")
    mcp_app.run(transport)  # type: ignore
    return 0


if __name__ == "__main__":
    main()
