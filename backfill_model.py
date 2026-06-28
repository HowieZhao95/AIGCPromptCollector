"""
批量回填历史数据的 model 字段

阶段一：关键词匹配（GPT Image 2 等有明显特征的模型）
阶段二：剩余 model 为空的，按提示词内容推断图片/视频，兜底填 NanoBanana 或 Seedance

用法：
  uv run backfill_model.py
  uv run backfill_model.py --dry-run
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "xhs_notes.db")

# ── 阶段一：关键词规则 ──────────────────────────────────────────────────────
RULES: list[tuple[str, list[str]]] = [
    (
        "GPT Image 2",
        [
            r"gpt[-_\s]?image[-_\s]?2",
            r"gpt[-_\s]?image(?!\s*[3-9])",
            r"image[-_\s]?2(?!\s*[0-9])",
            r"openai\s+image",
            r"chatgpt\s+image",
            r"#image[-_]?2\b",
            r"oai\s+image",
        ],
    ),
]

EXCLUDE_PATTERNS = [
    r"image2\.0",
    r"image\s*2\.0",
]


def matches_rule(text: str, patterns: list[str]) -> bool:
    lower = text.lower()
    for p in EXCLUDE_PATTERNS:
        if re.search(p, lower):
            return False
    for p in patterns:
        if re.search(p, lower, re.IGNORECASE):
            return True
    return False


# ── 阶段二：提示词内容推断兜底 ────────────────────────────────────────────
_VIDEO_KEYWORDS = [
    "视频", "短片", "短剧", "运动镜头", "fps", "帧率",
    "分镜", "故事板", "storyboard", "video", "animation",
    "motion", "clip", "duration", "seedance", "即梦", "runway",
    "sora", "kling", "可灵", "文生视频", "图生视频", "veo",
]


def infer_default_model(sp: dict) -> str:
    combined = " ".join([
        sp.get("prompt_cn") or "",
        sp.get("prompt_en") or "",
        sp.get("parameters") or "",
        " ".join(sp.get("style_tags") or []),
    ]).lower()
    return "Seedance" if any(kw in combined for kw in _VIDEO_KEYWORDS) else "NanoBanana"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT note_id, title, description, structured_prompt FROM notes "
        "WHERE structured_prompt IS NOT NULL "
        "AND json_extract(structured_prompt, '$.model') = ''"
    ).fetchall()

    print(f"待处理（model 为空）：{len(rows)} 条\n")

    phase1: list[tuple[str, str, str]] = []   # (note_id, model, title)
    phase2: list[tuple[str, str, str]] = []

    for note_id, title, description, sp_json in rows:
        combined = (title or "") + " " + (description or "")
        matched = None
        for canonical, patterns in RULES:
            if matches_rule(combined, patterns):
                matched = canonical
                break

        if matched:
            phase1.append((note_id, matched, title or ""))
        else:
            try:
                sp = json.loads(sp_json)
                model = infer_default_model(sp)
            except Exception:
                model = "NanoBanana"
            phase2.append((note_id, model, title or ""))

    print(f"[阶段一] 关键词命中：{len(phase1)} 条")
    for _, m, t in phase1:
        print(f"  [{m}] {t[:60]}")

    print(f"\n[阶段二] 内容推断兜底：{len(phase2)} 条")
    seedance = [(n, m, t) for n, m, t in phase2 if m == "Seedance"]
    nanobanana = [(n, m, t) for n, m, t in phase2 if m == "NanoBanana"]
    print(f"  Seedance: {len(seedance)} 条")
    for _, m, t in seedance[:10]:
        print(f"    {t[:60]}")
    print(f"  NanoBanana: {len(nanobanana)} 条")

    all_updates = phase1 + phase2
    print(f"\n共更新：{len(all_updates)} 条")

    if args.dry_run:
        print("(dry-run，不执行写入)")
        conn.close()
        return

    updated = 0
    for note_id, model, _ in all_updates:
        row = conn.execute(
            "SELECT structured_prompt FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        if not row:
            continue
        try:
            data = json.loads(row[0])
        except Exception:
            continue
        data["model"] = model
        conn.execute(
            "UPDATE notes SET structured_prompt = ? WHERE note_id = ?",
            (json.dumps(data, ensure_ascii=False), note_id),
        )
        updated += 1

    conn.commit()
    conn.close()
    print(f"✅ 已更新 {updated} 条记录")


if __name__ == "__main__":
    main()
