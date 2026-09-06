"""1.x 的 CN_STOCK_ 前缀配置名在 2.0.0 不再被读，启动时要能把残留报出来。"""

from finmcp.config import legacy_env_names


def test_reports_names_still_wearing_the_old_prefix():
    environ = {
        "CN_STOCK_DATA_FETCH_MAX_WORKERS": "8",
        "CN_STOCK_REPORT_CACHE_DIR": ".runtime/report-cache",
        "FETCH_MAX_WORKERS": "8",
        "AKSHARE_PROXY_ENABLED": "0",
        "PATH": "/usr/bin",
    }
    assert legacy_env_names(environ, prefix="") == [
        "CN_STOCK_DATA_FETCH_MAX_WORKERS",
        "CN_STOCK_REPORT_CACHE_DIR",
    ]


def test_nothing_to_report_when_env_is_clean():
    assert legacy_env_names({"FETCH_MAX_WORKERS": "8"}, prefix="") == []


def test_old_prefix_is_legitimate_when_env_prefix_is_set_to_it():
    # ENV_PREFIX=CN_STOCK_ 时 CN_STOCK_FETCH_MAX_WORKERS 就是有效配置，不能当残留报
    environ = {"CN_STOCK_FETCH_MAX_WORKERS": "8"}
    assert legacy_env_names(environ, prefix="CN_STOCK_") == []


def test_other_env_prefix_does_not_exempt_old_names():
    environ = {"CN_STOCK_FETCH_MAX_WORKERS": "8", "CNSTOCK_FETCH_MAX_WORKERS": "8"}
    assert legacy_env_names(environ, prefix="CNSTOCK_") == ["CN_STOCK_FETCH_MAX_WORKERS"]
