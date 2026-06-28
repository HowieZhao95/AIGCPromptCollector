"""
修复历史数据中的坏标题

对 title 为无意义占位符的笔记，用 LLM 根据已有 structured_prompt 内容生成描述性标题。

用法：
  uv run fix_bad_titles.py
  uv run fix_bad_titles.py --dry-run
"""

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

DB_PATH = str(Path(__file__).parent / "xhs_notes.db")
LLM_MODEL = "xiaomi/mimo-v2.5"

BAD_TITLES: set[str] = {
    "温馨提示", "跟风一下", "效果好的，付费没问题",
    "寄蜉蝣于天地，渺沧海之一粟", "4.3词", "4.0关键词",
    "软装拆解", "夜话乞巧", "荷花泉水",
}

AI_KEYWORDS = ["ai", "AI", "提示词", "渲染", "建筑", "室内", "景观", "效果图", "生图", "Banana", "Flux", "MJ"]


def is_bad_title(title: str) -> bool:
    if not title or title.strip() in BAD_TITLES:
        return True
    if len(title.strip()) <= 4 and not any(k in title for k in AI_KEYWORDS):
        return True
    return False


TITLE_SYSTEM = """你是 AI 提示词内容专家。根据提供的 AI 生图/视频提示词内容，生成一个简洁的中文标题（10-20字）。
标题要描述这条提示词的用途、风格或场景，例如："GPT Image2建筑摄影写实提示词"、"极简现代室内渲染提示词"、"Seedance城市空镜运镜提示词"。
只返回标题文字，不要任何解释或标点以外的内容。"""


async def generate_title(client: httpx.AsyncClient, sp: dict) -> str | None:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None

    content_parts = []
    if sp.get("prompt_cn"):
        content_parts.append(f"中文提示词: {sp['prompt_cn'][:300]}")
    if sp.get("prompt_en"):
        content_parts.append(f"英文提示词: {sp['prompt_en'][:300]}")
    if sp.get("model"):
        content_parts.append(f"模型: {sp['model']}")
    if sp.get("style_tags"):
        content_parts.append(f"风格标签: {', '.join(sp['style_tags'][:8])}")

    if not content_parts:
        return None

    try:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": TITLE_SYSTEM},
                    {"role": "user", "content": "\n".join(content_parts)},
                ],
                "temperature": 0.3,
                "max_tokens": 4096,
            },
            timeout=30,
        )
        body = resp.json()
        if resp.status_code != 200 or "choices" not in body:
            print(f"    ⚠️ API {resp.status_code}: {body.get('error', str(body)[:200])}")
            return None
        msg = body["choices"][0]["message"]
        title = (msg.get("content") or "").strip()
        if not title:
            print(f"    ⚠️ 空 content，finish_reason={body['choices'][0].get('finish_reason')}, reasoning_len={len(msg.get('reasoning') or '')}")
            return None
        # Clean up any quotes or extra text
        title = title.strip('"\'「」')
        return title if title else None
    except Exception as e:
        print(f"    ⚠️ LLM error: {e}")
        return None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout = 10000")
    rows = conn.execute(
        "SELECT note_id, title, structured_prompt FROM notes WHERE structured_prompt IS NOT NULL"
    ).fetchall()

    bad = [(nid, t, sp) for nid, t, sp in rows if is_bad_title(t)]
    print(f"发现坏标题：{len(bad)} 条")
    for _, t, _ in bad:
        print(f"  {t!r}")

    if not bad:
        conn.close()
        return

    if args.dry_run:
        print("\n(dry-run，不执行修复)")
        conn.close()
        return

    print(f"\n开始生成标题...")
    results: list[tuple[str, str, str]] = []  # (note_id, old_title, new_title)

    async with httpx.AsyncClient() as client:
        for note_id, old_title, sp_json in bad:
            try:
                sp = json.loads(sp_json)
            except Exception:
                print(f"  ⚠️ JSON 解析失败: {old_title!r}")
                continue
            new_title = await generate_title(client, sp)
            if new_title:
                results.append((note_id, old_title, new_title))
                print(f"  ✅ {old_title!r} → {new_title!r}")
            else:
                print(f"  ⚠️ 生成失败，保留原标题: {old_title!r}")

    # 批量写入
    updated = 0
    for note_id, _, new_title in results:
        conn.execute("UPDATE notes SET title = ? WHERE note_id = ?", (new_title, note_id))
        updated += 1
    conn.commit()
    conn.close()
    print(f"\n完成：{updated}/{len(bad)} 条标题已修复")


if __name__ == "__main__":
    asyncio.run(main())
