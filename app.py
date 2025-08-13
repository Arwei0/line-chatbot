from flask import Flask, request, abort, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage, ImageSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)
import os, uuid, base64, random, threading
from dotenv import load_dotenv
import requests  # Pexels 搜圖用

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

# === 檔案儲存（接收用戶上傳圖檔用） ===
UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.get("/files/<path:fname>")
def serve_file(fname):
    return send_from_directory(UPLOAD_DIR, fname, as_attachment=False)

# === AI 客戶端（可選） ===
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

def ask_ai(user_text):
    if client_openai:
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
        resp = client_gemini.generate_content(user_text)
        return (getattr(resp, "text", "") or "").strip()
    return f"你說：{user_text}"

# === 可選：OpenAI 生圖（未使用時可忽略） ===
def generate_image_openai(prompt, size="1024x1024"):
    if not client_openai:
        raise RuntimeError("缺少 OPENAI_API_KEY，無法生成圖片")
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
    return request.url_root.rstrip("/") + f"/files/{fname}"

# === 群組觸發詞 ===
GROUP_TRIGGERS = ("@bot", "/ai", "小幫手")

def _should_reply_in_context(event, text_lower):
    st = getattr(event.source, "type", "user")
    if st == "user":
        return True
    return any(text_lower.startswith(t) for t in GROUP_TRIGGERS)

def _strip_trigger_prefix(text_raw):
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

# === 智慧「正在輸入中…」提示 ===
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

# === Pexels 搜圖 ===
def pexels_search_image(query):
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

# === Tarot（簡要牌義：大牌 22 張；可後續擴到 78） ===
TAROT_CARDS = [
    ("愚者 The Fool", "新的開始、冒險、自由", "魯莽、猶豫、缺乏計畫"),
    ("魔術師 The Magician", "行動力、資源、實現", "欺瞞、方向不清、資源錯配"),
    ("女祭司 The High Priestess", "直覺、潛意識、靜觀", "壓抑直覺、秘密外洩"),
    ("女皇 The Empress", "豐盛、滋養、關懷", "窒息式關愛、懶散"),
    ("皇帝 The Emperor", "結構、權威、掌控", "僵化、控制過度"),
    ("教皇 The Hierophant", "傳統、指導、規範", "反叛、因循守舊"),
    ("戀人 The Lovers", "選擇、關係、價值契合", "猶豫不決、價值衝突"),
    ("戰車 The Chariot", "意志、前進、勝利", "失控、分心、停滯"),
    ("力量 Strength", "溫柔的力量、自我掌控", "自我懷疑、情緒失衡"),
    ("隱者 The Hermit", "內省、尋道、暫歇", "孤立、逃避"),
    ("命運之輪 Wheel of Fortune", "轉機、循環、機運", "反覆、時運低迷"),
    ("正義 Justice", "公平、因果、決斷", "偏頗、不公、迴避責任"),
    ("吊人 The Hanged Man", "換位思考、犧牲、等待", "僵住不前、無謂犧牲"),
    ("死神 Death", "結束與重生、蛻變", "抗拒改變、拖延"),
    ("節制 Temperance", "平衡、調和、節奏感", "失衡、過度"),
    ("惡魔 The Devil", "束縛、欲望、依附", "掙脫、覺察"),
    ("高塔 The Tower", "瓦解、突變、覺醒", "延遲崩解、壓抑真相"),
    ("星星 The Star", "希望、療癒、願景", "失望、信心不足"),
    ("月亮 The Moon", "直覺、夢境、模糊", "迷霧散去、真相浮現"),
    ("太陽 The Sun", "成功、喜悅、清朗", "過度樂觀、精力不濟"),
    ("審判 Judgement", "覺醒、召喚、復甦", "自我苛責、猶疑"),
    ("世界 The World", "完成、整合、成就", "未竟之事、收尾拖延"),
]
POS_LABELS = ["過去", "現在", "未來"]

# 占卜會話暫存
TAROT_SESSIONS = {}  # key=(source_type,id) -> {"mode":1|3, "remaining":int, "picked":[(name,up,rev,is_rev)], "question":str}

def chat_key(event):
    t = getattr(event.source, "type", "user")
    if t == "user":
        return ("user", event.source.user_id)
    if t == "group":
        return ("group", event.source.group_id)
    if t == "room":
        return ("room", event.source.room_id)
    return ("user", None)

def draw_one_tarot():
    name, up, rev = random.choice(TAROT_CARDS)
    is_rev = random.random() < 0.5
    return name, up, rev, is_rev

def tarot_line(name, up, rev, is_rev):
    state = "逆位" if is_rev else "正位"
    meaning = (rev if is_rev else up)
    return f"{name}（{state}）\n→ {meaning}"

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

@app.post("/webhook")
def webhook_alias():
    return callback()

# --- 文字訊息處理 ---
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

    IMG_ALIASES = ("/img ", "/圖 ", "/圖片 ", "/pic ", "/photo ")

    try:
        # --- 圖片搜尋：Pexels；找不到退 Picsum ---
        if text_lower.startswith(IMG_ALIASES):
            prefix = next(p for p in IMG_ALIASES if text_lower.startswith(p))
            query = text_raw[len(prefix):].strip()
            img_url = pexels_search_image(query) or "https://picsum.photos/1024"
            if typing_timer: typing_timer.cancel()
            line_bot_api.reply_message(
                event.reply_token,
                ImageSendMessage(original_content_url=img_url, preview_image_url=img_url)
            )
            return

        # --- /塔羅：顯示五個按鈕 ---
        if text_lower == "/塔羅":
            qr = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="最近財運", text="最近財運")),
                QuickReplyButton(action=MessageAction(label="感情狀況", text="感情狀況")),
                QuickReplyButton(action=MessageAction(label="今日運勢", text="今日運勢")),
                QuickReplyButton(action=MessageAction(label="工作順利嗎", text="工作順利嗎")),
                QuickReplyButton(action=MessageAction(label="停止占卜", text="停止占卜")),
            ])
            if typing_timer: typing_timer.cancel()
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請選擇你想占卜的問題：", quick_reply=qr)
            )
            return

        # --- 選題：建立 1 張或 3 張模式 ---
        if text_raw in ("最近財運", "感情狀況", "今日運勢", "工作順利嗎", "停止占卜"):
            if text_raw == "停止占卜":
                TAROT_SESSIONS.pop(chat_key(event), None)
                final_reply = "已停止占卜。需要時再輸入 /塔羅 開始。"
            else:
                mode = 1 if text_raw == "今日運勢" else 3
                TAROT_SESSIONS[chat_key(event)] = {
                    "mode": mode,
                    "remaining": mode,
                    "picked": [],
                    "question": text_raw
                }
                qr = QuickReply(items=[QuickReplyButton(action=MessageAction(label="抽牌", text="抽牌"))])
                if mode == 3:
                    msg = f"問題：{text_raw}\n請選擇第 1 張牌（1~78 的數字），或點下方『抽牌』按鈕。"
                else:
                    msg = "問題：今日運勢\n請選擇 1~78 的數字，或點下方『抽牌』按鈕。"

                if typing_timer: typing_timer.cancel()
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg, quick_reply=qr))
                return

        # --- 抽牌/輸入號碼：逐張揭示與總結 ---
        if text_raw == "抽牌" or (text_raw.isdigit() and 1 <= int(text_raw) <= 78):
            sess = TAROT_SESSIONS.get(chat_key(event))
            if not sess:
                final_reply = "目前沒有進行中的占卜。請輸入 /塔羅 開始。"
            else:
                name, up, rev, is_rev = draw_one_tarot()
                sess["picked"].append((name, up, rev, is_rev))
                sess["remaining"] -= 1

                if sess["mode"] == 3:
                    pos = POS_LABELS[len(sess["picked"]) - 1]
                    header = f"\n"
                else:
                    header = "【今日指引】\n"

                reveal = header + tarot_line(name, up, rev, is_rev)

                if sess["remaining"] > 0:
                    qr = QuickReply(items=[QuickReplyButton(action=MessageAction(label="抽牌", text="抽牌"))])
                    msg = f"{reveal}\n\n請選擇第 {len(sess['picked'])+1} 張牌（1~78），或點『抽牌』。"
                    if typing_timer: typing_timer.cancel()
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg, quick_reply=qr))
                    return
                else:
                    # 完成
                    if sess["mode"] == 3:
                        lines = []
                        for (pname, pup, prev, pis_rev), pos in zip(sess["picked"], POS_LABELS):
                            lines.append(f"{pos}：{tarot_line(pname, pup, prev, pis_rev)}")
                        summary = "【三張牌總結】\n\n" + "\n\n".join(lines)
                    else:
                        summary = reveal + "\n\n占卜完成，祝順心！"
                    TAROT_SESSIONS.pop(chat_key(event), None)
                    final_reply = summary

        # ---- 其他指令 / 一般聊天 ----
        elif text_lower in ("hi", "hello", "嗨"):
            final_reply = "哈囉，我是你的小助理！輸入 /help 看功能。"
        elif text_lower == "/help":
            final_reply = (
                "指令：\n"
                "- /圖 或 /img /圖片 /pic /photo <內容>：Pexels 找圖（找不到回隨機圖）\n"
                "- /塔羅：按鈕選題，支援 1 張或 3 張抽牌\n"
                "- /id：顯示你的使用者ID\n"
                "- /time：伺服器時間\n"
                "- /engine：目前使用的回覆引擎\n"
                "- 其他訊息：由 AI 回覆（若無 API key 則回 Echo）"
            )
        elif text_lower == "/id":
            final_reply = f"你的ID：{event.source.user_id}"
        elif text_lower == "/time":
            import datetime
            final_reply = f"現在時間：{datetime.datetime.now()}"
        elif text_lower == "/engine":
            engine = "openai" if client_openai else ("gemini" if client_gemini else "echo")
            final_reply = f"目前引擎：{engine}"
        else:
            final_reply = ask_ai(text_raw) or f""

    except Exception as e:
        print("[handler] error:", e)
        final_reply = f""

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
        target = _push_target_id(event)
        if target:
            line_bot_api.push_message(target, TextSendMessage(text=final_reply))

# --- 用戶上傳圖片：存檔回連結 ---
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

        if getattr(event.source, "type", "user") == "user":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"已收到你的圖片 ✅\n連結：{public_url}")
            )
        else:
            # 群組/聊天室：安靜不回
            pass

    except Exception as e:
        print("[image] save failed:", e)
        if getattr(event.source, "type", "user") == "user":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="抱歉，圖片儲存失敗。"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)