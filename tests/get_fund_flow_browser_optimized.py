import asyncio
import json
import time
import re
from playwright.async_api import async_playwright

async def get_fund_flow_browser_optimized(symbols: list) -> str:
    if not symbols:
        return json.dumps({}, ensure_ascii=False)

    semaphore = asyncio.Semaphore(2)
    results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-gpu',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--mute-audio'
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        async def fetch_single(symbol: str):
            async with semaphore:
                page = await context.new_page()
                url = "https://data.eastmoney.com/zjlx/dpzjlx.html" if symbol in ['000001', '399001', '399006', '000688', 'dpzjlx'] else f"https://data.eastmoney.com/zjlx/{symbol}.html"
                
                try:
                    await page.route("**/*.{png,jpg,jpeg,gif,css,woff,woff2,ico,svg}", lambda route: route.abort())
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    
                    # [修复] 提高等待级别：等待“今日主力净流入”这几个字所在的元素稳定
                    await page.wait_for_selector("text=今日主力净流入", timeout=10000)
                    
                    # 给 Ajax 数据填充多一点容错执行时间
                    await asyncio.sleep(0.5)

                    # [修复核心] 重构解析逻辑
                    stock_data = await page.evaluate("""() => {
                        const getByDataField = (fid) => {
                            const el = document.querySelector(`td[data-field="${fid}"]`);
                            const txt = el ? el.innerText.trim() : "";
                            return (txt && txt !== '-') ? txt : null;
                        };
                        
                        const getByText = (fieldLabel) => {
                            // 暴力遍历正文：寻找核心标签后的下一个数字
                            const fullText = document.body.innerText;
                            const regex = new RegExp(fieldLabel + "：?\\\\s*([-\\\\d\\\\.\\\\+]+)(万|亿|%)");
                            const match = fullText.match(regex);
                            return match ? match[1] + (match[2] === '%' ? '' : match[2]) : null;
                        };

                        const titleEl = document.querySelector(".title") || document.querySelector("h1");
                        const name = titleEl ? titleEl.innerText.trim() : "";

                        // 字段映射表
                        const mapping = {
                            "f62": "今日主力净流入",
                            "f184": "主力净比",
                            "f66": "今日超大单净流入",
                            "f69": "超大单净比",
                            "f72": "今日大单净流入",
                            "f75": "大单净比",
                            "f78": "今日中单净流入",
                            "f81": "中单净比",
                            "f84": "今日小单净流入",
                            "f87": "小单净比"
                        };

                        const res = { "标的名称": name };
                        for (let k in mapping) {
                            res[k] = getByDataField(k) || getByText(mapping[k]) || "0";
                        }
                        return res;
                    }""")

                    def to_ratio(val_str):
                        try:
                            if isinstance(val_str, (int, float)): return float(val_str)
                            return float(val_str.replace('%',''))
                        except: return 0.0

                    results[symbol] = {
                        "标的名称": stock_data["标的名称"] or (symbol if "dpzjlx" not in url else "沪深资金流向"),
                        "主力净流入": stock_data["f62"],
                        "主力净比(%)": to_ratio(stock_data["f184"]),
                        "超大单净流入": stock_data["f66"],
                        "超大单净比(%)": to_ratio(stock_data["f69"]),
                        "大单净流入": stock_data["f72"],
                        "大单净比(%)": to_ratio(stock_data["f75"]),
                        "中单净流入": stock_data["f78"],
                        "中单净比(%)": to_ratio(stock_data["f81"]),
                        "小单净流入": stock_data["f84"],
                        "小单净比(%)": to_ratio(stock_data["f87"]),
                    }

                except Exception as e:
                    results[symbol] = {"error": str(e)}
                finally:
                    await page.close()

        tasks = [fetch_single(sym) for sym in symbols]
        await asyncio.gather(*tasks)
        await browser.close()
        
        return json.dumps(results, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    t0 = time.time()
    watchlist = ["dpzjlx", "000333", "600900", "300750"]
    print(asyncio.run(get_fund_flow_browser_optimized(watchlist)))
    print(f"\n🚀 修正版批量查询 ({len(watchlist)} 个) 总耗时: {time.time() - t0:.2f}s")
