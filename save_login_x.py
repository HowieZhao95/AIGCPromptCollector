"""
第一步：手动登录 X (Twitter)，保存 Cookie（只需运行一次）
之后 download_x_prompt.py 会自动加载，无需再次登录
"""
import asyncio
from playwright.async_api import async_playwright
from pathlib import Path

COOKIE_FILE = Path(__file__).parent / "x_auth.json"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print("🌐 打开 X (Twitter)，请手动完成登录...")
        await page.goto("https://x.com/login")
        print("✅ 请在浏览器中登录 X（账号密码或 Google 登录均可）")
        print("   登录完成后，回到终端按回车保存 Cookie...")
        input()

        await context.storage_state(path=str(COOKIE_FILE))
        print(f"✅ Cookie 已保存到 {COOKIE_FILE}")
        print("   以后运行 download_x_prompt.py 将自动登录，无需手动操作")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
