from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage, ImageSendMessage
)
import os, threading, uuid, base64
from dotenv import load_dotenv
import requests  # Pexels

# 讀 .env（支援含 BOM）
load_dotenv(encoding="utf-8-sig")

app = Flask(__name__)

# === 環境變數 ===
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PORT = int(os.getenv("PORT", 5000))

def _mask(s):
    return f"{s[:6]}...{s[-4:]}" if s and len(s) > 12 else str(bool(s))

print("[env] LINE_TOKEN:", _mask(CHANNEL_ACCESS_TOKEN),
      "LINE_SECRET:", _mask(CHANNEL_SECRET),
      "OPENAI:", bool(OPENAI_API_KEY),
      "GEMINI:", bool(GEMINI_API_KEY),
      "PEXELS:", bool(PEXELS_API_KEY))

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

# === 影像生成（OpenAI：gpt-image-1）—若未設 OPENAI_API_KEY 就不使用 ===
def generate_image_openai(prompt: str, size: str = "1024x1024") -> str:
    """使用 gpt-image-1 生成圖片，存檔並回傳公開 URL。"""
    if not client_openai:
        raise RuntimeError("缺少 OPENAI_API_KEY，無法生成圖片")
    print("[img] generating with gpt-image-1:", prompt)
    result = client_openai.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size=size,
        response_format="b64_json",
    )
    b64 = result.data[0].b64_json
    img_bytes = base64.b64decode(b64)
    fname = f"{uuid.uuid4().hex}.png"
    fpath = os.path.join(UPLOAD_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(img_bytes)
    public_url = request.url_root.rstrip("/") + f"/files/{fname}"
    return public_url

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

# === Pexels 搜圖：回一張可直接給 LINE 的大圖 URL；找不到回 None ===
def pexels_search_image(query: str) -> str | None:
    if not PEXELS_API_KEY:
        return None
    try:
        url = "https://api.pexels.com/v1/search"
        params = {"query": query, "per_page": 1, "orientation": "landscape"}
        headers = {"Authorization": PEXELS_API_KEY}
        r = requests.get(url, params=params, headers=headers, timeout=12)
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if not photos:
            return None
        src = photos[0].get("src", {})
        return src.get("large2x") or src.get("original") or src.get("large")
    except Exception as e:
        print("[pexels] search error:", e)
        return None

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

# --- 文字訊息處理（含：/img 走 Pexels，找不到退 Picsum；若你有 OpenAI 也可改生圖） ---
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
        # --- /img：依關鍵字找圖（Pexels），找不到退 Picsum ---
        if text_lower.startswith("/img "):
            query = text_raw[5:].strip()

            # 有 Gemini 的話，先把關鍵字精煉成英文（命中率更高）— 沒有也可略過
            search_kw = query
            if client_gemini:
                try:
                    resp = client_gemini.generate_content(
                        ["請將以下需求濃縮成 3~5 個英文關鍵字，逗號分隔：", query]
                    )
                    kw = (getattr(resp, "text", "") or "").strip()
                    if kw:
                        search_kw = kw
                except Exception:
                    pass

            img_url = pexels_search_image(search_kw) or "https://picsum.photos/1024"

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
                           "- /img <內容>：依關鍵字找圖（Pexels，免費；找不到則回隨機圖）\n"
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