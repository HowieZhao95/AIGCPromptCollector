"""
X (Twitter) 帖子采集脚本

采集帖子的所有图片 URL、文字内容、来源、发布时间，存入 SQLite。
每条帖子经 LLM 提取结构化提示词，无有效提示词的帖子不入库。
与小红书采集共享同一数据库，统一数据格式。

依赖：先运行一次 save_login_x.py 保存 Cookie

用法：
  uv run download_x_prompt.py --keyword "midjourney architecture" --category "建筑"
  uv run download_x_prompt.py --keyword "AI art prompt" --category "综合" --max-notes 50
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

COOKIE_FILE = Path(__file__).parent / "x_auth.json"
BASE_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Database (shared schema with XHS)
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
# Tweet image extraction
# ---------------------------------------------------------------------------
async def extract_all_images(page: Page) -> list[str]:
    """Extract all image/video-thumbnail URLs from a tweet."""
    # Small scroll to trigger lazy-loading
    await page.evaluate("window.scrollBy(0, 300)")
    await page.wait_for_timeout(800)

    return await page.evaluate("""
        () => {
            const urls = new Set();
            const twimgBase = (src) => {
                if (!src || !src.includes('pbs.twimg.com')) return null;
                const base = src.split('?')[0];
                // Media images: request highest quality
                if (base.includes('/media/')) return base + '?format=jpg&name=large';
                // Video thumbnails: already direct JPEG/PNG, keep as-is
                if (base.includes('_video_thumb') ||
                    base.includes('amplify_video') ||
                    base.includes('tweet_video_thumb')) return base;
                return null;
            };

            // All img tags (covers lazy-loaded via src / data-src)
            document.querySelectorAll('img').forEach(img => {
                const src = img.src || img.dataset.src || img.getAttribute('data-lazy-src') || '';
                const url = twimgBase(src);
                if (url) urls.add(url);
            });

            // Video poster thumbnails
            document.querySelectorAll('video[poster]').forEach(v => {
                const url = twimgBase(v.getAttribute('poster') || '');
                if (url) urls.add(url);
            });

            // Twitter card images
            document.querySelectorAll(
                '[data-testid="card.layoutLarge.media"] img, [data-testid="card.layoutSmall.media"] img'
            ).forEach(img => {
                const url = twimgBase(img.src || '');
                if (url) urls.add(url);
            });

            return [...urls];
        }
    """)


# ---------------------------------------------------------------------------
# Tweet metadata extraction
# ---------------------------------------------------------------------------
async def extract_tweet_metadata(page: Page) -> dict:
    """Extract text, author, publish time from a tweet detail page."""
    return await page.evaluate("""
        () => {
            // Tweet text
            const tweetText = document.querySelector('[data-testid="tweetText"]');
            const description = tweetText ? tweetText.innerText.trim() : '';

            // Author name and handle
            const userNames = document.querySelectorAll('[data-testid="User-Name"]');
            let authorName = '', authorId = '';
            if (userNames.length > 0) {
                const spans = userNames[0].querySelectorAll('span');
                for (const s of spans) {
                    const t = s.textContent.trim();
                    if (t.startsWith('@')) { authorId = t.slice(1); }
                    else if (t && !authorName && t !== '·' && !t.includes('Replying')) { authorName = t; }
                }
            }

            // Publish time
            const timeEl = document.querySelector('article time');
            const publishTime = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText.trim()) : '';

            // Title = first line of tweet (up to 100 chars)
            const title = description.split('\\n')[0].slice(0, 100);

            return { title, description, authorName, authorId, publishTime };
        }
    """)


# ---------------------------------------------------------------------------
# Search results scrolling
# ---------------------------------------------------------------------------
async def scroll_and_collect_tweets(page: Page, max_notes: int) -> list[dict]:
    """Scroll search results and collect tweet URLs with IDs."""
    seen = set()
    collected = []

    for _ in range(30):
        # Extract tweet links from the timeline
        links = await page.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('a[href*="/status/"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/^\\/([\\w]+)\\/status\\/(\\d+)/);
                    if (m && !href.includes('/photo/') && !href.includes('/analytics')) {
                        results.push({ href, tweetId: m[2], author: m[1] });
                    }
                });
                return results;
            }
        """)

        prev_count = len(seen)
        for link in links:
            tid = link["tweetId"]
            if tid not in seen:
                seen.add(tid)
                collected.append(link)

        if len(collected) >= max_notes:
            break

        await page.evaluate("window.scrollBy(0, 800)")
        await page.wait_for_timeout(2000)

        if len(seen) == prev_count:
            # Try one more scroll in case of lazy loading
            await page.evaluate("window.scrollBy(0, 1200)")
            await page.wait_for_timeout(2000)
            if len(seen) == prev_count:
                break

    return collected[:max_notes]


# ---------------------------------------------------------------------------
# LLM prompt extraction (shared logic with XHS scraper)
# ---------------------------------------------------------------------------
MODEL_ALIASES = {
    "Midjourney": ["midjourney", "mj", "mid journey", "mid-journey"],
    "FLUX": ["flux", "flux.1", "flux1", "flux 1", "flux.2", "flux 2", "flux2",
             "flux.2 klein", "flux klein", "flux pro", "flux dev", "flux schnell"],
    "Seedream": ["seedream", "seed dream", "seedream5", "seedream5.0",
                 "seedream5.0 lite", "seedream 5.0"],
    "Seedance": ["seedance", "seedance2.0", "seedance2", "seedance 2.0",
                 "seedance 2", "seedance1.0", "seedance1", "seedance 1.0",
                 "seed dance", "seedream5.0 lite / seedance2.0"],
    "NanoBanana": ["nanobanana", "nano banana", "nano-banana", "banana", "banana2",
                   "banana pro", "bananapro", "banana-pro", "banana 2",
                   "nano banana 2", "lovart", "lovart (nano banana 2)",
                   "banana (推测为 banana2/bananapro)"],
}

_MODEL_LOOKUP: dict[str, str] = {}
for canonical, aliases in MODEL_ALIASES.items():
    _MODEL_LOOKUP[canonical.lower()] = canonical
    for a in aliases:
        _MODEL_LOOKUP[a.lower()] = canonical

VALID_MODELS = set(MODEL_ALIASES.keys())


def normalize_model(raw: str) -> str:
    canonical = _MODEL_LOOKUP.get(raw.strip().lower(), raw.strip())
    return canonical if canonical in VALID_MODELS else ""


PROMPT_SYSTEM = """你是 AI 图像/视频生成提示词提取专家。从社交媒体帖子中提取提示词信息。
如果帖子包含 AI 图像或视频生成提示词，返回严格 JSON（不要 markdown 代码块）：
{"prompt_en": "英文提示词", "prompt_cn": "中文提示词", "model": "模型名", "parameters": "参数", "style_tags": ["标签"]}
- prompt_en / prompt_cn: 至少有一个非空
- model: 必须是以下之一（严格匹配）: Midjourney, FLUX, Seedream, Seedance, NanoBanana
  - 别名对照: MJ/Mid Journey → Midjourney, Flux.1/Flux1 → FLUX, Nano Banana → NanoBanana, Seed Dream → Seedream, Seedance2.0/Seed Dance → Seedance
  - Seedream 是字节跳动图像生成模型，Seedance 是字节跳动视频生成模型，注意区分
  - 如果帖子中的模型不属于以上任何一个，model 填空字符串 ""
- parameters: 如 --ar 16:9, --v 6, steps, cfg, 分辨率, 时长等
- style_tags: 风格关键词列表

如果帖子不包含任何 AI 图像或视频生成提示词，返回空 JSON: {}"""


async def extract_prompt(client: httpx.AsyncClient, title: str, description: str) -> str | None:
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
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\s*', '', result)
            result = re.sub(r'\s*```$', '', result)
        parsed = json.loads(result)
        if not parsed or (not parsed.get("prompt_en") and not parsed.get("prompt_cn")):
            return None
        if parsed.get("model"):
            parsed["model"] = normalize_model(parsed["model"])
        return json.dumps(parsed, ensure_ascii=False)
    except Exception as e:
        print(f"    ⚠️ LLM: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="X (Twitter) 帖子采集脚本")
    parser.add_argument("--keyword", required=True, help="搜索关键词")
    parser.add_argument("--category", required=True, help="分类标签")
    parser.add_argument("--max-notes", type=int, default=20, help="最大采集数（默认 20）")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--delay", type=float, default=3, help="帖子间延迟秒数（默认 3）")
    parser.add_argument("--db", default="xhs_notes.db", help="数据库路径")
    args = parser.parse_args()

    if not COOKIE_FILE.exists():
        print("❌ 未找到登录状态！请先运行：uv run save_login_x.py")
        sys.exit(1)

    db = Database(str(BASE_DIR / args.db))
    print(f"📋 keyword={args.keyword}, category={args.category}, max={args.max_notes}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(
            storage_state=str(COOKIE_FILE),
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Search with image filter
        search_url = f"https://x.com/search?q={args.keyword}&src=typed_query&f=top"
        print(f"🔍 搜索: {args.keyword}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)

        # Login check
        if page.url.startswith("https://x.com/i/flow/login") or "/login" in page.url:
            print("⚠️  Cookie 已过期，请重新运行 save_login_x.py")
            await browser.close()
            db.close()
            sys.exit(1)

        print("⏳ 收集帖子链接...")
        tweet_links = await scroll_and_collect_tweets(page, args.max_notes)
        print(f"✅ 找到 {len(tweet_links)} 条帖子")
        if tweet_links:
            print(f"  🔗 示例: /{tweet_links[0]['author']}/status/{tweet_links[0]['tweetId']}")

        new_count = 0
        skipped = 0
        rejected = 0

        async with httpx.AsyncClient() as client:
            for idx, link in enumerate(tweet_links, 1):
                tweet_id = link["tweetId"]
                author_handle = link["author"]

                if db.note_exists(tweet_id):
                    print(f"  ⏭️  [{idx}/{len(tweet_links)}] 跳过: {tweet_id}")
                    skipped += 1
                    continue

                full_url = f"https://x.com/{author_handle}/status/{tweet_id}"
                print(f"\n📌 [{idx}/{len(tweet_links)}] {tweet_id}")

                try:
                    await page.goto(full_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(3000)

                    # Detect error pages
                    page_text = await page.evaluate("document.body?.innerText?.slice(0, 300) || ''")
                    if "doesn't exist" in page_text or "suspended" in page_text or "this page" in page_text.lower() and "doesn" in page_text.lower():
                        print(f"  ⚠️ 页面不可用，跳过")
                        rejected += 1
                        await asyncio.sleep(1)
                        continue

                    meta = await extract_tweet_metadata(page)
                    img_urls = await extract_all_images(page)
                    title = meta.get("title", "")
                    desc = meta.get("description", "")

                    print(f"  📝 {(title or '无')[:50]}")
                    print(f"  👤 @{meta.get('authorId') or author_handle}  🖼️ {len(img_urls)} 张")

                    # LLM quality check
                    print(f"  🤖 提取提示词...")
                    prompt_json = await extract_prompt(client, title, desc)
                    if not prompt_json:
                        print(f"  ⛔ 无有效提示词，跳过")
                        rejected += 1
                        await asyncio.sleep(1)
                        continue

                    print(f"  ✅ 提示词已提取")

                    db.insert_note(
                        note_id=tweet_id, url=full_url,
                        title=title, description=desc,
                        author_name=meta.get("authorName", ""),
                        author_id=meta.get("authorId", author_handle),
                        publish_time=meta.get("publishTime", ""),
                        category=args.category, search_keyword=args.keyword,
                        structured_prompt=prompt_json,
                        image_count=len(img_urls),
                    )
                    db.insert_images(tweet_id, img_urls)
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
