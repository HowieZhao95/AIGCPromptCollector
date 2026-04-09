"""
第一步：从本机 Chrome 导出 X (Twitter) Cookie（只需运行一次）
之后 download_x_prompt.py 会自动加载，无需再次登录

原理：复用你日常使用的 Chrome 浏览器里已登录的 X 账号 Cookie，
      无需在 Playwright 内重新登录，避免机器人检测。

前提：本机 Chrome 已登录 x.com
"""
import asyncio
from playwright.async_api import async_playwright
from pathlib import Path
import sys

COOKIE_FILE = Path(__file__).parent / "x_auth.json"

# macOS 默认 Chrome 用户数据目录
CHROME_USER_DATA = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


async def main():
    if not CHROME_USER_DATA.exists():
        print("❌ 未找到 Chrome 用户数据目录")
        print(f"   期望路径: {CHROME_USER_DATA}")
        print("   请确认已安装 Google Chrome 并登录过 x.com")
        sys.exit(1)

    print(f"📂 使用 Chrome 用户数据: {CHROME_USER_DATA}")
    print("⚠️  请先关闭所有 Chrome 窗口，否则会启动失败")
    print()
    input("确认 Chrome 已关闭后，按回车继续...")

    async with async_playwright() as p:
        # 使用真实 Chrome + 已有用户数据（含登录 Cookie）
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(CHROME_USER_DATA),
            channel="chrome",       # 使用本机安装的 Chrome（非 Chromium）
            headless=False,
            args=["--profile-directory=Default"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print("🌐 打开 x.com 验证登录状态...")
        await page.goto("https://x.com/home")
        await page.wait_for_timeout(3000)

        # 检查是否已登录
        url = page.url
        if "login" in url or "i/flow" in url:
            print("⚠️  检测到未登录，请在弹出的浏览器中手动登录 X...")
            print("   登录完成后回到终端按回车")
            input()
        else:
            print("✅ 检测到已登录状态")

        await context.storage_state(path=str(COOKIE_FILE))
        print(f"✅ Cookie 已保存到 {COOKIE_FILE}")
        print("   以后运行 download_x_prompt.py 将自动加载，无需手动操作")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
