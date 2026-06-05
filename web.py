"""
B站舆情分析 Agent - Web界面

运行:
  pip install flask
  python web.py
  浏览器打开 http://localhost:5000
"""
import sys
import io
import os
import json
import uuid
from threading import Thread
from queue import Queue

# 编码
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 加载 .env 文件（不依赖 python-dotenv）
def load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_dotenv()

from flask import Flask, request, Response, render_template, send_from_directory

# 导入 agent（在 load_dotenv 之后，这样 agent.py 能读到环境变量）
from agent import TOOLS, TOOL_HANDLERS, API_KEY, BASE_URL, MODEL, SYSTEM_PROMPT, WORK_DIR

try:
    import anthropic
except ImportError:
    print("请安装: pip install anthropic flask")
    sys.exit(1)

app = Flask(__name__, template_folder="templates", static_folder="static")

# 会话存储 (session_id -> messages)
sessions = {}


def run_agent_streaming(user_input: str, session_id: str, event_queue: Queue):
    """
    带流式状态推送的 agent loop。
    每个中间步骤往 event_queue 里放消息，web层读取后通过 SSE 推给前端。
    """
    # 实时读取环境变量（支持 .env 加载）
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", None)

    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**client_kwargs)

    messages = sessions.get(session_id, [])
    messages.append({"role": "user", "content": user_input})

    try:
        while True:
            event_queue.put({"type": "status", "data": "思考中..."})

            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in assistant_content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input
                        tool_id = block.id

                        # 推送工具调用状态
                        status_msg = f"正在执行: {tool_name}"
                        if tool_name == "find_latest_video":
                            status_msg = f"🔍 查找UID {tool_input.get('uid', '')} 的最新视频..."
                        elif tool_name == "crawl_comments":
                            status_msg = f"📥 爬取视频 {tool_input.get('bvid', '')} 的评论..."
                        elif tool_name == "analyze_sentiment":
                            status_msg = "📊 情感分析中..."
                        elif tool_name == "analyze_negative":
                            status_msg = "🔬 负面深度分析中（可能需要1-2分钟）..."
                        elif tool_name == "list_files":
                            status_msg = "📂 查看已有文件..."
                        elif tool_name == "read_file":
                            status_msg = f"📄 读取文件: {tool_input.get('filename', '')}..."

                        event_queue.put({"type": "status", "data": status_msg})

                        # 执行工具
                        handler = TOOL_HANDLERS.get(tool_name)
                        if handler:
                            try:
                                result = handler(tool_input)
                            except Exception as e:
                                result = json.dumps({"error": str(e)}, ensure_ascii=False)
                        else:
                            result = json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": result,
                        })

                messages.append({"role": "user", "content": tool_results})
            else:
                # 提取最终回答
                final_text = ""
                for block in assistant_content:
                    if hasattr(block, "text"):
                        final_text += block.text

                sessions[session_id] = messages
                event_queue.put({"type": "message", "data": final_text})
                event_queue.put({"type": "done", "data": ""})
                return

    except Exception as e:
        event_queue.put({"type": "error", "data": str(e)})
        event_queue.put({"type": "done", "data": ""})


# ============ 路由 ============

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    user_input = data.get("message", "").strip()
    session_id = data.get("session_id", str(uuid.uuid4()))

    if not user_input:
        return {"error": "消息不能为空"}, 400

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not api_key:
        return {"error": "API Key 未配置，请检查 .env 文件"}, 500

    event_queue = Queue()

    # 在后台线程执行 agent
    thread = Thread(target=run_agent_streaming, args=(user_input, session_id, event_queue))
    thread.start()

    def generate():
        while True:
            event = event_queue.get()
            event_type = event["type"]
            event_data = json.dumps(event, ensure_ascii=False)
            yield f"event: {event_type}\ndata: {event_data}\n\n"
            if event_type == "done":
                break

    return Response(generate(), mimetype="text/event-stream")


@app.route("/images/<filename>")
def serve_image(filename):
    return send_from_directory(WORK_DIR, filename)


@app.route("/new_session", methods=["POST"])
def new_session():
    session_id = str(uuid.uuid4())
    sessions[session_id] = []
    return {"session_id": session_id}


if __name__ == "__main__":
    print("=" * 50)
    print("B站舆情分析 Agent - Web界面")
    print("打开浏览器访问: http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
