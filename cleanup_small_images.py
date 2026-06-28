"""
清理数据库中的小尺寸图片（缩略图/图标）

通过 HTTP Range 请求只获取图片头部字节来解析尺寸，避免下载完整图片。
支持格式：WebP (VP8/VP8L)、PNG、JPEG（部分）

用法：
  uv run cleanup_small_images.py
  uv run cleanup_small_images.py --min-size 300 --dry-run
"""

import argparse
import asyncio
import sqlite3
import struct
from pathlib import Path

import httpx

DB_PATH = str(Path(__file__).parent / "xhs_notes.db")
DEFAULT_MIN_SIZE = 200  # 最小尺寸（宽或高），低于此值的图片将被删除
CONCURRENCY = 20        # 并发请求数
HEADER_BYTES = 64       # 只读取前 64 字节用于解析尺寸


def parse_dimensions(data: bytes) -> tuple[int, int] | None:
    """从图片头部字节解析 (width, height)，无法解析则返回 None。"""
    if len(data) < 12:
        return None

    # PNG: magic 8 bytes + IHDR chunk (width at [16:20], height at [20:24])
    if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 24:
        w = struct.unpack('>I', data[16:20])[0]
        h = struct.unpack('>I', data[20:24])[0]
        return w, h

    # WebP
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP' and len(data) >= 20:
        chunk_type = data[12:16]

        # VP8 (lossy): frame_tag(3) + start_code(3) + width(2) + height(2)
        if chunk_type == b'VP8 ' and len(data) >= 30:
            if data[23:26] == b'\x9d\x01\x2a':
                w = struct.unpack('<H', data[26:28])[0] & 0x3FFF
                h = struct.unpack('<H', data[28:30])[0] & 0x3FFF
                return w, h

        # VP8L (lossless): signature(1) + packed bits
        if chunk_type == b'VP8L' and len(data) >= 25:
            if data[20] == 0x2F:
                bits = struct.unpack('<I', data[21:25])[0]
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return w, h

        # VP8X (extended): flags(4) + canvas width-1 (24-bit LE) + canvas height-1 (24-bit LE)
        if chunk_type == b'VP8X' and len(data) >= 30:
            w = struct.unpack('<I', data[24:27] + b'\x00')[0] + 1
            h = struct.unpack('<I', data[27:30] + b'\x00')[0] + 1
            return w, h

    # JPEG: 0xFF 0xD8 magic — scanning SOF markers requires more data; return None (keep image)
    if data[:2] == b'\xff\xd8':
        return None

    return None


async def get_image_size(client: httpx.AsyncClient, url: str) -> tuple[int, int] | None:
    """获取图片尺寸，只下载头部字节。返回 None 表示无法判断（保留该图片）。"""
    try:
        resp = await client.get(
            url,
            headers={"Range": f"bytes=0-{HEADER_BYTES - 1}"},
            follow_redirects=True,
            timeout=10,
        )
        if resp.status_code not in (200, 206):
            return None
        return parse_dimensions(resp.content)
    except Exception:
        return None


async def check_batch(
    client: httpx.AsyncClient,
    batch: list[tuple[int, str]],  # [(id, url), ...]
    min_size: int,
) -> list[int]:
    """返回尺寸过小的图片 id 列表。"""
    tasks = [get_image_size(client, url) for _, url in batch]
    results = await asyncio.gather(*tasks)
    small_ids = []
    for (img_id, url), dims in zip(batch, results):
        if dims is not None:
            w, h = dims
            if w < min_size or h < min_size:
                small_ids.append(img_id)
                print(f"  ✗ {w}x{h}  {url[:80]}")
    return small_ids


async def main():
    parser = argparse.ArgumentParser(description="清理数据库中的小尺寸图片")
    parser.add_argument("--min-size", type=int, default=DEFAULT_MIN_SIZE,
                        help=f"最小尺寸阈值（默认 {DEFAULT_MIN_SIZE}px）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检测不删除")
    parser.add_argument("--db", default=DB_PATH, help="数据库路径")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys=ON")

    rows = conn.execute("SELECT id, url FROM images ORDER BY id").fetchall()
    total = len(rows)
    print(f"共 {total} 张图片，最小尺寸阈值: {args.min_size}px")

    all_small_ids: list[int] = []

    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def bounded_check(batch):
            async with sem:
                return await check_batch(client, batch, args.min_size)

        # Split into batches of CONCURRENCY
        batches = [rows[i:i + CONCURRENCY] for i in range(0, total, CONCURRENCY)]
        for i, batch in enumerate(batches):
            print(f"\n[{i * CONCURRENCY + 1}-{min((i + 1) * CONCURRENCY, total)}/{total}]")
            small_ids = await bounded_check(batch)
            all_small_ids.extend(small_ids)

    print(f"\n{'=' * 50}")
    print(f"发现小图: {len(all_small_ids)} 张 / {total} 张")

    if not all_small_ids:
        print("没有需要清理的图片。")
        conn.close()
        return

    if args.dry_run:
        print("(dry-run 模式，不执行删除)")
        conn.close()
        return

    # 删除小图记录
    placeholders = ",".join("?" * len(all_small_ids))
    # 先获取受影响的 note_id
    affected_notes = conn.execute(
        f"SELECT DISTINCT note_id FROM images WHERE id IN ({placeholders})",
        all_small_ids,
    ).fetchall()

    conn.execute(f"DELETE FROM images WHERE id IN ({placeholders})", all_small_ids)
    conn.commit()
    print(f"已删除 {len(all_small_ids)} 张小图")

    # 更新受影响笔记的 image_count
    for (note_id,) in affected_notes:
        count = conn.execute(
            "SELECT COUNT(*) FROM images WHERE note_id = ?", (note_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE notes SET image_count = ? WHERE note_id = ?", (count, note_id)
        )
    conn.commit()
    print(f"已更新 {len(affected_notes)} 条笔记的图片数量")

    conn.close()
    print("完成！")


if __name__ == "__main__":
    asyncio.run(main())
