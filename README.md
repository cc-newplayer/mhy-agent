# B站舆情分析系统

针对B站视频评论区的自动化舆情分析流水线，覆盖数据采集、情感分类、负面深度分析全链路。

当前应用场景：游戏社区舆情监控（《崩坏：星穹铁道》）

## 功能

- **评论爬取**：Playwright 浏览器自动化，支持主评论 + 楼中楼子回复
- **情感分析**：Claude API 三分类（正面/中性/负面），4路并发，2000+条评论约1-2分钟
- **负面深度分析**：TF-IDF 提取候选词 → Claude 归纳语义议题 → 按议题聚合典型评论
- **Agent 模式**：自然语言驱动，自动决策调用顺序
- **Web 前端**：Flask + SSE 流式推送，聊天气泡交互
- **标注评估工具**：生成标注 Excel + 准确性评估报告（混淆矩阵、P/R/F1）

## 技术栈

- Python 3.13
- Playwright（浏览器自动化）
- Claude API（claude-opus-4-7）
- jieba + scikit-learn（中文分词 + TF-IDF）
- matplotlib（图表）
- Flask（Web 后端）

## 安装

```bash
pip install playwright anthropic jieba matplotlib scikit-learn flask
playwright install chromium
```

## 配置

设置环境变量：

```bash
set ANTHROPIC_AUTH_TOKEN=sk-xxxxx
set ANTHROPIC_BASE_URL=https://your-api-endpoint.com  # 可选，中转站地址
```

## 使用

### 命令行流程

```bash
# 1. 首次登录（扫码）
python login.py

# 2. 爬取评论
python bilibili_comments_browser.py BV1PnV46DEP4

# 3. 情感分析
python sentiment_analysis.py comments_BV1PnV46DEP4_full.json

# 4. 负面深度分析
python negative_analysis.py comments_BV1PnV46DEP4_full_sentiment.json
```

### Web 模式

```bash
python web.py
# 或双击 启动舆情分析.bat
```

### 标注评估

```bash
# 生成标注Excel（各类各抽50条）
python generate_annotation_excel.py

# 标注完成后生成准确性报告
python generate_accuracy_report.py sentiment_annotation_BVxxx.xlsx
```

## 项目结构

```
├── bilibili_comments_browser.py   # 评论爬取
├── sentiment_analysis.py          # 情感分析（4路并发）
├── negative_analysis.py           # 负面议题分析
├── agent.py                       # Agent 模式
├── web.py                         # Web 后端
├── templates/index.html           # Web 前端
├── login.py                       # B站登录
├── generate_annotation_excel.py   # 生成标注Excel
├── generate_accuracy_report.py    # 准确性评估报告
├── sentiment_test_v2.py           # 提示词A/B测试
├── CHANGELOG.md                   # 开发日志
└── 启动舆情分析.bat               # 一键启动
```

## 当前评估结果

| 视频 | 样本量 | 准确率 | 正面F1 | 中性F1 | 负面F1 |
|------|--------|--------|--------|--------|--------|
| BV1PnV46DEP4 | 150 | 63.3% | 54.3% | 61.0% | 75.6% |
| BV1wYdWB5EVF | 150 | 53.3% | 54.5% | 55.8% | 46.4% |

## License

MIT
