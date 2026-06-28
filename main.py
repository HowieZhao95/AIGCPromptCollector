from browser_use import Agent, BrowserProfile, Browser
from browser_use.llm.openrouter.chat import ChatOpenRouter
from dotenv import load_dotenv
import asyncio
import os

load_dotenv()

llm = ChatOpenRouter(
    model="anthropic/claude-sonnet-4-5",   # 换成 Claude，JSON 输出更稳定
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

CHROME_USER_DATA = os.path.expanduser("~/Library/Application Support/Google/Chrome")
SAVE_DIR = os.path.expanduser("~/Desktop/建筑提示词")
os.makedirs(SAVE_DIR, exist_ok=True)

async def main():
    browser = Browser(
        browser_profile=BrowserProfile(
            headless=False,
            channel="chrome",
            user_data_dir=CHROME_USER_DATA,
            profile_directory="Default",
        )
    )

    agent = Agent(
        task=f"""
        目标：从小红书下载"建筑提示词"相关笔记的图片，保存到 {SAVE_DIR}

        步骤：
        1. 打开 https://www.xiaohongshu.com/search_result?keyword=建筑提示词
        2. 等待页面加载完成
        3. 对前10条笔记，逐条执行以下操作：
           a. 点击笔记进入详情页
           b. 执行 JavaScript 提取页面中所有 <img> 标签的 src，过滤出包含 "xhscdn" 或 "sns-img" 的图片URL
           c. 用 Python 的 httpx 或 requests 下载这些图片，保存到 {SAVE_DIR}，文件名用 笔记序号_图片序号.jpg
           d. 返回搜索结果页，点击下一条
        4. 完成后汇报：共处理了几条笔记，下载了多少张图片，保存路径是哪里

        注意：
        - 每条笔记只下载封面图（第一张）即可，避免太慢
        - 如果某条笔记打开失败，跳过继续下一条
        - 下载时带上 Referer: https://www.xiaohongshu.com 请求头
        """,
        llm=llm,
        browser=browser,
    )
    result = await agent.run()
    print("\n✅ 最终结果：", result)

asyncio.run(main())
