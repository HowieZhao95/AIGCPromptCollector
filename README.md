# AIGC Prompt Collector

[English](#english) | [中文](#中文)

---

<a id="english"></a>

## English

Multi-platform AI image generation prompt collector and manager. Automatically scrapes notes from social platforms (Xiaohongshu & X/Twitter), extracts structured prompts via LLM, stores everything in a local SQLite database, and provides a dark-themed Web UI for browsing and management.

### Supported Platforms

| Platform | Scraper | Login Script |
|----------|---------|--------------|
| **Xiaohongshu (小红书)** | `download_xhs_prompt.py` | `save_login.py` |
| **X (Twitter)** | `download_x_prompt.py` | `save_login_x.py` |

### Features

- **Multi-Platform** — Supports Xiaohongshu and X (Twitter) with platform-specific scrapers sharing a unified data format
- **Automated Scraping** — Playwright-based browser automation with infinite scroll, carousel image extraction, and cookie-based authentication
- **LLM Quality Gate** — Each note is processed by an LLM to extract structured prompts (prompt, model, parameters, style tags). Notes without valid prompts are discarded automatically
- **Model Normalization** — Extracted model names are normalized to a whitelist: `Midjourney`, `FLUX`, `Seedream`, `NanoBanana`, with extensive alias mapping
- **Scheduled Tasks** — Built-in scheduler with configurable intervals. Automatically re-runs scraping jobs and skips already-collected content (deduplication)
- **Web UI** — Dark-themed single-page app with Dashboard, Task Management, Prompt Browser, Scheduled Tasks, and Settings
- **Task Persistence** — All tasks and logs are persisted to SQLite, surviving server restarts
- **Image Proxy** — Built-in proxy endpoint bypasses hotlink protection for seamless image display
- **API Key Management** — Configure OpenRouter API key directly from the Web UI

### Quick Start

#### 1. Install Dependencies

```bash
uv sync
uv run playwright install chromium
```

#### 2. Save Login State (one-time per platform)

```bash
# Xiaohongshu
uv run save_login.py

# X (Twitter)
uv run save_login_x.py
```

A browser window opens — log in manually, then press Enter in the terminal. Cookies are saved for future use.

> Re-run the relevant script when cookies expire.

#### 3. Start the Web UI

```bash
uv run app.py
```

Open **http://localhost:8000** in your browser.

#### 4. Configure API Key

Navigate to **Settings** in the sidebar and enter your OpenRouter API key. Get one at [openrouter.ai/keys](https://openrouter.ai/keys).

### Web UI Pages

| Page | Description |
|------|-------------|
| **Dashboard** | Overview stats: total notes, images, category/model distribution charts, recent notes |
| **Tasks** | Create scraping tasks, real-time SSE log terminal, task history with stop/delete/view-log actions |
| **Prompt Browser** | Card grid with thumbnails, filter by category/model, keyword search, pagination |
| **Scheduled Tasks** | Create recurring scraping jobs with configurable intervals, enable/disable/run-now/delete |
| **Settings** | Configure OpenRouter API key, view login cookie status |

### CLI Usage

```bash
# Xiaohongshu
uv run download_xhs_prompt.py --keyword "architecture prompts" --category "architecture"
uv run download_xhs_prompt.py --keyword "interior AI" --category "interior" --max-notes 50 --delay 5

# X (Twitter)
uv run download_x_prompt.py --keyword "midjourney architecture" --category "architecture"
uv run download_x_prompt.py --keyword "AI art prompt" --category "art" --max-notes 50
```

| Argument | Description | Default |
|----------|-------------|---------|
| `--keyword` | Search keyword (required) | — |
| `--category` | Category label (required) | — |
| `--max-notes` | Max notes to collect | 20 |
| `--delay` | Delay between notes (seconds) | 3 |
| `--headless` | Run in headless mode | off |
| `--db` | Database path | `xhs_notes.db` |

### Data Schema

#### Structured Prompt Format

```json
{
  "prompt_en": "A futuristic building with glass facade...",
  "prompt_cn": "...",
  "model": "Midjourney",
  "parameters": "--ar 16:9 --v 6",
  "style_tags": ["futuristic", "architecture"]
}
```

#### Allowed Models

`Midjourney` | `FLUX` | `Seedream` | `NanoBanana`

All variants and aliases (MJ, Flux.1, Banana Pro, Seed Dream, etc.) are auto-normalized.

#### Database Tables

| Table | Purpose |
|-------|---------|
| `notes` | Note metadata + structured prompt JSON |
| `images` | Image URLs per note (supports carousel) |
| `tasks` | Scraping task records with full logs |
| `schedules` | Scheduled recurring task configurations |

### Project Structure

```
├── app.py                    # FastAPI backend + API endpoints
├── templates/index.html      # Single-page frontend (Tailwind CSS dark theme)
├── download_xhs_prompt.py    # Xiaohongshu scraper (Playwright + LLM extraction)
├── download_x_prompt.py      # X (Twitter) scraper (Playwright + LLM extraction)
├── save_login.py             # Xiaohongshu login cookie saver
├── save_login_x.py           # X (Twitter) login cookie saver
├── pyproject.toml            # Dependencies
├── .env                      # Environment variables (API key)
├── *_auth.json               # Login cookies (auto-generated, gitignored)
└── xhs_notes.db              # SQLite database (auto-generated, gitignored)
```

### Tech Stack

- **Backend**: FastAPI + Uvicorn + SQLite (WAL mode)
- **Frontend**: Single HTML + Tailwind CSS CDN + Vanilla JS
- **Scraping**: Playwright (browser automation)
- **LLM**: OpenRouter API (Claude Sonnet)
- **Real-time**: Server-Sent Events (SSE)
- **Scheduling**: Built-in asyncio scheduler (configurable intervals)

---

<a id="中文"></a>

## 中文

多平台 AI 图像生成提示词采集与管理工具。自动从社交平台（小红书 & X/Twitter）采集笔记，通过 LLM 提取结构化提示词，存入本地 SQLite 数据库，并提供暗黑主题 Web UI 进行浏览和管理。

### 支持平台

| 平台 | 采集脚本 | 登录脚本 |
|------|----------|----------|
| **小红书** | `download_xhs_prompt.py` | `save_login.py` |
| **X (Twitter)** | `download_x_prompt.py` | `save_login_x.py` |

### 功能特性

- **多平台支持** — 支持小红书和 X (Twitter)，各平台独立采集脚本，统一数据格式
- **自动采集** — 基于 Playwright 的浏览器自动化，支持无限滚动、轮播图提取、Cookie 认证
- **LLM 质量过滤** — 每条笔记经 LLM 提取结构化提示词（prompt、model、parameters、style_tags），无有效提示词的笔记自动丢弃
- **模型名归一化** — 提取的模型名自动映射到白名单：`Midjourney`、`FLUX`、`Seedream`、`NanoBanana`，覆盖各种别名写法
- **定时任务** — 内置调度器，可配置执行间隔，自动跳过已采集内容（去重）
- **Web UI** — 暗黑主题单页应用，包含 Dashboard、任务管理、提示词浏览、定时任务、设置
- **任务持久化** — 所有任务和日志持久化到 SQLite，服务重启后可继续查看
- **图片代理** — 内置代理端点绕过防盗链，前端直接展示原始图片
- **API Key 管理** — 可在 Web UI 中直接配置 OpenRouter API Key

### 快速开始

#### 1. 安装依赖

```bash
uv sync
uv run playwright install chromium
```

#### 2. 保存登录状态（每个平台仅需一次）

```bash
# 小红书
uv run save_login.py

# X (Twitter)
uv run save_login_x.py
```

运行后会打开浏览器，手动登录对应平台，登录成功后回到终端按回车。Cookie 自动保存，后续采集自动加载。

> Cookie 过期后需重新运行对应登录脚本。

#### 3. 启动 Web UI

```bash
uv run app.py
```

打开浏览器访问 **http://localhost:8000**

#### 4. 配置 API Key

在侧栏点击「设置」，输入 OpenRouter API Key。获取地址：[openrouter.ai/keys](https://openrouter.ai/keys)

### Web UI 页面说明

| 页面 | 功能 |
|------|------|
| **Dashboard** | 总览统计：笔记数、图片数、分类/模型分布图表、最近采集列表 |
| **采集任务** | 新建采集任务、实时 SSE 日志终端、任务历史管理（停止/删除/查看日志） |
| **提示词浏览** | 卡片网格展示缩略图，按分类、模型筛选，关键词搜索，分页浏览 |
| **定时任务** | 创建定时采集任务，可配置执行间隔，支持启用/禁用/立即执行/删除 |
| **设置** | 配置 OpenRouter API Key，查看登录 Cookie 状态 |

### 命令行使用

```bash
# 小红书
uv run download_xhs_prompt.py --keyword "建筑提示词" --category "建筑"
uv run download_xhs_prompt.py --keyword "室内设计AI" --category "室内" --max-notes 50 --delay 5

# X (Twitter)
uv run download_x_prompt.py --keyword "midjourney architecture" --category "建筑"
uv run download_x_prompt.py --keyword "AI art prompt" --category "综合" --max-notes 50
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--keyword` | 搜索关键词（必填） | — |
| `--category` | 分类标签（必填） | — |
| `--max-notes` | 最大采集笔记数 | 20 |
| `--delay` | 笔记间延迟秒数 | 3 |
| `--headless` | 无头模式运行 | 否 |
| `--db` | 数据库路径 | `xhs_notes.db` |

### 数据结构

#### 提示词格式（structured_prompt）

```json
{
  "prompt_en": "A futuristic building with glass facade...",
  "prompt_cn": "未来主义玻璃幕墙建筑...",
  "model": "Midjourney",
  "parameters": "--ar 16:9 --v 6",
  "style_tags": ["futuristic", "architecture"]
}
```

#### 支持的模型

`Midjourney` | `FLUX` | `Seedream` | `NanoBanana`

所有变体和别名（MJ、Flux.1、Banana Pro、Seed Dream 等）均自动归一化。

#### 数据库表

| 表 | 用途 |
|----|------|
| `notes` | 笔记元数据 + 结构化提示词 JSON |
| `images` | 笔记关联的图片 URL（支持轮播多图） |
| `tasks` | 采集任务记录及完整日志 |
| `schedules` | 定时任务配置 |

### 项目结构

```
├── app.py                    # FastAPI 后端 + API 端点
├── templates/index.html      # 单页前端（Tailwind CSS 暗黑主题）
├── download_xhs_prompt.py    # 小红书采集脚本（Playwright + LLM 提取）
├── download_x_prompt.py      # X (Twitter) 采集脚本（Playwright + LLM 提取）
├── save_login.py             # 小红书登录 Cookie 保存
├── save_login_x.py           # X (Twitter) 登录 Cookie 保存
├── pyproject.toml            # 项目依赖
├── .env                      # 环境变量（API Key）
├── *_auth.json               # 登录 Cookie（自动生成，已 gitignore）
└── xhs_notes.db              # SQLite 数据库（自动生成，已 gitignore）
```

### 技术栈

- **后端**: FastAPI + Uvicorn + SQLite（WAL 模式）
- **前端**: 单 HTML 文件 + Tailwind CSS CDN + 原生 JS
- **采集**: Playwright（浏览器自动化）
- **LLM**: OpenRouter API（Claude Sonnet）
- **实时通信**: Server-Sent Events（SSE）
- **定时调度**: 内置 asyncio 调度器（可配置间隔）
