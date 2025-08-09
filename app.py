from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
from dotenv import load_dotenv

# 讀 .env（支援含 BOM）
load_dotenv(encoding="utf-8-sig")

app = Flask(__name__)

# === 環境變數 ===
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 5000))

def _mask(s):
    return f"{s[:6]}...{s[-4:]}" if s and len(s) > 12 else str(bool(s))

print("[env] LINE_TOKEN:", _mask(CHANNEL_ACCESS_TOKEN),
      "LINE_SECRET:", _mask(CHANNEL_SECRET),
      "OPENAI:", bool(OPENAI_API_KEY),
      "GEMINI:", bool(GEMINI_API_KEY))

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET，請檢查 .env 與路徑。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# === AI 客戶端 ===
client_openai = None
client_gemini = None

if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client_openai = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("[init] OpenAI 初始化失敗：", e)

if GEMINI_API_KEY and client_openai is None:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        client_gemini = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        print("[init] Gemini 初始化失敗：", e)

SYSTEM_PROMPT = "你是友善、清楚的中文助理，回覆要重點清楚、必要時給步驟與範例。"

def ask_ai(user_text: str) -> str:
    """依照可用金鑰選擇 AI；失敗時丟例外讓上層回退 Echo。"""
    if client_openai:
        print("[ai] using openai")
        resp = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.5,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            timeout=20,
        )
        return (resp.choices[0].message.content or "").strip()

    if client_gemini:
        print("[ai] using gemini")
        resp = client_gemini.generate_content(user_text)
        return (getattr(resp, "text", "") or "").strip()

    print("[ai] no api key -> echo")
    return f"你說：{user_text}"

# === 群組觸發詞設定 ===
GROUP_TRIGGERS = ("@bot", "/ai", "小幫手")

def _should_reply_in_context(event, text_lower: str) -> bool:
    """私訊 -> 一律回；群組/多人聊天室 -> 需要觸發詞開頭"""
    src_type = getattr(event.source, "type", "user")
    if src_type == "user":
        return True
    return any(text_lower.startswith(t) for t in GROUP_TRIGGERS)

def _strip_trigger_prefix(text_raw: str) -> str:
    """去掉觸發詞前綴"""
    tl = text_raw.strip().lower()
    for t in GROUP_TRIGGERS:
        if tl.startswith(t):
            return text_raw[len(t):].strip()
    return text_raw

# --- 健康檢查 ---
@app.get("/")
def health():
    return "OK"

# --- LINE Webhook ---
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@app.post("/webhook")
def webhook_alias():
    return callback()

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text_raw = (event.message.text or "").strip()
    text_lower = text_raw.lower()

    # 判斷是否該回覆（群組需要觸發詞）
    if not _should_reply_in_context(event, text_lower):
        return

    # 如果有觸發詞，把它去掉
    text_raw = _strip_trigger_prefix(text_raw)
    text_lower = text_raw.lower()

    # --- 固定指令 ---
    if text_lower in ("hi", "hello", "嗨"):
        reply = "哈囉，我是你的小助理！輸入 /help 看功能。"
    elif text_lower == "/help":
        reply = ("指令：\n"
                 "- /id：顯示你的使用者ID\n"
                 "- /time：伺服器時間\n"
                 "- /engine：目前使用的回覆引擎\n"
                 "- 其他訊息：由 AI 回覆（若無 API key 則回 Echo）")
    elif text_lower == "/id":
        reply = f"你的ID：{event.source.user_id}"
    elif text_lower == "/time":
        import datetime
        reply = f"現在時間：{datetime.datetime.now()}"
    elif text_lower == "/engine":
        engine = "openai" if client_openai else ("gemini" if client_gemini else "echo")
        reply = f"目前引擎：{engine}"
    else:
        try:
            reply = ask_ai(text_raw) or f"你說：{text_raw}"
        except Exception as e:
            print("[ai] error -> fallback echo:", e)
            reply = f"你說：{text_raw}"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)