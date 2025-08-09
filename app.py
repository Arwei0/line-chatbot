from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage, ImageSendMessage
)
import os, time, threading, uuid, base64
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
    raise RuntimeError("缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET，請檢查環境變數。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# === 檔案儲存：提供公開網址 (/files/...) 給 LINE 載圖（接收用戶上傳用） ===
UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.get("/files/<path:fname>")
def serve_file(fname):
    return send_from_directory(UPLOAD_DIR, fname, as_attachment=False)

# === AI 客戶端（兩個都有時優先 OpenAI）— 只影響文字聊天，不影響 /img ===
client_openai = None
client_gemini = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client_openai = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("[init] OpenAI init error:", e)
if GEMINI_API_KEY and client_openai is None:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        client_gemini = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        print("[init] Gemini init error:", e)

SYSTEM_PROMPT = "你是友善、清楚的中文助理，回覆要重點清楚、必要時給步驟與範例。"

def ask_ai(user_text: str) -> str:
    """文字對話：有 OpenAI 用 OpenAI；否則用 Gemini；都沒有就 Echo。"""
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

# === 群組觸發詞 ===
GROUP_TRIGGERS = ("@bot", "/ai", "小幫手")

def _should_reply_in_context(event, text_lower: str) -> bool:
    """私訊一律回；群組/多人聊天室需觸發詞開頭"""
    st = getattr(event.source, "type", "user")
    if st == "user":
        return True
    return any(text_lower.startswith(t) for t in GROUP_TRIGGERS)

def _strip_trigger_prefix(text_raw: str) -> str:
    tl = text_raw.strip().lower()
    for t in GROUP_TRIGGERS:
        if tl.startswith(t):
            return text_raw[len(t):].strip()
    return text_raw

def _push_target_id(event):
    st = getattr(event.source, "type", "user")
    if st == "user":
        return event.source.user_id
    if st == "group":
        return event.source.group_id
    if st == "room":
        return event.source.room_id
    return None

# === 智慧「正在輸入中…」：延遲 1 秒才送；若答案先準備好就取消，不會出現 ===
TYPING_DELAY_SEC = 1.0

def _schedule_typing(target_id, delay=TYPING_DELAY_SEC):
    def _send():
        try:
            line_bot_api.push_message(target_id, TextSendMessage(text="正在輸入中…"))
        except Exception as e:
            print("[typing] push failed:", e)
    t = threading.Timer(delay, _send)
    t.start()
    return t

# --- 健康檢查 ---
@app.get("/")
def health():
    return "OK"

# --- Webhook 路由 ---
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@app.post("/webhook")  # 若後台填 /webhook 也相容
def webhook_alias():
    return callback()

# --- 文字訊息處理（含：/img 走 Picsum 隨機圖） ---
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text_raw = (event.message.text or "").strip()
    text_lower = text_raw.lower()

    # 群組/聊天室：沒叫到就不回
    if not _should_reply_in_context(event, text_lower):
        return

    # 去掉觸發詞
    text_raw = _strip_trigger_prefix(text_raw)
    text_lower = text_raw.lower()

    target_id = _push_target_id(event)
    typing_timer = _schedule_typing(target_id, delay=TYPING_DELAY_SEC) if target_id else None

    try:
        # --- /img：用 Picsum 回一張隨機圖（免費、零金鑰） ---
        if text_lower.startswith("/img "):
            # 保留關鍵字但 Picsum 不支援查詢，純隨機圖
            # 你之後拿到 Pexels key 再把這裡改掉即可
            img_url = "https://picsum.photos/1024"
            if typing_timer: typing_timer.cancel()
            line_bot_api.reply_message(
                event.reply_token,
                ImageSendMessage(original_content_url=img_url, preview_image_url=img_url)
            )
            return

        # ---- 一般指令 / 文字 → 走 AI 或固定回覆 ----
        if text_lower in ("hi", "hello", "嗨"):
            final_reply = "哈囉，我是你的小助理！輸入 /help 看功能。"
        elif text_lower == "/help":
            final_reply = ("指令：\n"
                           "- /img <內容>：回一張隨機圖片（Picsum，之後可換 Pexels 搜圖）\n"
                           "- /id：顯示你的使用者ID\n"
                           "- /time：伺服器時間\n"
                           "- /engine：目前使用的回覆引擎\n"
                           "- 其他訊息：由 AI 回覆（若無 API key 則回 Echo）")
        elif text_lower == "/id":
            final_reply = f"你的ID：{event.source.user_id}"
        elif text_lower == "/time":
            import datetime
            final_reply = f"現在時間：{datetime.datetime.now()}"
        elif text_lower == "/engine":
            engine = "openai" if client_openai else ("gemini" if client_gemini else "echo")
            final_reply = f"目前引擎：{engine}"
        else:
            final_reply = ask_ai(text_raw) or f"你說：{text_raw}"

    except Exception as e:
        print("[handler] error:", e)
        final_reply = f"你說：{text_raw}"

    # 取消 typing（如果還沒送出）
    try:
        if typing_timer:
            typing_timer.cancel()
    except Exception:
        pass

    # 回覆文字（優先 reply；失敗改 push）
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=final_reply))
    except Exception as e:
        print("[reply] failed, fallback to push:", e)
        if target_id:
            line_bot_api.push_message(target_id, TextSendMessage(text=final_reply))

# ---（選配）處理用戶上傳的圖片：存檔後回傳 ---
@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    try:
        content = line_bot_api.get_message_content(event.message.id)
        fname = f"{uuid.uuid4().hex}.jpg"
        fpath = os.path.join(UPLOAD_DIR, fname)
        with open(fpath, "wb") as f:
            for chunk in content.iter_content():
                f.write(chunk)
        public_url = request.url_root.rstrip("/") + f"/files/{fname}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"我收到你的圖片！連結：{public_url}"))
        target_id = _push_target_id(event)
        if target_id:
            line_bot_api.push_message(target_id, ImageSendMessage(
                original_content_url=public_url, preview_image_url=public_url
            ))
    except Exception as e:
        print("[image] save failed:", e)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="抱歉，圖片儲存失敗。"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)