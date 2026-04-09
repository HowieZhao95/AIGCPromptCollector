"""
AIGC Prompt Collector — FastAPI 后端
启动: uv run app.py → http://localhost:8000
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates

app = FastAPI(title="AIGC Prompt Collector")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DB_PATH = str(Path(__file__).parent / "xhs_notes.db")
ENV_FILE = Path(__file__).parent / ".env"

PLATFORMS = {
    "xhs": {
        "name": "小红书",
        "scraper": str(Path(__file__).parent / "download_xhs_prompt.py"),
        "cookie": Path(__file__).parent / "xhs_auth.json",
        "login_script": "save_login.py",
        "login_url": "https://www.xiaohongshu.com",
    },
    "x": {
        "name": "X (Twitter)",
        "scraper": str(Path(__file__).parent / "download_x_prompt.py"),
        "cookie": Path(__file__).parent / "x_auth.json",
        "login_script": "save_login_x.py",
        "login_url": "https://x.com/home",
    },
}

# In-memory runtime state for running tasks (process handle + live logs)
_running: dict[str, dict] = {}
# Track currently executing schedule IDs to prevent double-fire
_active_schedules: set[str] = set()
# Shared HTTP client for image proxy (created at startup)
_http_client: httpx.AsyncClient | None = None
# Active login sessions: platform -> { browser, context, page }
_login_sessions: dict[str, dict] = {}
# Recent auto-login results: platform -> { status, message }
_login_results: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_prompt(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT,
    description TEXT, author_name TEXT, author_id TEXT,
    publish_time TEXT, category TEXT NOT NULL, search_keyword TEXT NOT NULL,
    structured_prompt TEXT, image_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT NOT NULL REFERENCES notes(note_id),
    image_index INTEGER NOT NULL, url TEXT NOT NULL,
    UNIQUE(note_id, image_index)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id  TEXT PRIMARY KEY,
    keyword  TEXT NOT NULL,
    category TEXT NOT NULL,
    max_notes INTEGER DEFAULT 20,
    delay    REAL DEFAULT 3,
    status   TEXT NOT NULL DEFAULT 'running',
    logs     TEXT DEFAULT '',
    platform TEXT DEFAULT 'xhs',
    started_at  TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id    TEXT PRIMARY KEY,
    keyword        TEXT NOT NULL,
    category       TEXT NOT NULL,
    max_notes      INTEGER DEFAULT 20,
    delay          REAL DEFAULT 3,
    interval_hours REAL NOT NULL DEFAULT 24,
    enabled        INTEGER DEFAULT 1,
    platform       TEXT DEFAULT 'xhs',
    last_run_at    TEXT,
    next_run_at    TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);
"""

_db_initialized = False

# Migrations: add columns that may not exist in older databases
_MIGRATIONS = [
    ("tasks", "platform", "ALTER TABLE tasks ADD COLUMN platform TEXT DEFAULT 'xhs'"),
    ("schedules", "platform", "ALTER TABLE schedules ADD COLUMN platform TEXT DEFAULT 'xhs'"),
]


def _run_migrations(conn: sqlite3.Connection):
    """Add missing columns to existing tables (safe to run repeatedly)."""
    for table, column, sql in _MIGRATIONS:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(sql)
    conn.commit()


@contextmanager
def get_db():
    """Yield a DB connection with auto-close on exit."""
    global _db_initialized
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not _db_initialized:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DB_SCHEMA)
        _run_migrations(conn)
        _db_initialized = True
    try:
        yield conn
    finally:
        conn.close()


def dict_row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def dict_rows(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---------------------------------------------------------------------------
# API: Stats
# ---------------------------------------------------------------------------
@app.get("/api/stats")
async def stats():
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) as c FROM notes").fetchone()["c"]
        total_images = db.execute("SELECT COALESCE(SUM(image_count), 0) as c FROM notes").fetchone()["c"]
        categories = dict_rows(db.execute(
            "SELECT category, COUNT(*) as count FROM notes GROUP BY category ORDER BY count DESC"
        ).fetchall())
        models = dict_rows(db.execute(
            """SELECT json_extract(structured_prompt, '$.model') as model, COUNT(*) as count
               FROM notes WHERE structured_prompt IS NOT NULL
                 AND json_extract(structured_prompt, '$.model') IS NOT NULL
                 AND json_extract(structured_prompt, '$.model') != ''
               GROUP BY model ORDER BY count DESC"""
        ).fetchall())
        recent = dict_rows(db.execute(
            "SELECT note_id, title, category, image_count, structured_prompt, created_at FROM notes ORDER BY created_at DESC LIMIT 10"
        ).fetchall())
    for n in recent:
        n["model"] = _parse_prompt(n.pop("structured_prompt")).get("model", "")
    return {"total": total, "total_images": total_images, "categories": categories, "models": models, "recent": recent}


# ---------------------------------------------------------------------------
# API: Categories & Models
# ---------------------------------------------------------------------------
@app.get("/api/categories")
async def list_categories():
    with get_db() as db:
        rows = db.execute("SELECT DISTINCT category FROM notes ORDER BY category").fetchall()
    return [r["category"] for r in rows]


@app.get("/api/models")
async def list_models():
    with get_db() as db:
        rows = db.execute(
            """SELECT DISTINCT json_extract(structured_prompt, '$.model') as model
               FROM notes WHERE structured_prompt IS NOT NULL
                 AND json_extract(structured_prompt, '$.model') IS NOT NULL
                 AND json_extract(structured_prompt, '$.model') != ''
               ORDER BY model"""
        ).fetchall()
    return [r["model"] for r in rows if r["model"]]


# ---------------------------------------------------------------------------
# API: Notes
# ---------------------------------------------------------------------------
@app.get("/api/notes")
async def list_notes(
    category: str | None = None,
    model: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(24, ge=1, le=100),
):
    where, params = [], []
    if category:
        where.append("n.category = ?")
        params.append(category)
    if model:
        where.append("json_extract(n.structured_prompt, '$.model') = ?")
        params.append(model)
    if search:
        where.append("(n.title LIKE ? OR n.description LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) as c FROM notes n{where_sql}", params).fetchone()["c"]
        rows = dict_rows(db.execute(
            f"""SELECT n.note_id, n.url, n.title, n.author_name, n.category, n.image_count,
                       n.structured_prompt, n.created_at,
                       (SELECT url FROM images WHERE note_id = n.note_id ORDER BY image_index LIMIT 1) as thumbnail
                FROM notes n{where_sql}
                ORDER BY n.created_at DESC LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall())

    for row in rows:
        row["model"] = _parse_prompt(row.pop("structured_prompt")).get("model", "")

    return {"total": total, "page": page, "per_page": per_page, "notes": rows}


@app.get("/api/notes/{note_id}")
async def get_note(note_id: str):
    with get_db() as db:
        note = dict_row(db.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone())
        if not note:
            return {"error": "not found"}
        images = dict_rows(db.execute(
            "SELECT image_index, url FROM images WHERE note_id = ? ORDER BY image_index",
            (note_id,)
        ).fetchall())
    note["images"] = images
    note["prompt"] = _parse_prompt(note.get("structured_prompt"))
    return note


# ---------------------------------------------------------------------------
# API: Image Proxy (bypass XHS referer check)
# ---------------------------------------------------------------------------
@app.get("/api/image-proxy")
async def image_proxy(url: str):
    import re as _re
    try:
        # Choose Referer based on image domain
        if "pbs.twimg.com" in url or "twimg.com" in url:
            referer = "https://x.com/"
            req_url = url  # Keep full URL with quality params for Twitter
        else:
            referer = "https://www.xiaohongshu.com/"
            # sns-webpic-qc.xhscdn.com URLs are time-limited; rewrite to stable ci.xiaohongshu.com
            m = _re.match(r'https?://sns-webpic-qc\.xhscdn\.com/\d+/[a-f0-9]+/(.+)', url)
            if m:
                req_url = "https://ci.xiaohongshu.com/" + m.group(1)
            else:
                req_url = url.split("?")[0]
        resp = await _http_client.get(
            req_url,
            headers={
                "Referer": referer,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            follow_redirects=True, timeout=15,
        )
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "image/jpeg")
            return Response(content=resp.content, media_type=content_type)
    except Exception:
        pass
    return Response(status_code=404)


# ---------------------------------------------------------------------------
# API: Tasks (launch scraper + SSE logs, persisted to SQLite)
# ---------------------------------------------------------------------------
def _save_task(task_id: str, **fields):
    """Update task fields in DB."""
    allowed = {"status", "finished_at", "logs"}
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        return
    sets = ", ".join(f"{k} = ?" for k in safe)
    with get_db() as db:
        db.execute(f"UPDATE tasks SET {sets} WHERE task_id = ?", [*safe.values(), task_id])
        db.commit()


async def _launch_scraper(keyword: str, category: str, max_notes: int, delay: float, platform: str = "xhs") -> str:
    """Shared logic: persist task to DB, launch subprocess, stream logs. Returns task_id."""
    task_id = str(uuid.uuid4())[:8]
    started_at = _now()
    scraper_path = PLATFORMS[platform]["scraper"]

    with get_db() as db:
        db.execute(
            """INSERT INTO tasks (task_id, keyword, category, max_notes, delay, status, logs, started_at, platform)
               VALUES (?, ?, ?, ?, ?, 'running', '', ?, ?)""",
            (task_id, keyword, category, max_notes, delay, started_at, platform),
        )
        db.commit()

    rt = {"logs": [], "process": None, "status": "running"}
    _running[task_id] = rt

    async def run():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", scraper_path,
            "--keyword", keyword, "--category", category,
            "--max-notes", str(max_notes), "--delay", str(delay), "--headless",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        rt["process"] = proc
        async for line in proc.stdout:
            rt["logs"].append(line.decode("utf-8", errors="replace").rstrip())
        await proc.wait()
        # If already stopped externally, don't overwrite status
        if rt["status"] == "running":
            rt["status"] = "done" if proc.returncode == 0 else "failed"
        _save_task(task_id, status=rt["status"], finished_at=_now(), logs="\n".join(rt["logs"]))
        _running.pop(task_id, None)

    asyncio.create_task(run())
    return task_id


@app.on_event("startup")
async def _on_startup():
    global _http_client
    # Init DB schema
    with get_db() as db:
        db.execute("UPDATE tasks SET status = 'failed' WHERE status = 'running'")
        db.commit()
    # Shared HTTP client for image proxy
    _http_client = httpx.AsyncClient()
    # Start scheduler
    asyncio.create_task(_scheduler_loop())


@app.on_event("shutdown")
async def _on_shutdown():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


@app.get("/api/platforms")
async def list_platforms():
    result = []
    for key, cfg in PLATFORMS.items():
        result.append({
            "id": key,
            "name": cfg["name"],
            "cookie_exists": cfg["cookie"].exists(),
            "login_script": cfg["login_script"],
        })
    return result


@app.post("/api/tasks")
async def create_task(body: dict):
    platform = body.get("platform", "xhs")
    if platform not in PLATFORMS:
        return {"error": f"不支持的平台: {platform}"}

    pcfg = PLATFORMS[platform]
    if not pcfg["cookie"].exists():
        return {"error": f"未找到 {pcfg['cookie'].name}，请先运行 uv run {pcfg['login_script']}"}

    keyword = body.get("keyword", "")
    category = body.get("category", "")
    if not keyword or not category:
        return {"error": "keyword 和 category 必填"}

    max_notes = body.get("max_notes", 20)
    delay = body.get("delay", 3)
    task_id = await _launch_scraper(keyword, category, max_notes, delay, platform)
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}/logs")
async def task_logs(task_id: str):
    rt = _running.get(task_id)
    if rt:
        async def stream_live():
            sent = 0
            while True:
                while sent < len(rt["logs"]):
                    yield f"data: {rt['logs'][sent]}\n\n"
                    sent += 1
                if rt["status"] != "running":
                    yield f"event: done\ndata: {rt['status']}\n\n"
                    break
                await asyncio.sleep(0.3)
        return StreamingResponse(stream_live(), media_type="text/event-stream")

    with get_db() as db:
        row = db.execute("SELECT logs, status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return Response(status_code=404)

    async def stream_stored():
        for line in (row["logs"] or "").split("\n"):
            if line:
                yield f"data: {line}\n\n"
        yield f"event: done\ndata: {row['status']}\n\n"

    return StreamingResponse(stream_stored(), media_type="text/event-stream")


@app.get("/api/tasks")
async def list_tasks():
    with get_db() as db:
        rows = dict_rows(db.execute(
            "SELECT task_id, keyword, category, max_notes, status, platform, started_at, finished_at FROM tasks ORDER BY started_at DESC"
        ).fetchall())
    for r in rows:
        rt = _running.get(r["task_id"])
        if rt:
            r["status"] = rt["status"]
    return [{"id": r["task_id"], **{k: r[k] for k in ("keyword", "category", "max_notes", "status", "platform", "started_at", "finished_at")}} for r in rows]


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str):
    rt = _running.get(task_id)
    if not rt or rt["status"] != "running":
        return {"error": "任务未在运行"}
    proc = rt.get("process")
    if proc:
        rt["status"] = "stopped"  # Set BEFORE terminate so run() respects it
        proc.terminate()
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    rt = _running.get(task_id)
    if rt and rt["status"] == "running":
        return {"error": "请先停止任务"}
    with get_db() as db:
        db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        db.commit()
    _running.pop(task_id, None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Settings (API Key)
# ---------------------------------------------------------------------------
def _read_env() -> dict[str, str]:
    result = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def _write_env(data: dict[str, str]):
    lines = []
    if ENV_FILE.exists():
        existing_keys = set()
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in data:
                    lines.append(f"{k}={data[k]}")
                    existing_keys.add(k)
                else:
                    lines.append(line)
            else:
                lines.append(line)
        for k, v in data.items():
            if k not in existing_keys:
                lines.append(f"{k}={v}")
    else:
        for k, v in data.items():
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


@app.get("/api/settings")
async def get_settings():
    env = _read_env()
    key = env.get("OPENROUTER_API_KEY", "")
    if len(key) > 14:
        masked = key[:10] + "*" * (len(key) - 14) + key[-4:]
    else:
        masked = ""
    return {"api_key_masked": masked, "api_key_set": bool(key)}


@app.get("/api/cookie-status")
async def cookie_status():
    result = {}
    for key, cfg in PLATFORMS.items():
        if cfg["cookie"].exists():
            size = cfg["cookie"].stat().st_size
            label = f"{size / 1024:.1f} KB" if size > 1024 else f"{size} B"
            result[key] = {"exists": True, "size": label, "name": cfg["name"], "login_script": cfg["login_script"]}
        else:
            result[key] = {"exists": False, "name": cfg["name"], "login_script": cfg["login_script"]}
    return result


# ---------------------------------------------------------------------------
# API: Login
# X (Twitter): read cookies directly from Chrome DB — no browser launch
# XHS: open Playwright browser, auto-detect login, auto-save
# ---------------------------------------------------------------------------

def _extract_x_cookies_from_chrome() -> list[dict]:
    """
    Read X.com cookies directly from Chrome's SQLite cookie database.
    Decrypts using the Chrome Safe Storage key from macOS Keychain.
    Returns a list of cookie dicts compatible with Playwright storage_state.
    """
    cookie_db = (
        Path.home() / "Library" / "Application Support"
        / "Google" / "Chrome" / "Default" / "Cookies"
    )
    if not cookie_db.exists():
        raise FileNotFoundError("Chrome Cookie 数据库未找到，请确认已安装 Google Chrome")

    # Get encryption key from macOS Keychain
    r = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("无法读取 Chrome 加密密钥，请确认 Chrome 已安装并运行过")
    password = r.stdout.strip().encode()
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)

    def decrypt(enc: bytes) -> str:
        if enc[:3] == b"v10":
            enc = enc[3:]
            iv = b" " * 16
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            dec = cipher.decryptor()
            raw = dec.update(enc) + dec.finalize()
            pad = raw[-1]
            # Chrome prepends 32 bytes of random nonce before the actual value
            return raw[32:-pad].decode("utf-8", errors="replace")
        return enc.decode("utf-8", errors="replace")

    # samesite column: -1=unspecified, 0=None, 1=Lax, 2=Strict
    _SAMESITE = {0: "None", 1: "Lax", 2: "Strict"}

    # Copy DB to temp (Chrome may hold a lock on the original)
    tmp_db = tempfile.mktemp(suffix=".db")
    shutil.copy2(cookie_db, tmp_db)
    try:
        conn = sqlite3.connect(tmp_db)
        rows = conn.execute(
            """SELECT host_key, name, path, expires_utc, is_secure, is_httponly,
                      encrypted_value, samesite
               FROM cookies
               WHERE host_key LIKE '%.x.com' OR host_key = 'x.com'
                  OR host_key LIKE '%.twitter.com' OR host_key = 'twitter.com'"""
        ).fetchall()
        conn.close()
    finally:
        Path(tmp_db).unlink(missing_ok=True)

    cookies = []
    for host, name, path, expires_utc, is_secure, is_httponly, enc_value, samesite in rows:
        if not name:
            continue
        try:
            value = decrypt(enc_value)
        except Exception:
            value = ""
        # Chrome epoch → Unix timestamp; use -1 for session/expired cookies
        expires = int(expires_utc / 1_000_000 - 11_644_473_600) if expires_utc else -1
        if expires != -1 and expires < 0:
            expires = -1
        cookies.append({
            "name": name,
            "value": value,
            "domain": host,
            "path": path or "/",
            "expires": expires,
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": _SAMESITE.get(samesite, "Lax"),
        })
    return cookies


async def _close_login_session(session: dict):
    """Close browser/context and clean up temp dir."""
    try:
        if session.get("browser"):
            await session["browser"].close()
        else:
            await session["context"].close()
        await session["pw"].stop()
    except Exception:
        pass


async def _auto_detect_and_save(platform: str):
    """Poll XHS browser until login detected, then auto-save and close."""
    pcfg = PLATFORMS[platform]
    await asyncio.sleep(4)  # Give browser time to open

    max_wait = 300  # 5 min timeout
    elapsed = 0
    while elapsed < max_wait:
        session = _login_sessions.get(platform)
        if not session:
            return
        try:
            page = session["page"]
            url = page.url
            has_modal = await page.evaluate(
                "() => !!document.querySelector('input[placeholder*=\"手机号\"], .login-container, [class*=\"login-modal\"]')"
            )
            if "xiaohongshu.com" in url and not has_modal:
                await session["context"].storage_state(path=str(pcfg["cookie"]))
                _login_results[platform] = {"status": "saved", "message": f"{pcfg['name']} 登录成功"}
                _login_sessions.pop(platform, None)
                await _close_login_session(session)
                print(f"✅ [{platform}] 自动登录成功")
                return
        except Exception as e:
            print(f"[login-detect] {platform}: {e}")
        await asyncio.sleep(2)
        elapsed += 2

    session = _login_sessions.pop(platform, None)
    if session:
        _login_results[platform] = {"status": "timeout", "message": "登录超时，请重试"}
        await _close_login_session(session)


@app.post("/api/login/{platform}")
async def start_login(platform: str):
    """
    X: extract cookies directly from Chrome DB (no browser launch).
    XHS: open Playwright browser and auto-detect login.
    """
    if platform not in PLATFORMS:
        return {"error": f"不支持的平台: {platform}"}
    if platform in _login_sessions:
        return {"error": "该平台已有登录窗口打开中"}
    pcfg = PLATFORMS[platform]
    _login_results.pop(platform, None)

    if platform == "x":
        # Direct cookie extraction — no browser needed
        # Set pending so the frontend poller knows to keep waiting
        _login_results["x"] = {"status": "pending", "message": "正在从 Chrome 读取 Cookie..."}

        async def _extract():
            try:
                cookies = await asyncio.get_event_loop().run_in_executor(
                    None, _extract_x_cookies_from_chrome
                )
                if not cookies:
                    _login_results["x"] = {"status": "error", "message": "未在 Chrome 中找到 X 的登录信息，请先在 Chrome 里登录 x.com"}
                    return
                state = {"cookies": cookies, "origins": []}
                pcfg["cookie"].write_text(json.dumps(state, ensure_ascii=False))
                _login_results["x"] = {"status": "saved", "message": "X (Twitter) Cookie 已从 Chrome 导入"}
                print("✅ [x] Cookie 从 Chrome 直接导入成功")
            except Exception as e:
                _login_results["x"] = {"status": "error", "message": str(e)}

        asyncio.create_task(_extract())
        return {"ok": True}

    # XHS: open browser
    async def _open_browser():
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(pcfg["login_url"])
            _login_sessions[platform] = {
                "pw": pw, "browser": browser, "context": context, "page": page,
            }
            asyncio.create_task(_auto_detect_and_save(platform))
        except Exception as e:
            _login_results[platform] = {"status": "error", "message": f"启动失败: {e}"}
            try:
                await pw.stop()
            except Exception:
                pass

    asyncio.create_task(_open_browser())
    return {"ok": True}


@app.post("/api/login/{platform}/cancel")
async def cancel_login(platform: str):
    """Abort the login session."""
    session = _login_sessions.pop(platform, None)
    if session:
        await _close_login_session(session)
    return {"ok": True}


@app.get("/api/login/{platform}/status")
async def login_status(platform: str):
    """Poll login progress."""
    result = _login_results.get(platform)
    if platform in _login_sessions:
        return {"active": True, "status": _login_sessions[platform]["status"], "result": result}
    return {"active": False, "result": result}


@app.put("/api/settings")
async def update_settings(body: dict):
    key = body.get("api_key", "").strip()
    if not key:
        return {"error": "API Key 不能为空"}
    env = _read_env()
    env["OPENROUTER_API_KEY"] = key
    _write_env(env)
    os.environ["OPENROUTER_API_KEY"] = key
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Schedules (定时任务 CRUD)
# ---------------------------------------------------------------------------
@app.post("/api/schedules")
async def create_schedule(body: dict):
    sid = str(uuid.uuid4())[:8]
    keyword = body.get("keyword", "")
    category = body.get("category", "")
    if not keyword or not category:
        return {"error": "keyword 和 category 必填"}
    max_notes = body.get("max_notes", 20)
    delay = body.get("delay", 3)
    interval_hours = body.get("interval_hours", 24)
    if interval_hours < 0.5:
        return {"error": "间隔不能小于 0.5 小时"}
    now = _now()
    next_run = (datetime.now() + timedelta(hours=interval_hours)).strftime("%Y-%m-%d %H:%M:%S")
    platform = body.get("platform", "xhs")
    if platform not in PLATFORMS:
        return {"error": f"不支持的平台: {platform}"}
    with get_db() as db:
        db.execute(
            """INSERT INTO schedules (schedule_id, keyword, category, max_notes, delay, interval_hours, enabled, platform, next_run_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (sid, keyword, category, max_notes, delay, interval_hours, platform, next_run, now),
        )
        db.commit()
    return {"schedule_id": sid}


@app.get("/api/schedules")
async def list_schedules():
    with get_db() as db:
        rows = dict_rows(db.execute(
            "SELECT * FROM schedules ORDER BY created_at DESC"
        ).fetchall())
    return rows


@app.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, body: dict):
    with get_db() as db:
        row = db.execute("SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)).fetchone()
        if not row:
            return {"error": "定时任务不存在"}
        if body.get("next_run_at_now"):
            db.execute("UPDATE schedules SET enabled = 1, next_run_at = ? WHERE schedule_id = ?", (_now(), schedule_id))
            db.commit()
            return {"ok": True}
        allowed = {"keyword", "category", "max_notes", "delay", "interval_hours", "enabled"}
        fields = {k: v for k, v in body.items() if k in allowed}
        if not fields:
            return {"error": "无有效字段"}
        if "enabled" in fields and fields["enabled"]:
            interval = fields.get("interval_hours", row["interval_hours"])
            fields["next_run_at"] = (datetime.now() + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")
        elif "interval_hours" in fields:
            fields["next_run_at"] = (datetime.now() + timedelta(hours=fields["interval_hours"])).strftime("%Y-%m-%d %H:%M:%S")
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE schedules SET {sets} WHERE schedule_id = ?", [*fields.values(), schedule_id])
        db.commit()
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    with get_db() as db:
        db.execute("DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,))
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scheduler: background loop that triggers due schedules
# ---------------------------------------------------------------------------
async def _scheduler_loop():
    await asyncio.sleep(5)
    while True:
        try:
            now_str = _now()
            with get_db() as db:
                due = dict_rows(db.execute(
                    "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ?",
                    (now_str,),
                ).fetchall())
                for sched in due:
                    sid = sched["schedule_id"]
                    if sid in _active_schedules:
                        continue  # Still running from last trigger, skip
                    interval = sched["interval_hours"]
                    next_run = (datetime.now() + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")
                    db.execute(
                        "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE schedule_id = ?",
                        (now_str, next_run, sid),
                    )
                    db.commit()
                    _active_schedules.add(sid)

                    async def _run_and_cleanup(s=sched, schedule_id=sid):
                        try:
                            task_id = await _launch_scraper(
                                s["keyword"], s["category"], s["max_notes"], s["delay"],
                                s.get("platform", "xhs"),
                            )
                            # Wait for the task to finish
                            while task_id in _running:
                                await asyncio.sleep(2)
                            print(f"⏰ 定时任务 [{schedule_id}] 完成 (task={task_id})")
                        finally:
                            _active_schedules.discard(schedule_id)

                    asyncio.create_task(_run_and_cleanup())
        except Exception as e:
            print(f"⏰ 调度器异常: {e}")
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# API: Delete note
# ---------------------------------------------------------------------------
@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    with get_db() as db:
        db.execute("DELETE FROM images WHERE note_id = ?", (note_id,))
        db.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
