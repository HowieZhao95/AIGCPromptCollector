"""
第一步：手动登录小红书，保存 Cookie（只需运行一次）
之后 download_xhs.py 会自动加载，无需再次登录
"""
import asyncio
from playwright.async_api import async_playwright
from pathlib import Path

COOKIE_FILE = Path(__file__).parent / "xhs_auth.json"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print("🌐 打开小红书，请手动完成登录...")
        await page.goto("https://www.xiaohongshu.com")
        print("✅ 请在浏览器中登录小红书（扫码或手机号均可）")
        print("   登录完成后，回到终端按回车保存 Cookie...")
        input()

        # 保存登录状态
        await context.storage_state(path=str(COOKIE_FILE))
        print(f"✅ Cookie 已保存到 {COOKIE_FILE}")
        print("   以后运行 download_xhs.py 将自动登录，无需手动操作")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
