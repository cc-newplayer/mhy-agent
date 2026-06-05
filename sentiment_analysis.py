"""
B站评论情感分析脚本

原理：
  读取爬取的评论JSON，调用Claude API对每条评论进行情感分类（正面/负面/中性），
  将结果保存为新JSON，并用matplotlib绘制情感分布饼图。

依赖安装：
  pip install anthropic matplotlib

用法：
  python sentiment_analysis.py comments_BV1PnV46DEP4.json

注意：需要设置环境变量 ANTHROPIC_API_KEY，或在下方代码中填写。
"""
import sys
import io
import json
import time
import os
import argparse
import concurrent.futures
import threading

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    import anthropic
except ImportError:
    print("请先安装 anthropic: pip install anthropic")
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")  # 无GUI环境
    import matplotlib.pyplot as plt
except ImportError:
    print("请先安装 matplotlib: pip install matplotlib")
    sys.exit(1)

# ============================================================
# API配置：优先读取环境变量，支持自定义base_url（兼容中转站）
# 设置方式（任选其一）：
#   set ANTHROPIC_API_KEY=sk-ant-xxxxx
#   set ANTHROPIC_AUTH_TOKEN=sk-xxxxx
#   或直接在下方填写
# ============================================================
API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", None)  # 留空则使用官方地址

# 限流设置
BATCH_SIZE = 30       # 每批处理条数（30条/请求，平衡token与调用次数）
MAX_WORKERS = 4       # 并发请求数
BATCH_INTERVAL = 0.5  # 并发组之间休息秒数（避免触发限流）

# 进度锁
_progress_lock = threading.Lock()
_completed_count = 0


def analyze_batch(client, comments_batch, batch_id=0):
    """
    调用Claude API对一批评论进行情感分类
    一次请求分析多条，减少API调用次数
    """
    # 构建评论列表文本
    comments_text = ""
    for i, comment in enumerate(comments_batch):
        comments_text += f"{i+1}. {comment['content']}\n"

    prompt = f"""请对以下B站视频评论逐条进行情感分类。
每条评论只需判断为：正面、负面、中性 三选一。

判断标准：
- 正面：表达喜爱、赞美、期待、支持、开心等积极情绪
- 负面：表达不满、批评、失望、愤怒等消极情绪
- 中性：客观描述、提问、无明显情感倾向

评论列表：
{comments_text}

请严格按以下JSON数组格式输出，不要输出其他内容：
[{{"index": 1, "sentiment": "正面"}}, {{"index": 2, "sentiment": "负面"}}, ...]
"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-opus-4-7",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )

            # 解析返回结果
            result_text = response.content[0].text.strip()

            # 尝试提取JSON
            start = result_text.find("[")
            end = result_text.rfind("]") + 1
            if start == -1 or end == 0:
                raise ValueError(f"API返回格式异常: {result_text[:200]}")

            results = json.loads(result_text[start:end])
            return results

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # 指数退避: 1s, 2s
                continue
            raise


def draw_pie_chart(sentiment_counts, output_path):
    """绘制情感分布饼图"""
    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    labels = []
    sizes = []
    colors_map = {"正面": "#4CAF50", "负面": "#F44336", "中性": "#FFC107"}
    colors = []

    for label in ["正面", "中性", "负面"]:
        count = sentiment_counts.get(label, 0)
        if count > 0:
            labels.append(f"{label} ({count})")
            sizes.append(count)
            colors.append(colors_map[label])

    fig, ax = plt.subplots(figsize=(8, 6))
    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        colors=colors,
        autopct="%1.1f%%",
        startangle=90,
        textprops={"fontsize": 12},
    )
    for autotext in autotexts:
        autotext.set_fontsize(11)
        autotext.set_fontweight("bold")

    ax.set_title("B站评论情感分布", fontsize=14, fontweight="bold", pad=20)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  饼图已保存: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="B站评论情感分析 (Claude API)")
    parser.add_argument("input_file", help="输入的评论JSON文件")
    parser.add_argument("--output", help="输出JSON文件名 (默认: 原文件名_sentiment.json)")
    parser.add_argument("--chart", help="饼图文件名 (默认: sentiment_chart.png)")
    args = parser.parse_args()

    if not API_KEY:
        print("错误: 未找到 API Key")
        print("请设置环境变量: set ANTHROPIC_API_KEY=sk-ant-xxxxx")
        print("  或: set ANTHROPIC_AUTH_TOKEN=sk-xxxxx")
        print("  或在脚本中直接填写")
        sys.exit(1)

    # 读取评论
    print(f"读取评论文件: {args.input_file}")
    with open(args.input_file, "r", encoding="utf-8") as f:
        comments = json.load(f)
    print(f"  共 {len(comments)} 条评论")

    # 初始化Claude客户端
    client_kwargs = {"api_key": API_KEY}
    if BASE_URL:
        client_kwargs["base_url"] = BASE_URL
    client = anthropic.Anthropic(**client_kwargs)

    # 分批处理（并发）
    total_batches = (len(comments) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\n开始情感分析 (每{BATCH_SIZE}条一批, {MAX_WORKERS}路并发, 共{total_batches}批)...")

    # 准备所有批次
    batches = []
    for batch_idx in range(total_batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(comments))
        batches.append((batch_idx, start, end, comments[start:end]))

    failed_batches = []
    start_time = time.time()

    def process_batch(batch_info):
        """并发处理单个批次"""
        global _completed_count
        batch_idx, start, end, batch = batch_info
        try:
            results = analyze_batch(client, batch, batch_idx)
            # 将情感标签写回对应评论
            for item in results:
                idx = item["index"] - 1
                actual_idx = start + idx
                if 0 <= actual_idx < len(comments):
                    sentiment = item["sentiment"]
                    if sentiment not in ("正面", "负面", "中性"):
                        sentiment = "中性"
                    comments[actual_idx]["sentiment"] = sentiment

            with _progress_lock:
                _completed_count += 1
                elapsed = time.time() - start_time
                speed = _completed_count / elapsed * BATCH_SIZE
                print(f"  [{_completed_count}/{total_batches}] 批次{batch_idx+1} 完成 "
                      f"({elapsed:.0f}s, ~{speed:.0f}条/分钟)")
            return True

        except Exception as e:
            with _progress_lock:
                _completed_count += 1
                print(f"  [{_completed_count}/{total_batches}] 批次{batch_idx+1} 失败: {e}")
            failed_batches.append((start, end))
            return False

    # 并发执行
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for i, batch_info in enumerate(batches):
            futures.append(executor.submit(process_batch, batch_info))
            # 每提交一组并发任务后短暂等待，避免瞬间打满限流
            if (i + 1) % MAX_WORKERS == 0:
                time.sleep(BATCH_INTERVAL)

        concurrent.futures.wait(futures)

    # 失败的批次标记为中性
    for start, end in failed_batches:
        for i in range(start, end):
            if "sentiment" not in comments[i]:
                comments[i]["sentiment"] = "中性"

    elapsed_total = time.time() - start_time
    print(f"\n处理完成，总耗时 {elapsed_total:.1f}s ({elapsed_total/60:.1f}分钟)")
    if failed_batches:
        print(f"  失败批次: {len(failed_batches)} 个")

    # 确保所有评论都有sentiment字段
    for c in comments:
        if "sentiment" not in c:
            c["sentiment"] = "中性"

    # 统计
    sentiment_counts = {"正面": 0, "负面": 0, "中性": 0}
    for c in comments:
        sentiment_counts[c["sentiment"]] = sentiment_counts.get(c["sentiment"], 0) + 1

    print(f"\n情感分布统计:")
    print(f"  正面: {sentiment_counts['正面']} 条 ({sentiment_counts['正面']/len(comments)*100:.1f}%)")
    print(f"  中性: {sentiment_counts['中性']} 条 ({sentiment_counts['中性']/len(comments)*100:.1f}%)")
    print(f"  负面: {sentiment_counts['负面']} 条 ({sentiment_counts['负面']/len(comments)*100:.1f}%)")

    # 保存结果JSON
    output_file = args.output or args.input_file.replace(".json", "_sentiment.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")

    # 绘制饼图
    chart_file = args.chart or "sentiment_chart.png"
    draw_pie_chart(sentiment_counts, chart_file)

    # 显示各类代表性评论
    print("\n--- 正面评论示例 ---")
    positive = [c for c in comments if c["sentiment"] == "正面"]
    for c in positive[:3]:
        print(f"  👍{c['likes']} {c['user']}: {c['content'][:60]}")

    print("\n--- 负面评论示例 ---")
    negative = [c for c in comments if c["sentiment"] == "负面"]
    for c in negative[:3]:
        print(f"  👎{c['likes']} {c['user']}: {c['content'][:60]}")


if __name__ == "__main__":
    main()
