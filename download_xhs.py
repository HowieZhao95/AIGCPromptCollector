"""
小红书图片下载脚本（全自动，无需手动登录）
依赖：先运行一次 save_login.py 保存 Cookie
"""
import asyncio
import httpx
import os
import sys
from pathlib import Path
from playwright.async_api import async_playwright

COOKIE_FILE = Path(__file__).parent / "xhs_auth.json"
SAVE_DIR = Path.home() / "Desktop" / "建筑提示词"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "Referer": "https://www.xiaohongshu.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}


async def download_image(client: httpx.AsyncClient, url: str, save_path: Path) -> bool:
    try:
        clean_url = url.split("?")[0]
        resp = await client.get(clean_url, headers=HEADERS, follow_redirects=True, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 5000:
            save_path.write_bytes(resp.content)
            print(f"  ✅ 已保存: {save_path.name} ({len(resp.content)//1024}KB)")
            return True
        else:
            print(f"  ⚠️ 图片无效 (status={resp.status_code}, size={len(resp.content)})")
    except Exception as e:
        print(f"  ❌ 下载失败: {e}")
    return False


async def main():
    # 检查 Cookie 文件
    if not COOKIE_FILE.exists():
        print("❌ 未找到登录状态文件！")
        print("   请先运行：uv run save_login.py")
        sys.exit(1)

    print(f"✅ 加载登录状态: {COOKIE_FILE}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        # 加载已保存的登录 Cookie，全自动无需登录
        context = await browser.new_context(
            storage_state=str(COOKIE_FILE),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        print("🔍 打开小红书搜索页面...")
        await page.goto("https://www.xiaohongshu.com/search_result?keyword=建筑提示词&type=51")
        await page.wait_for_timeout(4000)

        # 检查是否仍需登录
        login_dialog = await page.query_selector("input[placeholder*='手机号'], .login-container")
        if login_dialog:
            print("⚠️  Cookie 已过期，请重新运行 save_login.py 更新登录状态")
            await browser.close()
            sys.exit(1)

        # 等待笔记列表
        print("⏳ 等待笔记列表加载...")
        try:
            await page.wait_for_selector("section.note-item", timeout=15000)
        except Exception:
            # 备用选择器
            await page.wait_for_selector("a[href*='/explore/']", timeout=10000)
        print("✅ 笔记列表加载完成")

        downloaded_count = 0
        failed_count = 0

        async with httpx.AsyncClient() as client:
            for note_index in range(1, 11):
                print(f"\n📌 处理第 {note_index}/10 条笔记...")

                # 获取当前页面笔记列表
                note_cards = await page.query_selector_all("section.note-item a.cover")
                if not note_cards:
                    note_cards = await page.query_selector_all("a[href*='/explore/']")

                if len(note_cards) < note_index:
                    print(f"  ⚠️ 只找到 {len(note_cards)} 条笔记，结束")
                    break

                card = note_cards[note_index - 1]
                note_url = await card.get_attribute("href")

                try:
                    # 直接用 URL 打开笔记详情（更稳定）
                    if note_url:
                        full_url = f"https://www.xiaohongshu.com{note_url}" if note_url.startswith("/") else note_url
                        await page.goto(full_url)
                    else:
                        await card.click()
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    print(f"  ❌ 打开失败: {e}")
                    failed_count += 1
                    await page.go_back()
                    await page.wait_for_timeout(2000)
                    continue

                # 提取图片 URL
                img_urls = await page.evaluate("""
                    () => {
                        const imgs = Array.from(document.querySelectorAll('img'));
                        const urls = imgs
                            .map(img => img.src || img.dataset.src || '')
                            .filter(src => src && (
                                src.includes('xhscdn') ||
                                src.includes('sns-img') ||
                                src.includes('ci.xiaohongshu')
                            ) && !src.includes('avatar') && !src.includes('emoji'));
                        return [...new Set(urls)];
                    }
                """)

                if img_urls:
                    print(f"  🖼️  找到 {len(img_urls)} 张图片，下载第1张...")
                    save_path = SAVE_DIR / f"{note_index:02d}.jpg"
                    ok = await download_image(client, img_urls[0], save_path)
                    if ok:
                        downloaded_count += 1
                    else:
                        failed_count += 1
                else:
                    print(f"  ⚠️  未找到图片")
                    failed_count += 1

                # 返回搜索结果页
                await page.goto("https://www.xiaohongshu.com/search_result?keyword=建筑提示词&type=51")
                await page.wait_for_timeout(3000)

        print(f"\n{'='*40}")
        print(f"🎉 完成！成功下载 {downloaded_count} 张，失败 {failed_count} 张")
        print(f"📁 保存位置：{SAVE_DIR}")
        print(f"{'='*40}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
