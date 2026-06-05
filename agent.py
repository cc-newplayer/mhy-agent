"""
B站舆情分析 Agent

自然语言驱动的舆情分析。支持指令如：
  - "帮我分析BV1PnV46DEP4的舆情"
  - "看看UID 401742377 最新视频的评论情况"
  - "分析一下原神最新PV的负面评论"

原理：
  用 Claude tool_use 做调度，Agent自动决策调用哪些工具、按什么顺序执行。

用法：
  python agent.py
  python agent.py "分析BV1PnV46DEP4的舆情"
"""
import sys
import io
import os
import json
import time
import argparse
from datetime import datetime

# 安全地设置 UTF-8 输出（避免多次 wrap 导致 buffer 关闭）
if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    import anthropic
except ImportError:
    print("请安装 anthropic: pip install anthropic")
    sys.exit(1)

# ============ 配置 ============
API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", None)
MODEL = "claude-opus-4-7"
WORK_DIR = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = """你是B站舆情分析助手。你可以帮用户爬取B站视频评论并进行情感分析和负面议题分析。

工作流程：
1. 用户给出视频BV号或UP主UID，你先获取视频信息
2. 爬取评论
3. 进行情感分析
4. 如果负面比例较高（>15%），主动进行负面深度分析
5. 向用户汇报结果

注意：
- 如果用户给的是UID，先用 find_latest_video 获取最新视频BV号
- 爬取需要浏览器登录态，如果失败要提示用户先运行登录
- 分析完成后用中文总结关键发现
"""

# ============ Tools 定义 ============
TOOLS = [
    {
        "name": "find_latest_video",
        "description": "通过B站用户UID查找其最新投稿视频的BV号和标题。",
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "B站用户UID，纯数字"
                }
            },
            "required": ["uid"]
        }
    },
    {
        "name": "crawl_comments",
        "description": "爬取指定B站视频的评论（主评论+楼中楼）。需要浏览器登录态。返回评论文件路径。",
        "input_schema": {
            "type": "object",
            "properties": {
                "bvid": {
                    "type": "string",
                    "description": "视频BV号，如 BV1PnV46DEP4"
                },
                "pages": {
                    "type": "integer",
                    "description": "最多爬取页数，默认5",
                    "default": 5
                }
            },
            "required": ["bvid"]
        }
    },
    {
        "name": "analyze_sentiment",
        "description": "对评论文件进行情感分析（正面/负面/中性三分类）。返回带标签的文件路径和统计数据。",
        "input_schema": {
            "type": "object",
            "properties": {
                "comments_file": {
                    "type": "string",
                    "description": "评论JSON文件路径"
                }
            },
            "required": ["comments_file"]
        }
    },
    {
        "name": "analyze_negative",
        "description": "对负面评论进行深度分析：提取议题、聚合典型评论、生成报告。需要先完成情感分析。",
        "input_schema": {
            "type": "object",
            "properties": {
                "sentiment_file": {
                    "type": "string",
                    "description": "情感分析后的JSON文件路径"
                }
            },
            "required": ["sentiment_file"]
        }
    },
    {
        "name": "list_files",
        "description": "列出项目目录下已有的分析报告和数据文件。用于确认有哪些可用的历史分析结果。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_type": {
                    "type": "string",
                    "description": "过滤文件类型: all(全部), report(报告和图表), data(数据文件)",
                    "enum": ["all", "report", "data"]
                }
            },
            "required": ["file_type"]
        }
    },
    {
        "name": "read_file",
        "description": "读取项目目录下的文件内容（支持.json/.md/.csv）。用于查看已有分析报告或评论数据，以便回答用户追问。",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "文件名（如 negative_report_BV1xxx_20260604.md）"
                }
            },
            "required": ["filename"]
        }
    },
]


# ============ Tool 执行函数 ============

def tool_find_latest_video(uid: str) -> str:
    """通过UID查找最新视频"""
    from playwright.sync_api import sync_playwright
    from bilibili_comments_browser import (
        BROWSER_DATA_DIR, navigate_json, check_login, get_latest_video_by_uid
    )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_DATA_DIR, headless=True
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.bilibili.com", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        is_login, uname = check_login(page)
        if not is_login:
            context.close()
            return json.dumps({"error": "未登录，请先运行: python bilibili_comments_browser.py --login"}, ensure_ascii=False)

        try:
            bvid, title = get_latest_video_by_uid(page, uid)
            result = {"bvid": bvid, "title": title, "uid": uid}
        except Exception as e:
            result = {"error": str(e)}
        finally:
            context.close()

    return json.dumps(result, ensure_ascii=False)


def tool_crawl_comments(bvid: str, pages: int = 5) -> str:
    """爬取视频评论"""
    from playwright.sync_api import sync_playwright
    from bilibili_comments_browser import (
        BROWSER_DATA_DIR, check_login, get_video_info, fetch_comments
    )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_DATA_DIR, headless=True
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.bilibili.com", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        is_login, uname = check_login(page)
        if not is_login:
            context.close()
            return json.dumps({"error": "未登录"}, ensure_ascii=False)

        try:
            info = get_video_info(page, bvid)
            aid = info["aid"]
            title = info["title"]
            comments = fetch_comments(page, aid, pages)
        except Exception as e:
            context.close()
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        context.close()

    if not comments:
        return json.dumps({"error": "未获取到评论"}, ensure_ascii=False)

    # 保存文件
    filename = os.path.join(WORK_DIR, f"comments_{bvid}_full.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)

    return json.dumps({
        "file": filename,
        "video_title": title,
        "comment_count": len(comments),
        "bvid": bvid
    }, ensure_ascii=False)

# APPEND_MARKER_1


def tool_analyze_sentiment(comments_file: str) -> str:
    """情感分析"""
    from sentiment_analysis import analyze_batch, draw_pie_chart, BATCH_SIZE, BATCH_INTERVAL

    with open(comments_file, "r", encoding="utf-8") as f:
        comments = json.load(f)

    client_kwargs = {"api_key": API_KEY}
    if BASE_URL:
        client_kwargs["base_url"] = BASE_URL
    client = anthropic.Anthropic(**client_kwargs)

    total_batches = (len(comments) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"  情感分析中... ({len(comments)}条评论，{total_batches}批)")

    for batch_idx in range(total_batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(comments))
        batch = comments[start:end]

        try:
            results = analyze_batch(client, batch)
            for item in results:
                idx = item["index"] - 1
                actual_idx = start + idx
                if 0 <= actual_idx < len(comments):
                    sentiment = item["sentiment"]
                    if sentiment not in ("正面", "负面", "中性"):
                        sentiment = "中性"
                    comments[actual_idx]["sentiment"] = sentiment
        except Exception as e:
            print(f"    批次{batch_idx+1}失败: {e}")
            for i in range(start, end):
                if "sentiment" not in comments[i]:
                    comments[i]["sentiment"] = "中性"

        if batch_idx < total_batches - 1:
            time.sleep(BATCH_INTERVAL)

    for c in comments:
        if "sentiment" not in c:
            c["sentiment"] = "中性"

    # 统计
    counts = {"正面": 0, "负面": 0, "中性": 0}
    for c in comments:
        counts[c["sentiment"]] = counts.get(c["sentiment"], 0) + 1

    # 保存
    output_file = comments_file.replace(".json", "_sentiment.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)

    # 画图
    chart_file = os.path.join(WORK_DIR, "sentiment_chart.png")
    draw_pie_chart(counts, chart_file)

    total = len(comments)
    return json.dumps({
        "file": output_file,
        "total": total,
        "positive": counts["正面"],
        "negative": counts["负面"],
        "neutral": counts["中性"],
        "negative_ratio": f"{counts['负面']/total*100:.1f}%",
        "chart": chart_file
    }, ensure_ascii=False)

# APPEND_MARKER_2


def tool_analyze_negative(sentiment_file: str) -> str:
    """负面深度分析"""
    from negative_analysis import (
        extract_tfidf_keywords, identify_topics, classify_comments_to_topics,
        compute_comment_score, draw_topic_chart, generate_suggestions,
        generate_report, clean_text
    )
    from collections import defaultdict

    with open(sentiment_file, "r", encoding="utf-8") as f:
        comments = json.load(f)

    negative = [c for c in comments if c.get("sentiment") == "负面"]
    if len(negative) < 3:
        return json.dumps({"message": "负面评论太少（<3条），无需深度分析"}, ensure_ascii=False)

    client_kwargs = {"api_key": API_KEY}
    if BASE_URL:
        client_kwargs["base_url"] = BASE_URL
    client = anthropic.Anthropic(**client_kwargs)

    print(f"  负面深度分析中... ({len(negative)}条负面评论)")

    # TF-IDF
    candidates = extract_tfidf_keywords(comments, negative, top_n=25)
    time.sleep(1)

    # 归纳议题
    topics = identify_topics(client, candidates, negative)
    time.sleep(2)

    # 分类
    negative = classify_comments_to_topics(client, topics, negative)

    # 聚合
    topic_comments = defaultdict(list)
    for c in negative:
        for t in c.get("topics", ["其他"]):
            topic_comments[t].append(c)

    topic_stats = []
    for t in topics:
        label = t["label"]
        in_topic = topic_comments.get(label, [])
        in_topic.sort(key=compute_comment_score, reverse=True)
        topic_stats.append({
            "label": label,
            "description": t["description"],
            "keywords": t.get("keywords", []),
            "count": len(in_topic),
            "typical": in_topic[:3],
        })
    topic_stats.sort(key=lambda x: x["count"], reverse=True)

    # 从文件名提取BV号用于命名
    import re
    bv_match = re.search(r"(BV[\w]+)", sentiment_file)
    bv_tag = bv_match.group(1) if bv_match else "unknown"
    date_tag = datetime.now().strftime("%Y%m%d")

    # 图表
    chart_path = os.path.join(WORK_DIR, f"negative_chart_{bv_tag}_{date_tag}.png")
    draw_topic_chart(topic_stats, chart_path)

    # 建议
    suggestions = generate_suggestions(client, topic_stats)

    # 报告
    report = generate_report(len(comments), negative, topic_stats, chart_path, suggestions)
    report_path = os.path.join(WORK_DIR, f"negative_report_{bv_tag}_{date_tag}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # 返回摘要
    topics_summary = [{"label": t["label"], "count": t["count"], "desc": t["description"]} for t in topic_stats]
    return json.dumps({
        "report_file": report_path,
        "chart_file": chart_path,
        "total_negative": len(negative),
        "topics": topics_summary,
        "suggestions": suggestions
    }, ensure_ascii=False)


def tool_list_files(file_type: str = "all") -> str:
    """列出项目目录下的分析报告和数据文件"""
    import glob
    patterns = ["*.json", "*.md", "*.png", "*.csv"]
    files = []
    for pat in patterns:
        for f in glob.glob(os.path.join(WORK_DIR, pat)):
            name = os.path.basename(f)
            size = os.path.getsize(f)
            files.append({"name": name, "size_kb": round(size / 1024, 1)})

    # 按类型过滤
    if file_type == "report":
        files = [f for f in files if f["name"].endswith(".md") or f["name"].endswith(".png")]
    elif file_type == "data":
        files = [f for f in files if f["name"].endswith(".json")]

    files.sort(key=lambda x: x["name"])
    return json.dumps({"files": files, "directory": WORK_DIR}, ensure_ascii=False)


def tool_read_file(filename: str) -> str:
    """读取指定文件内容"""
    filepath = os.path.join(WORK_DIR, filename)
    if not os.path.exists(filepath):
        return json.dumps({"error": f"文件不存在: {filename}"}, ensure_ascii=False)

    # 安全检查：不允许路径穿越
    if ".." in filename or os.path.isabs(filename):
        return json.dumps({"error": "不允许的文件路径"}, ensure_ascii=False)

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".json":
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        # JSON文件可能很大，截断返回
        if isinstance(data, list) and len(data) > 50:
            summary = f"共{len(data)}条记录，以下为前50条"
            return json.dumps({"summary": summary, "data": data[:50]}, ensure_ascii=False)
        return json.dumps({"data": data}, ensure_ascii=False)
    elif ext in (".md", ".csv", ".txt"):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        # 文本文件截断到8000字符避免context过长
        if len(content) > 8000:
            content = content[:8000] + f"\n\n... (截断，原文共{len(content)}字符)"
        return json.dumps({"content": content}, ensure_ascii=False)
    else:
        return json.dumps({"error": f"不支持的文件类型: {ext}"}, ensure_ascii=False)

# ============ Tool 调度 ============

TOOL_HANDLERS = {
    "find_latest_video": lambda args: tool_find_latest_video(args["uid"]),
    "crawl_comments": lambda args: tool_crawl_comments(args["bvid"], args.get("pages", 5)),
    "analyze_sentiment": lambda args: tool_analyze_sentiment(args["comments_file"]),
    "analyze_negative": lambda args: tool_analyze_negative(args["sentiment_file"]),
    "list_files": lambda args: tool_list_files(args.get("file_type", "all")),
    "read_file": lambda args: tool_read_file(args["filename"]),
}


# ============ Agent Loop ============

def run_agent(user_input: str, messages: list = None) -> str:
    """
    核心 Agent 循环：
    1. 把用户输入发给 Claude（带 tools）
    2. 如果 Claude 要调用 tool → 执行 → 把结果喂回去
    3. 重复直到 Claude 给出最终文字回答
    """
    client_kwargs = {"api_key": API_KEY}
    if BASE_URL:
        client_kwargs["base_url"] = BASE_URL
    client = anthropic.Anthropic(**client_kwargs)

    if messages is None:
        messages = []
    messages.append({"role": "user", "content": user_input})

    while True:
        print("  [Agent] 思考中...")
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # 收集assistant回复
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # 检查是否需要调用工具
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    tool_id = block.id
                    print(f"  [Agent] 调用工具: {tool_name}({json.dumps(tool_input, ensure_ascii=False)})")

                    # 执行工具
                    handler = TOOL_HANDLERS.get(tool_name)
                    if handler:
                        try:
                            result = handler(tool_input)
                        except Exception as e:
                            result = json.dumps({"error": str(e)}, ensure_ascii=False)
                    else:
                        result = json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)

                    print(f"  [Agent] 工具返回: {result[:200]}...")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                    })

            # 防御：中转站可能返回 tool_use 但 content 为空
            if not tool_results:
                return "（工具调用异常，请重试）", messages

            # 把工具结果喂回去
            messages.append({"role": "user", "content": tool_results})
        else:
            # 没有工具调用，提取最终文本回答
            final_text = ""
            for block in assistant_content:
                if hasattr(block, "text"):
                    final_text += block.text
            return final_text, messages


# ============ 主入口 ============

def main():
    parser = argparse.ArgumentParser(description="B站舆情分析 Agent")
    parser.add_argument("query", nargs="?", help="分析指令（不填则进入对话模式）")
    args = parser.parse_args()

    if not API_KEY:
        print("错误: 未找到 API Key")
        print("请设置: set ANTHROPIC_AUTH_TOKEN=你的key")
        sys.exit(1)

    print("=" * 50)
    print("B站舆情分析 Agent")
    print("输入分析指令，如: 分析BV1PnV46DEP4的舆情")
    print("输入 quit 退出")
    print("=" * 50)

    messages = []  # 保持对话历史，支持多轮

    if args.query:
        # 单次执行模式
        answer, messages = run_agent(args.query, messages)
        print(f"\n{answer}")
    else:
        # 对话模式
        while True:
            try:
                user_input = input("\n你: ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("再见！")
                break

            answer, messages = run_agent(user_input, messages)
            print(f"\nAgent: {answer}")


if __name__ == "__main__":
    main()
