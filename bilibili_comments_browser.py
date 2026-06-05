"""
B站视频评论爬取脚本（Playwright版）

原理：
  B站评论API对未登录/非浏览器请求有风控限制，requests无法获取完整评论。
  本脚本使用Playwright浏览器自动化，通过page.goto()导航到API地址获取JSON数据。
  使用持久化浏览器上下文保存登录状态，首次运行需扫码登录，之后自动复用。

接口：
  - 视频信息: https://api.bilibili.com/x/web-interface/view?bvid=xxx
  - 评论(新版游标翻页): https://api.bilibili.com/x/v2/reply/main
    参数: type=1, oid=aid, mode=3(热度)/2(时间), pagination_str={"offset":"..."}

依赖安装：
  pip install playwright
  playwright install chromium

用法：
  python bilibili_comments_browser.py BV1PnV46DEP4
  python bilibili_comments_browser.py --uid 12345678
  python bilibili_comments_browser.py BV1PnV46DEP4 --pages 10
  python bilibili_comments_browser.py BV1PnV46DEP4 --login  (首次登录用)
"""
import sys
import io
import json
import argparse
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("请先安装 playwright:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

# 浏览器数据目录（保存登录状态）
BROWSER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_data")
MAX_PAGES = 5
CST = timezone(timedelta(hours=8))


def navigate_json(page, url):
    """导航到API地址并解析返回的JSON"""
    page.goto(url, wait_until="domcontentloaded")
    text = page.evaluate("() => document.body.innerText")
    return json.loads(text)


def check_login(page):
    """检查是否已登录"""
    data = navigate_json(page, "https://api.bilibili.com/x/web-interface/nav")
    return data["data"]["isLogin"], data["data"].get("uname", "")


def get_video_info(page, bvid):
    """通过BV号获取视频信息"""
    data = navigate_json(page, f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if data["code"] != 0:
        raise ValueError(f"获取视频信息失败: {data.get('message')}")
    return data["data"]


def get_latest_video_by_uid(page, uid):
    """通过UID获取用户最新视频的BV号"""
    url = f"https://api.bilibili.com/x/space/wbi/arc/search?mid={uid}&ps=1&pn=1&order=pubdate"
    data = navigate_json(page, url)
    if data["code"] != 0:
        raise ValueError(f"获取用户视频列表失败: {data.get('message')}")
    vlist = data["data"]["list"]["vlist"]
    if not vlist:
        raise ValueError("该用户没有投稿视频")
    return vlist[0]["bvid"], vlist[0]["title"]


def fetch_sub_replies(page, aid, root_rpid, max_pages=3):
    """
    获取某条主评论下的子回复（楼中楼）
    /x/v2/reply/reply 接口，按页码翻页
    """
    sub_comments = []
    for pn in range(1, max_pages + 1):
        url = (
            f"https://api.bilibili.com/x/v2/reply/reply"
            f"?type=1&oid={aid}&root={root_rpid}&pn={pn}&ps=20"
        )
        data = navigate_json(page, url)

        if data["code"] != 0:
            break

        replies = data["data"].get("replies") or []
        if not replies:
            break

        for reply in replies:
            dt = datetime.fromtimestamp(reply["ctime"], tz=CST)
            sub_comments.append({
                "user": reply["member"]["uname"],
                "content": reply["content"]["message"].replace("\n", " "),
                "likes": reply["like"],
                "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "is_reply": True,
                "parent_rpid": root_rpid,
            })

        # 检查是否还有下一页
        page_info = data["data"].get("page", {})
        total_count = page_info.get("count", 0)
        if pn * 20 >= total_count:
            break

        page.wait_for_timeout(500)  # 子回复间隔短一些

    return sub_comments


def fetch_comments(page, aid, max_pages=5, fetch_replies=True):
    """
    分页爬取评论
    使用新版接口 /x/v2/reply/main + 游标翻页
    mode=3 热度排序, mode=2 时间排序

    fetch_replies=True 时，会对每条有子回复的主评论额外请求楼中楼
    """
    all_comments = []
    next_offset = ""

    for page_num in range(1, max_pages + 1):
        pagination = json.dumps({"offset": next_offset})
        url = (
            f"https://api.bilibili.com/x/v2/reply/main"
            f"?type=1&oid={aid}&mode=3"
            f"&pagination_str={quote(pagination)}"
        )
        data = navigate_json(page, url)

        if data["code"] != 0:
            print(f"  第{page_num}页失败: code={data['code']} {data.get('message','')}")
            break

        replies = data["data"].get("replies") or []
        if not replies:
            print(f"  第{page_num}页: 无更多评论")
            break

        cursor = data["data"].get("cursor", {})
        total = cursor.get("all_count", "?")
        sub_count = 0

        for reply in replies:
            dt = datetime.fromtimestamp(reply["ctime"], tz=CST)
            rpid = reply["rpid"]
            rcount = reply.get("rcount", 0)  # 子回复数量

            all_comments.append({
                "user": reply["member"]["uname"],
                "content": reply["content"]["message"].replace("\n", " "),
                "likes": reply["like"],
                "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "is_reply": False,
                "rpid": rpid,
                "reply_count": rcount,
            })

            # 抓取子回复
            if fetch_replies and rcount > 0:
                subs = fetch_sub_replies(page, aid, rpid)
                all_comments.extend(subs)
                sub_count += len(subs)

        print(f"  第{page_num}页: {len(replies)} 条主评论 + {sub_count} 条子回复 (总评论数: {total})")

        # 翻页游标
        pagination_reply = cursor.get("pagination_reply", {})
        next_offset = pagination_reply.get("next_offset", "")
        if cursor.get("is_end", True):
            break

        # 请求间隔1秒，避免被封
        page.wait_for_timeout(1000)

    return all_comments


def main():
    parser = argparse.ArgumentParser(description="B站视频评论爬取 (Playwright版)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("bvid", nargs="?", help="视频BV号 (例: BV1PnV46DEP4)")
    group.add_argument("--uid", help="用户UID，自动获取其最新视频")
    parser.add_argument("--pages", type=int, default=MAX_PAGES, help=f"最多爬取页数 (默认{MAX_PAGES})")
    parser.add_argument("--login", action="store_true", help="弹出浏览器窗口进行登录")
    parser.add_argument("--output", help="输出文件名 (默认自动生成，支持.json/.csv)")
    args = parser.parse_args()

    # login模式显示浏览器，否则无头
    headless = not args.login

    print("=" * 50)
    print("B站视频评论爬取工具")
    print("=" * 50)

    with sync_playwright() as p:
        # 使用持久化上下文，复用登录态
        context = p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_DATA_DIR,
            headless=headless,
        )
        pg = context.pages[0] if context.pages else context.new_page()

        # 访问B站首页建立会话
        print("\n初始化浏览器...")
        pg.goto("https://www.bilibili.com", wait_until="domcontentloaded")
        pg.wait_for_timeout(2000)

        # 检查登录状态
        is_login, uname = check_login(pg)
        if is_login:
            print(f"  已登录: {uname}")
        else:
            print("  未登录（评论数量可能受限）")
            if not args.login:
                print("  提示: 使用 --login 参数可弹出浏览器进行扫码登录")

        # --login 模式: 等待用户登录
        if args.login and not is_login:
            print("\n请在浏览器中扫码登录B站...")
            print("登录完成后按回车继续...")
            pg.goto("https://passport.bilibili.com/login")
            input()
            is_login, uname = check_login(pg)
            if is_login:
                print(f"  登录成功: {uname}")
            else:
                print("  仍未检测到登录，继续尝试...")

        # 如果只是登录，不爬取
        if args.login and not args.bvid and not args.uid:
            print("\n登录状态已保存，下次运行无需再登录。")
            context.close()
            return

        # 获取BV号
        if args.uid:
            print(f"\n查找用户 {args.uid} 的最新视频...")
            bvid, title = get_latest_video_by_uid(pg, args.uid)
            print(f"  最新视频: {title} ({bvid})")
        else:
            bvid = args.bvid

        # 获取视频信息
        print(f"\n获取视频信息: {bvid}")
        info = get_video_info(pg, bvid)
        aid = info["aid"]
        print(f"  标题: {info['title']}")
        print(f"  AID: {aid}")
        print(f"  评论数: {info['stat']['reply']}")

        # 爬取评论
        print(f"\n开始爬取评论 (最多{args.pages}页, 每页20条)...")
        comments = fetch_comments(pg, aid, args.pages)

        context.close()

    if not comments:
        print("\n未获取到评论")
        return

    # 确定输出文件名和格式
    if args.output:
        filename = args.output
    else:
        filename = f"comments_{bvid}.json"

    # 保存
    if filename.endswith(".csv"):
        import csv
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["user", "content", "likes", "time"])
            writer.writeheader()
            writer.writerows(comments)
    else:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False, indent=2)

    print(f"\n已保存 {len(comments)} 条评论到 {filename}")

    # 预览前10条
    print("\n--- 前10条评论 ---")
    for i, c in enumerate(comments[:10], 1):
        print(f"{i}. [{c['time']}] {c['user']} (👍{c['likes']})")
        print(f"   {c['content'][:70]}")


if __name__ == "__main__":
    main()
