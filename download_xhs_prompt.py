"""
小红书笔记全量采集脚本

采集笔记的所有图片 URL（含轮播）、文字内容、来源、发布时间，存入 SQLite。
每条笔记经 LLM 提取结构化提示词，无有效提示词的笔记不入库。

依赖：先运行一次 save_login.py 保存 Cookie

用法：
  uv run download_xhs_prompt.py --keyword "建筑提示词" --category "建筑"
  uv run download_xhs_prompt.py --keyword "建筑提示词" --category "建筑" --max-notes 50
"""

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

load_dotenv()

COOKIE_FILE = Path(__file__).parent / "xhs_auth.json"
BASE_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                note_id         TEXT PRIMARY KEY,
                url             TEXT NOT NULL,
                title           TEXT,
                description     TEXT,
                author_name     TEXT,
                author_id       TEXT,
                publish_time    TEXT,
                category        TEXT NOT NULL,
                search_keyword  TEXT NOT NULL,
                structured_prompt TEXT,
                image_count     INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS images (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id     TEXT NOT NULL REFERENCES notes(note_id),
                image_index INTEGER NOT NULL,
                url         TEXT NOT NULL,
                UNIQUE(note_id, image_index)
            );
        """)
        self.conn.commit()

    def note_exists(self, note_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone() is not None

    def insert_note(self, **kwargs):
        self.conn.execute(
            """INSERT OR IGNORE INTO notes
               (note_id, url, title, description, author_name, author_id,
                publish_time, category, search_keyword, structured_prompt, image_count)
               VALUES (:note_id, :url, :title, :description, :author_name,
                       :author_id, :publish_time, :category, :search_keyword,
                       :structured_prompt, :image_count)""",
            kwargs,
        )
        self.conn.commit()

    def insert_images(self, note_id: str, urls: list[str]):
        self.conn.executemany(
            "INSERT OR IGNORE INTO images (note_id, image_index, url) VALUES (?, ?, ?)",
            [(note_id, i, url) for i, url in enumerate(urls)],
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Carousel image extraction
# ---------------------------------------------------------------------------
async def extract_all_images(page: Page) -> list[str]:
    """Extract all image URLs from a note detail page, including carousel."""
    img_urls = await page.evaluate("""
        () => {
            const urls = new Set();
            // All img tags (swiper containers + page-wide)
            document.querySelectorAll('img').forEach(img => {
                const src = img.src || img.dataset.src || img.getAttribute('data-lazyload') || '';
                if (src) urls.add(src);
            });
            // JSON-LD structured data
            document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                try {
                    const data = JSON.parse(s.textContent);
                    const images = Array.isArray(data.image) ? data.image : (data.image ? [data.image] : []);
                    images.forEach(u => urls.add(typeof u === 'string' ? u : u.url || ''));
                } catch(e) {}
            });
            return [...urls].filter(u => u && (
                u.includes('xhscdn') || u.includes('sns-img') || u.includes('ci.xiaohongshu')
            ) && !u.includes('avatar') && !u.includes('emoji') && !u.includes('logo'));
        }
    """)

    # Fallback: click through carousel if indicator shows more images
    indicator = await page.query_selector('[class*="indicator"], [class*="counter"]')
    if indicator:
        text = await indicator.inner_text()
        match = re.search(r'(\d+)\s*/\s*(\d+)', text)
        if match and len(img_urls) < int(match.group(2)):
            for _ in range(int(match.group(2)) - 1):
                btn = await page.query_selector('[class*="next"], [class*="right"], .swiper-button-next')
                if not btn:
                    break
                await btn.click()
                await page.wait_for_timeout(600)
                new = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('img'))
                        .map(img => img.src || img.dataset.src || '')
                        .filter(u => u && (u.includes('xhscdn') || u.includes('sns-img') || u.includes('ci.xiaohongshu'))
                            && !u.includes('avatar') && !u.includes('emoji') && !u.includes('logo'))
                """)
                img_urls = list(set(img_urls + new))

    return img_urls


# ---------------------------------------------------------------------------
# Note metadata extraction
# ---------------------------------------------------------------------------
async def extract_note_metadata(page: Page) -> dict:
    """Extract title, description, author, publish time from note detail page."""
    return await page.evaluate("""
        () => {
            const getText = (sels) => {
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
                }
                return '';
            };
            const authorLink = (() => {
                for (const s of ['[class*="author"] a', 'a[href*="/user/profile/"]']) {
                    const el = document.querySelector(s);
                    if (el) return el.getAttribute('href') || '';
                }
                return '';
            })();
            const m = authorLink.match(/profile\\/([a-f0-9]+)/);
            return {
                title: getText(['#detail-title', '[class*="note-title"]', '.title', 'h1']),
                description: getText(['#detail-desc', '[class*="note-text"]', '[class*="desc"]', '[class*="content"]']),
                authorName: getText(['[class*="author"] [class*="name"]', '.username', '[class*="nickname"]']),
                authorId: m ? m[1] : '',
                publishTime: getText(['[class*="date"]', '[class*="time"]', 'time']),
            };
        }
    """)


# ---------------------------------------------------------------------------
# Search results scrolling
# ---------------------------------------------------------------------------
async def scroll_and_collect_notes(page: Page, max_notes: int) -> list[str]:
    """Scroll search results page and collect note URLs."""
    seen = set()
    collected = []

    for _ in range(20):
        cards = await page.query_selector_all("section.note-item a.cover")
        if not cards:
            cards = await page.query_selector_all("a[href*='/search_result/']")
        if not cards:
            cards = await page.query_selector_all("a[href*='/explore/']")
        if not cards:
            cards = await page.query_selector_all("a[href*='/note/']")

        prev_count = len(seen)
        for card in cards:
            href = await card.get_attribute("href")
            if href and href not in seen:
                seen.add(href)
                collected.append(href)

        if len(collected) >= max_notes:
            break

        await page.evaluate("window.scrollBy(0, 800)")
        await page.wait_for_timeout(1500)

        if len(seen) == prev_count:
            break  # No new content

    return collected[:max_notes]


# ---------------------------------------------------------------------------
# LLM prompt extraction (inline, returns None if no valid prompt)
# ---------------------------------------------------------------------------
MODEL_ALIASES = {
    "Midjourney": ["midjourney", "mj", "mid journey", "mid-journey"],
    "FLUX": ["flux", "flux.1", "flux1", "flux 1", "flux.2", "flux 2", "flux2",
             "flux.2 klein", "flux klein", "flux pro", "flux dev", "flux schnell"],
    "Seedream": ["seedream", "seed dream", "seedream5", "seedream5.0",
                 "seedream5.0 lite", "seedream 5.0", "seedance", "seedance2.0",
                 "seedream5.0 lite / seedance2.0"],
    "NanoBanana": ["nanobanana", "nano banana", "nano-banana", "banana", "banana2",
                   "banana pro", "bananapro", "banana-pro", "banana 2",
                   "nano banana 2", "lovart", "lovart (nano banana 2)",
                   "banana (推测为 banana2/bananapro)"],
}

# Build reverse lookup: lowercase variant -> canonical name
_MODEL_LOOKUP: dict[str, str] = {}
for canonical, aliases in MODEL_ALIASES.items():
    _MODEL_LOOKUP[canonical.lower()] = canonical
    for a in aliases:
        _MODEL_LOOKUP[a.lower()] = canonical


VALID_MODELS = set(MODEL_ALIASES.keys())


def normalize_model(raw: str) -> str:
    """Map model name to canonical form. Return '' if not in whitelist."""
    canonical = _MODEL_LOOKUP.get(raw.strip().lower(), raw.strip())
    return canonical if canonical in VALID_MODELS else ""


PROMPT_SYSTEM = """你是 AI 图像生成提示词提取专家。从小红书笔记中提取提示词信息。
如果笔记包含 AI 图像生成提示词，返回严格 JSON（不要 markdown 代码块）：
{"prompt_en": "英文提示词", "prompt_cn": "中文提示词", "model": "模型名", "parameters": "参数", "style_tags": ["标签"]}
- prompt_en / prompt_cn: 至少有一个非空
- model: 必须是以下之一（严格匹配）: Midjourney, FLUX, Seedream, NanoBanana
  - 别名对照: MJ/Mid Journey → Midjourney, Flux.1/Flux1 → FLUX, Nano Banana → NanoBanana, Seed Dream → Seedream
  - 如果笔记中的模型不属于以上任何一个，model 填空字符串 ""
- parameters: 如 --ar 16:9, --v 6, steps, cfg 等
- style_tags: 风格关键词列表

如果笔记不包含任何 AI 图像生成提示词，返回空 JSON: {}"""


async def extract_prompt(client: httpx.AsyncClient, title: str, description: str) -> str | None:
    """Call LLM to extract structured prompt. Returns JSON string or None if no prompt found."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "anthropic/claude-sonnet-4-5",
                "messages": [
                    {"role": "system", "content": PROMPT_SYSTEM},
                    {"role": "user", "content": f"标题: {title or '无'}\n\n正文: {description or '无'}"},
                ],
                "temperature": 0,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"    ⚠️ LLM API {resp.status_code}")
            return None
        result = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code block if present
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\s*', '', result)
            result = re.sub(r'\s*```$', '', result)
        parsed = json.loads(result)
        # Empty dict = no prompt found
        if not parsed or (not parsed.get("prompt_en") and not parsed.get("prompt_cn")):
            return None
        # Normalize model name
        if parsed.get("model"):
            parsed["model"] = normalize_model(parsed["model"])
        return json.dumps(parsed, ensure_ascii=False)
    except Exception as e:
        print(f"    ⚠️ LLM: {e}")
        return None


# ---------------------------------------------------------------------------
# Note ID extraction
# ---------------------------------------------------------------------------
def extract_note_id(url: str) -> str | None:
    match = re.search(r'/(?:explore|discovery/item|note|search_result)/([a-fA-F0-9]{24})', url)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="小红书笔记全量采集脚本")
    parser.add_argument("--keyword", required=True, help="搜索关键词")
    parser.add_argument("--category", required=True, help="分类标签")
    parser.add_argument("--max-notes", type=int, default=20, help="最大采集数（默认 20）")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--delay", type=float, default=3, help="笔记间延迟秒数（默认 3）")
    parser.add_argument("--db", default="xhs_notes.db", help="数据库路径")
    args = parser.parse_args()

    if not COOKIE_FILE.exists():
        print("❌ 未找到登录状态！请先运行：uv run save_login.py")
        sys.exit(1)

    db = Database(str(BASE_DIR / args.db))
    print(f"📋 keyword={args.keyword}, category={args.category}, max={args.max_notes}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(
            storage_state=str(COOKIE_FILE),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        search_url = f"https://www.xiaohongshu.com/search_result?keyword={args.keyword}&type=51"
        print(f"🔍 搜索: {args.keyword}")
        await page.goto(search_url)
        await page.wait_for_timeout(4000)

        if await page.query_selector("input[placeholder*='手机号'], .login-container"):
            print("⚠️  Cookie 已过期，请重新运行 save_login.py")
            await browser.close()
            db.close()
            sys.exit(1)

        print("⏳ 收集笔记链接...")
        note_urls = await scroll_and_collect_notes(page, args.max_notes)
        print(f"✅ 找到 {len(note_urls)} 条笔记")
        if note_urls:
            print(f"  🔗 示例链接: {note_urls[0][:100]}")

        new_count = 0
        skipped = 0
        rejected = 0

        async with httpx.AsyncClient() as client:
            for idx, note_href in enumerate(note_urls, 1):
                note_id = extract_note_id(note_href)
                if not note_id:
                    print(f"  ⚠️ [{idx}/{len(note_urls)}] 无法解析 note_id: {note_href[:80]}")
                    continue

                if db.note_exists(note_id):
                    print(f"  ⏭️  [{idx}/{len(note_urls)}] 跳过: {note_id}")
                    skipped += 1
                    continue

                full_url = f"https://www.xiaohongshu.com{note_href}" if note_href.startswith("/") else note_href
                print(f"\n📌 [{idx}/{len(note_urls)}] {note_id}")

                try:
                    await page.goto(full_url)
                    await page.wait_for_timeout(3000)

                    # Detect error/block pages
                    page_text = await page.evaluate("document.body?.innerText?.slice(0, 200) || ''")
                    if "无法浏览" in page_text or "已被删除" in page_text or "不存在" in page_text:
                        print(f"  ⚠️ 页面不可用，跳过")
                        rejected += 1
                        await asyncio.sleep(1)
                        continue

                    meta = await extract_note_metadata(page)
                    img_urls = await extract_all_images(page)
                    title = meta.get("title", "")
                    desc = meta.get("description", "")

                    print(f"  📝 {(title or '无')[:50]}")
                    print(f"  👤 {meta.get('authorName') or '未知'}  🖼️ {len(img_urls)} 张")

                    # LLM quality check: extract prompt, skip if none
                    print(f"  🤖 提取提示词...")
                    prompt_json = await extract_prompt(client, title, desc)
                    if not prompt_json:
                        print(f"  ⛔ 无有效提示词，跳过")
                        rejected += 1
                        await asyncio.sleep(1)
                        continue

                    print(f"  ✅ 提示词已提取")

                    canonical_url = f"https://www.xiaohongshu.com/explore/{note_id}"
                    db.insert_note(
                        note_id=note_id, url=canonical_url,
                        title=title, description=desc,
                        author_name=meta.get("authorName", ""), author_id=meta.get("authorId", ""),
                        publish_time=meta.get("publishTime", ""),
                        category=args.category, search_keyword=args.keyword,
                        structured_prompt=prompt_json,
                        image_count=len(img_urls),
                    )
                    db.insert_images(note_id, img_urls)
                    new_count += 1

                except Exception as e:
                    print(f"  ❌ {e}")
                    continue

                delay = args.delay + random.uniform(-1, 1)
                await asyncio.sleep(max(1, delay))

        await browser.close()

    print(f"\n{'=' * 40}")
    print(f"🎉 完成: {new_count} 入库, {rejected} 无提示词丢弃, {skipped} 已存在跳过")
    print(f"💾 数据库: {args.db}")
    print(f"{'=' * 40}")

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
