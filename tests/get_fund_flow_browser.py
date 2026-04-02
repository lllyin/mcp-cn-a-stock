import asyncio
import json
import re
from playwright.async_api import async_playwright

async def get_fund_flow_browser(symbol: str) -> str:
    """
    使用无头浏览器真实访问东方财富，绕过反爬，提取资金流向（原味单位）
    """
    if symbol in ['000001', '399001', '399006', '000688', 'dpzjlx']:
        url = "https://data.eastmoney.com/zjlx/dpzjlx.html"
    else:
        url = f"https://data.eastmoney.com/zjlx/{symbol}.html"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # 拦截无用资源，极速加载
            await page.route("**/*.{png,jpg,jpeg,gif,css,woff,woff2}", lambda route: route.abort())
            
            # 使用 domcontentloaded 避免被持续的网络请求卡住
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_selector("text=今日主力净流入", timeout=10000)

            text_content = await page.evaluate("document.body.innerText")

            # [优化点 1]：提取金额和单位，直接返回带单位的字符串 (如 "-16.5万" 或 "2.3亿")
            def extract_money_with_unit(pattern):
                match = re.search(pattern, text_content)
                if match:
                    return f"{match.group(1)}{match.group(2)}"
                return "0"

            def extract_ratio(pattern):
                match = re.search(pattern, text_content)
                return float(match.group(1)) if match else 0.0

            # [优化点 2]：去掉了 Key 里的 "(亿)" 后缀
            result = {
                "标的": "大盘" if url.endswith("dpzjlx.html") else symbol,
                "主力净流入": extract_money_with_unit(r"今日主力净流入：\s*([-\d\.]+)(万|亿)"),
                "主力净比(%)": extract_ratio(r"主力净比：\s*([-\d\.]+)%"),
                "超大单净流入": extract_money_with_unit(r"今日超大单净流入：\s*([-\d\.]+)(万|亿)"),
                "超大单净比(%)": extract_ratio(r"超大单净比：\s*([-\d\.]+)%"),
                "大单净流入": extract_money_with_unit(r"今日大单净流入：\s*([-\d\.]+)(万|亿)"),
                "大单净比(%)": extract_ratio(r"大单净比：\s*([-\d\.]+)%"),
                "中单净流入": extract_money_with_unit(r"今日中单净流入：\s*([-\d\.]+)(万|亿)"),
                "中单净比(%)": extract_ratio(r"中单净比：\s*([-\d\.]+)%"),
                "小单净流入": extract_money_with_unit(r"今日小单净流入：\s*([-\d\.]+)(万|亿)"),
                "小单净比(%)": extract_ratio(r"小单净比：\s*([-\d\.]+)%"),
            }

            return json.dumps(result, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"error": f"抓取或解析失败: {str(e)}"}, ensure_ascii=False)
        finally:
            await browser.close()

# --- 本地测试代码 ---
if __name__ == "__main__":
    print("--- 正在通过无头浏览器获取大盘资金流 ---")
    print(asyncio.run(get_fund_flow_browser("dpzjlx")))

    print("\n--- 正在获取 美的集团 (000333) 资金流 ---")
    print(asyncio.run(get_fund_flow_browser("000333")))