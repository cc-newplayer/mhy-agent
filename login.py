"""B站扫码登录（自动检测，无需手动按回车）"""
import sys, io, os, time
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from playwright.sync_api import sync_playwright
from bilibili_comments_browser import BROWSER_DATA_DIR, check_login

print("=" * 40)
print("B站扫码登录")
print("=" * 40)

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=BROWSER_DATA_DIR,
        headless=False,  # 显示浏览器
    )
    page = context.pages[0] if context.pages else context.new_page()

    # 先检查是否已登录
    page.goto("https://www.bilibili.com", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    is_login, uname = check_login(page)

    if is_login:
        print(f"\n已登录: {uname}")
        print("登录态有效，无需重新登录。")
        context.close()
    else:
        print("\n未登录，正在打开登录页面...")
        print("请在浏览器中扫码登录，登录成功后会自动检测。")
        page.goto("https://passport.bilibili.com/login")

        # 自动轮询检测登录状态
        for i in range(60):  # 最多等2分钟
            time.sleep(2)
            try:
                is_login, uname = check_login(page)
                if is_login:
                    print(f"\n✅ 登录成功: {uname}")
                    print("登录态已保存，下次无需再登录。")
                    break
            except:
                pass
        else:
            print("\n⏰ 超时未检测到登录，请重试。")

        context.close()

print("\n窗口已关闭。")
