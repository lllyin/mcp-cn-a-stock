import asyncio
import json
from playwright.async_api import async_playwright

async def get_fund_flow_browser(symbol: str) -> str:
    """
    使用无头浏览器+精准DOM节点提取，获取东方财富资金流向（最强稳定版）
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
            
            # 【关键修改 1】：直接等待带有 f62 属性的格子出现，这比等文字靠谱得多
            await page.wait_for_selector("td[data-field='f62']", timeout=10000)
            
            # 给 JS 留 0.5 秒把数字渲染进 <span> 的时间
            await page.wait_for_timeout(500)

            # 【关键修改 2】：封装一个狙击手函数，根据 data-field 直接拿 innerText
            async def get_val(field_id):
                try:
                    # 抓取 <td data-field="xxx"> 里面的纯文本（会自动包含里面的 span 文字）
                    text = await page.locator(f"td[data-field='{field_id}']").inner_text()
                    return text.strip() if text.strip() and text.strip() != "-" else "0"
                except:
                    return "0"

            # 获取百分比并转换为 float
            async def get_ratio(field_id):
                text = await get_val(field_id)
                try:
                    return float(text.replace("%", ""))
                except:
                    return 0.0

            # 像拼积木一样直接获取数据，清清爽爽
            result = {
                "标的": "大盘" if url.endswith("dpzjlx.html") else symbol,
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