"""
B站负面评论分析 v2 - 混合方案（TF-IDF + Claude语义归类）

流程：
  1. 读取情感分析后的JSON，筛选负面评论
  2. TF-IDF提取候选关键词（相对于全量评论的区分度）
  3. Claude对候选词进行语义归类，归纳为具体议题（去除废词）
  4. Claude对每条负面评论标注所属议题
  5. 按议题聚合，每个议题列出典型评论（按综合评分排序）
  6. 生成柱状图 + Markdown报告

依赖：
  pip install jieba matplotlib anthropic scikit-learn

用法：
  python negative_analysis.py comments_BV1PnV46DEP4_full_sentiment.json
"""
import sys
import io
import json
import re
import os
import argparse
import time
from collections import Counter, defaultdict
from datetime import datetime

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import jieba
from sklearn.feature_extraction.text import TfidfVectorizer

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    import anthropic
except ImportError:
    print("请安装 anthropic: pip install anthropic")
    sys.exit(1)

# API配置
API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", None)

# 停用词
STOP_WORDS = set("""
的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
这 他 她 它 们 那 个 为 什么 吗 吧 啊 呢 嘛 啦 哦 哈 呀 把 让 被 给 用 于
可以 但是 还是 如果 虽然 因为 所以 而且 或者 以及 这个 那个 什么 怎么 为什么
没 还 又 已经 正在 可能 应该 比较 非常 真的 其实 感觉 觉得 知道 自己 这么
而 之 与 及 其 从 对 等 已 时 过 后 前 里 中 下 更 最 太 才 只 然后
回复 就是 不是 可恶 几把 时候 一下 出来 起来 这里 那里 东西 大家 所有
doge doge_金箍 星星眼 大哭 笑哭 给心心 保佑 微笑 难过 思考 打call
哈哈 哈哈哈 哈哈哈哈
""".split())


def clean_text(text):
    """清洗评论文本"""
    text = re.sub(r"回复\s*@[^:：]+[:：]\s*", "", text)
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"[#@]", "", text)
    return text.strip()


def tokenize(text):
    """jieba分词，返回过滤后的词列表"""
    text = clean_text(text)
    words = jieba.cut(text)
    return [w for w in words
            if len(w) >= 2
            and w not in STOP_WORDS
            and not w.isdigit()
            and not all(c in "，。！？、；：""''（）【】…—～·" for c in w)]


def extract_tfidf_keywords(all_comments, negative_comments, top_n=30):
    """
    用TF-IDF提取负面评论中区分度高的候选词
    以全量评论为语料库，负面评论为目标文档
    """
    # 把所有评论分词
    all_texts = [" ".join(tokenize(c["content"])) for c in all_comments]
    neg_texts = [" ".join(tokenize(c["content"])) for c in negative_comments]

    # 合并负面评论为一个文档，与全量对比
    vectorizer = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b")
    # 语料 = 全量单条 + 负面合并文档
    corpus = all_texts + [" ".join(neg_texts)]
    tfidf_matrix = vectorizer.fit_transform(corpus)

    # 取最后一行（负面合并文档）的TF-IDF值
    feature_names = vectorizer.get_feature_names_out()
    neg_vector = tfidf_matrix[-1].toarray()[0]

    # 按TF-IDF值排序
    word_scores = [(feature_names[i], neg_vector[i])
                   for i in range(len(feature_names))
                   if neg_vector[i] > 0 and len(feature_names[i]) >= 2]
    word_scores.sort(key=lambda x: x[1], reverse=True)

    # 同时统计词频，TF-IDF高但只出现1次的意义不大
    neg_word_counts = Counter()
    for text in neg_texts:
        for w in text.split():
            neg_word_counts[w] += 1

    # 综合评分：TF-IDF * log(词频+1)
    import math
    candidates = []
    for word, score in word_scores[:100]:
        freq = neg_word_counts.get(word, 0)
        if freq >= 3:  # 至少出现3次
            combined = score * math.log(freq + 1)
            candidates.append((word, freq, combined))

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[:top_n]


def claude_call(client, prompt, max_tokens=2048):
    """封装Claude API调用"""
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def identify_topics(client, candidates, negative_comments):
    """让Claude基于候选词+负面评论归纳议题"""
    # 取点赞高的负面评论作为上下文
    sorted_neg = sorted(negative_comments, key=lambda x: x["likes"], reverse=True)
    sample_texts = "\n".join(
        f"- (👍{c['likes']}) {clean_text(c['content'])[:120]}"
        for c in sorted_neg[:30]
    )
    candidate_text = "、".join(f"{w}({freq}次)" for w, freq, _ in candidates[:20])

    prompt = f"""你是游戏社区舆情分析师。以下是一个B站游戏角色PV视频下负面评论的高频候选词和典型负面评论。

候选高频词：{candidate_text}

典型负面评论（按点赞排序，前30条）：
{sample_texts}

请基于以上信息，归纳出5-8个具体的"负面议题"。每个议题应该：
1. 用一个简短标签命名（2-6字，如"PV时长过短"）
2. 用一句话描述具体含义
3. 列出与该议题相关的候选词

去掉纯情绪词（如"可恶""几把"）和无实际指向的词。

请严格按以下JSON格式输出：
[
  {{"label": "议题标签", "description": "一句话描述", "keywords": ["相关词1", "相关词2"]}},
  ...
]
"""
    result = claude_call(client, prompt)
    start = result.find("[")
    end = result.rfind("]") + 1
    return json.loads(result[start:end])


def classify_comments_to_topics(client, topics, negative_comments):
    """让Claude对负面评论标注所属议题"""
    topic_labels = [t["label"] for t in topics]
    labels_text = "、".join(topic_labels)

    # 分批处理
    BATCH = 20
    total_batches = (len(negative_comments) + BATCH - 1) // BATCH
    all_assignments = []

    for batch_idx in range(total_batches):
        start = batch_idx * BATCH
        end = min(start + BATCH, len(negative_comments))
        batch = negative_comments[start:end]

        comments_text = ""
        for i, c in enumerate(batch):
            text = clean_text(c["content"])[:100]
            comments_text += f"{i+1}. {text}\n"

        prompt = f"""将以下评论分类到对应的负面议题。每条评论可以属于1-2个议题，也可能不属于任何议题（标记为"其他"）。

可选议题：{labels_text}、其他

评论：
{comments_text}

请严格按JSON数组输出，不要输出其他内容：
[{{"index": 1, "topics": ["议题1"]}}, {{"index": 2, "topics": ["其他"]}}, ...]
"""
        result = claude_call(client, prompt, max_tokens=2048)
        s = result.find("[")
        e = result.rfind("]") + 1
        try:
            assignments = json.loads(result[s:e])
            all_assignments.extend(assignments)
        except (json.JSONDecodeError, ValueError):
            # 失败的批次标记为其他
            for i in range(len(batch)):
                all_assignments.append({"index": i + 1, "topics": ["其他"]})

        print(f"    分类第 {batch_idx+1}/{total_batches} 批...", end=" ")
        if batch_idx < total_batches - 1:
            time.sleep(2)
    print("完成")

    # 映射回评论
    for i, assignment in enumerate(all_assignments):
        if i < len(negative_comments):
            negative_comments[i]["topics"] = assignment.get("topics", ["其他"])

    return negative_comments


def compute_comment_score(comment):
    """综合评分：点赞 + 是否主评论加权"""
    likes = comment.get("likes", 0)
    is_main = not comment.get("is_reply", True)
    return likes * 2 + (50 if is_main else 0)


def draw_topic_chart(topic_stats, output_path):
    """绘制议题分布柱状图"""
    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    labels = [t["label"] for t in topic_stats]
    counts = [t["count"] for t in topic_stats]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(range(len(labels)), counts, color="#F44336", alpha=0.8)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=11)
    ax.set_xlabel("评论数", fontsize=11)
    ax.set_title("负面议题分布", fontsize=14, fontweight="bold")
    ax.invert_yaxis()

    for bar, count in zip(bars, counts):
        ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
                str(count), va="center", fontsize=10)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  柱状图已保存: {output_path}")


def generate_suggestions(client, topic_stats):
    """基于议题生成运营建议"""
    topics_text = ""
    for t in topic_stats:
        topics_text += f"- {t['label']}({t['count']}条): {t['description']}\n"
        for c in t["typical"][:2]:
            topics_text += f"  典型: (👍{c['likes']}) {clean_text(c['content'])[:80]}\n"

    prompt = f"""你是游戏社区运营顾问。基于以下B站视频评论区的负面议题分析，给运营团队提出改进建议。

负面议题（按严重程度排序）：
{topics_text}

要求：
- 每个议题对应1条建议，共{len(topic_stats)}条
- 每条不超过50字
- 要具体可执行
- 明确指出针对哪个议题

格式：
1. [议题标签] 建议内容
2. [议题标签] 建议内容
...
"""
    return claude_call(client, prompt, max_tokens=1024)


def generate_report(all_count, negative_comments, topic_stats, chart_path, suggestions):
    """生成Markdown报告"""
    neg_count = len(negative_comments)
    ratio = neg_count / all_count * 100

    report = f"""# B站评论负面分析报告 v2

> 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
> 分析方法: TF-IDF候选词提取 + Claude语义议题归纳

## 1. 概览

| 指标 | 数值 |
|------|------|
| 总评论数 | {all_count} |
| 负面评论数 | {neg_count} |
| 负面占比 | {ratio:.1f}% |
| 识别议题数 | {len(topic_stats)} |

## 2. 负面议题分布

![负面议题分布]({chart_path})

"""
    for i, t in enumerate(topic_stats, 1):
        report += f"### 议题{i}: {t['label']}（{t['count']}条，占负面{t['count']/neg_count*100:.0f}%）\n\n"
        report += f"**含义**: {t['description']}\n\n"
        report += f"**相关词**: {', '.join(t.get('keywords', []))}\n\n"
        report += f"**典型评论**:\n\n"
        for c in t["typical"][:3]:
            is_main_tag = "💬" if not c.get("is_reply") else "↩️"
            content = clean_text(c["content"])[:150]
            report += f"- {is_main_tag} **{c['user']}** (👍{c['likes']}): {content}\n"
        report += "\n"

    report += f"""## 3. 运营改进建议

{suggestions}

---
*本报告由自动化脚本生成。分析方法：TF-IDF提取候选词 → Claude归纳语义议题 → 按议题聚合典型评论。*
"""
    return report


def main():
    parser = argparse.ArgumentParser(description="负面评论分析 v2 (TF-IDF + Claude)")
    parser.add_argument("input_file", help="情感分析后的JSON文件")
    parser.add_argument("--output", default="negative_report.md", help="输出报告文件名")
    args = parser.parse_args()

    if not API_KEY:
        print("错误: 未找到 API Key")
        sys.exit(1)

    # 初始化Claude
    client_kwargs = {"api_key": API_KEY}
    if BASE_URL:
        client_kwargs["base_url"] = BASE_URL
    client = anthropic.Anthropic(**client_kwargs)

    # 读取数据
    print(f"读取文件: {args.input_file}")
    with open(args.input_file, "r", encoding="utf-8") as f:
        comments = json.load(f)

    all_count = len(comments)
    negative = [c for c in comments if c.get("sentiment") == "负面"]
    print(f"  总评论: {all_count}, 负面: {len(negative)}")

    if len(negative) < 3:
        print("负面评论太少，无需深度分析")
        return

    # Step 1: TF-IDF提取候选词
    print("\n[Step 1] TF-IDF提取候选关键词...")
    candidates = extract_tfidf_keywords(comments, negative, top_n=25)
    print(f"  候选词: {', '.join(f'{w}({freq})' for w, freq, _ in candidates[:15])}")

    # Step 2: Claude归纳议题
    print("\n[Step 2] Claude归纳负面议题...")
    topics = identify_topics(client, candidates, negative)
    print(f"  识别到 {len(topics)} 个议题:")
    for t in topics:
        print(f"    - {t['label']}: {t['description']}")
    time.sleep(2)

    # Step 3: 对负面评论分类
    print(f"\n[Step 3] 对 {len(negative)} 条负面评论进行议题分类...")
    negative = classify_comments_to_topics(client, topics, negative)

    # Step 4: 按议题聚合 + 排序典型评论
    print("\n[Step 4] 聚合议题统计...")
    topic_comments = defaultdict(list)
    for c in negative:
        for t in c.get("topics", ["其他"]):
            topic_comments[t].append(c)

    topic_stats = []
    for t in topics:
        label = t["label"]
        comments_in_topic = topic_comments.get(label, [])
        # 按综合评分排序
        comments_in_topic.sort(key=compute_comment_score, reverse=True)
        topic_stats.append({
            "label": label,
            "description": t["description"],
            "keywords": t.get("keywords", []),
            "count": len(comments_in_topic),
            "typical": comments_in_topic[:3],
        })

    # 按评论数排序
    topic_stats.sort(key=lambda x: x["count"], reverse=True)

    for t in topic_stats:
        print(f"  {t['label']}: {t['count']}条")

    # Step 5: 绘图
    chart_path = "negative_keywords_chart.png"
    print(f"\n[Step 5] 生成柱状图...")
    draw_topic_chart(topic_stats, chart_path)

    # Step 6: 生成建议
    print("\n[Step 6] Claude生成运营建议...")
    suggestions = generate_suggestions(client, topic_stats)
    print(f"  {suggestions}")

    # Step 7: 生成报告
    print(f"\n[Step 7] 生成Markdown报告...")
    report = generate_report(all_count, negative, topic_stats, chart_path, suggestions)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  报告已保存: {args.output}")

    # 保存带议题标注的数据
    output_data = args.input_file.replace(".json", "_topics.json")
    with open(output_data, "w", encoding="utf-8") as f:
        json.dump(negative, f, ensure_ascii=False, indent=2)
    print(f"  议题标注数据已保存: {output_data}")

    print("\n完成！")


if __name__ == "__main__":
    main()

