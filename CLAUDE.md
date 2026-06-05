# Project: B站舆情分析系统

## 项目结构

```
mhy-agent/
├── bilibili_comments_browser.py        # 评论爬取（Playwright，主评论+楼中楼）
├── sentiment_analysis.py               # 情感分析（Claude API，三分类）
├── negative_analysis.py                # 负面深度分析 v2（TF-IDF + Claude议题归纳）
├── comments_BV1PnV46DEP4_full.json     # 原始评论数据（1385条）
├── comments_BV1PnV46DEP4_full_sentiment.json  # 带情感标签
├── comments_BV1PnV46DEP4_full_sentiment_topics.json  # 带议题标注
├── sentiment_chart_full.png            # 情感饼图
├── negative_keywords_chart.png         # 负面议题柱状图
├── negative_report_full.md             # 最终分析报告
└── CHANGELOG.md                        # 开发日志
```

## 技术方案

- **爬取**：Playwright浏览器自动化，page.goto()导航到B站API获取JSON，绕过风控。需要已登录的浏览器会话。
- **评论接口**：`/x/v2/reply/main`（游标翻页，主评论）+ `/x/v2/reply/reply`（页码翻页，楼中楼）
- **情感分析**：Claude API批量分类（每10条一批，批间2秒）
- **负面分析**：TF-IDF提取候选词 → Claude归纳语义议题 → Claude对每条评论分类到议题 → 按议题聚合典型评论（综合评分=likes×2+主评论加权50）

## API配置

- 使用环境变量 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL`
- 模型：`claude-opus-4-7`（中转站可用）
- `claude-sonnet-4-20250514` 在此中转站不稳定，避免使用

## 依赖

```
pip install playwright anthropic jieba matplotlib scikit-learn
playwright install chromium
```

## 运行流程

```bash
# 1. 爬取（需要登录态，首次用 --login 扫码）
python bilibili_comments_browser.py --login
python bilibili_comments_browser.py BV1PnV46DEP4

# 2. 情感分析
python sentiment_analysis.py comments_BV1PnV46DEP4_full.json

# 3. 负面深度分析
python negative_analysis.py comments_BV1PnV46DEP4_full_sentiment.json
```

## 已知问题

- B站API未登录只返回3条评论，必须有浏览器登录态
- requests直接调用会被风控，只能用Playwright导航方式
- 中转站模型通道不稳定，偶尔503需重试
