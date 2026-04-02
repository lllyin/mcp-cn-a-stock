import asyncio
import json
from playwright.async_api import async_playwright

async def get_fund_flow_browser(symbol: str) -> str:
    """
    使用无头浏览器+精准DOM节点提取，获取东方财富资金流向（含动态标的名称）
    """
    if symbol in ['000001', '399001', '399006', '000688', 'dpzjlx']:
        url = "https://data.eastmoney.com/zjlx/dpzjlx.html"
    else:
        url = f"https://data.eastmoney.com/zjlx/{symbol}.html"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # 拦截无用资源，极速加载
            await page.route("**/*.{png,jpg,jpeg,gif,css,woff,woff2}", lambda route: route.abort())
            
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # 等待核心数据格子加载完毕
            await page.wait_for_selector("td[data-field='f62']", timeout=10000)
            await page.wait_for_timeout(500)

            # 【新增提取标的名称】：精准狙击 class="title" 的容器
            try:
                # first 确保哪怕页面有多个 title 类，我们也只取最上面那个大标题
                target_name = await page.locator(".title").first.inner_text()
                target_name = target_name.strip()
            except:
                # 容错降级：如果提取失败，大盘显示"沪深两市"，个股显示代码
                target_name = "沪深两市大盘" if url.endswith("dpzjlx.html") else symbol

            # 提取数据的底层函数
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

            # 组装最终的结构化 JSON
            result = {
                "标的名称": target_name,   # <--- 动态提取的名字在这里！
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

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"error": f"抓取或解析失败: {str(e)}"}, ensure_ascii=False)
        finally:
            await browser.close()

# --- 本地测试代码 ---
if __name__ == "__main__":
    print("--- 正在获取 大盘 资金流 ---")
    print(asyncio.run(get_fund_flow_browser("dpzjlx")))
    
    print("\n--- 正在获取 美的集团 (000333) 资金流 ---")
    print(asyncio.run(get_fund_flow_browser("000333")))