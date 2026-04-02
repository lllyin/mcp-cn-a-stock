import asyncio
import json
from playwright.async_api import async_playwright

async def get_fund_flow_browser(symbols: list) -> str:
    """
    使用无头浏览器批量获取东方财富资金流向
    :param symbols: 代码列表，支持传单个如 ["000333"]，或多个 ["dpzjlx", "000333", "600900"]
    """
    # 如果传入的是空列表，直接返回
    if not symbols:
        return json.dumps({}, ensure_ascii=False)

    # 限制并发数为 3，防止服务器内存 OOM
    semaphore = asyncio.Semaphore(3)
    results = {}

    async with async_playwright() as p:
        # 1. 只启动一次浏览器，极其节省 CPU
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        )

        # 2. 定义单个任务的抓取逻辑
        async def fetch_single(symbol: str):
            async with semaphore:  # 拿到并发令牌后才执行
                page = await context.new_page()
                
                url = "https://data.eastmoney.com/zjlx/dpzjlx.html" if symbol in ['000001', '399001', '399006', '000688', 'dpzjlx'] else f"https://data.eastmoney.com/zjlx/{symbol}.html"
                
                try:
                    # 拦截无用资源，极速加载
                    await page.route("**/*.{png,jpg,jpeg,gif,css,woff,woff2}", lambda route: route.abort())
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    
                    # 等待核心数据格子加载完毕
                    await page.wait_for_selector("td[data-field='f62']", timeout=10000)
                    await page.wait_for_timeout(500)

                    # 提取标的名称
                    try:
                        target_name = await page.locator(".title").first.inner_text()
                        target_name = target_name.strip()
                    except:
                        target_name = "沪深两市大盘" if url.endswith("dpzjlx.html") else symbol

                    # 提取底层函数
                    async def get_val(field_id):
                        try:
                            text = await page.locator(f"td[data-field='{field_id}']").inner_text()
                            return text.strip() if text.strip() and text.strip() != "-" else "0"
                        except:
                            return "0"

                    async def get_ratio(field_id):
                        text = await get_val(field_id)
                        try:
                            return float(text.replace("%", ""))
                        except:
                            return 0.0

                    # 组装单只股票的结果
                    results[symbol] = {
                        "标的名称": target_name,
                        "主力净流入": await get_val("f62"),
                        "主力净比(%)": await get_ratio("f184"),
                        "超大单净流入": await get_val("f66"),
                        "超大单净比(%)": await get_ratio("f69"),
                        "大单净流入": await get_val("f72"),
                        "大单净比(%)": await get_ratio("f75"),
                        "中单净流入": await get_val("f78"),
                        "中单净比(%)": await get_ratio("f81"),
                        "小单净流入": await get_val("f84"),
                        "小单净比(%)": await get_ratio("f87"),
                    }

                except Exception as e:
                    # 如果某只股票抓取失败，不影响其他股票，单独标记 error
                    results[symbol] = {"error": f"抓取或解析失败: {str(e)}"}
                finally:
                    # 极其重要：抓完必须关闭当前标签页释放内存
                    await page.close()

        # 3. 将所有代码打包成任务，并发生效
        tasks = [fetch_single(sym) for sym in symbols]
        await asyncio.gather(*tasks)

        # 4. 关闭浏览器
        await browser.close()
        
        # 返回最终的字典对象转 JSON
        return json.dumps(results, ensure_ascii=False, indent=2)

# --- 本地测试代码 ---
if __name__ == "__main__":
    print("--- 正在测试：查询单个大盘 ---")
    print(asyncio.run(get_fund_flow_browser(["dpzjlx"])))
    
    print("\n--- 正在测试：批量查询大盘与多个个股 ---")
    watchlist = ["dpzjlx", "000333", "600900", "300750"]
    print(asyncio.run(get_fund_flow_browser(watchlist)))