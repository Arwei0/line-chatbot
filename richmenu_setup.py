# richmenu_setup.py  (1x3 三鍵：占星/生圖/使用教學)
import os
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import RichMenu, RichMenuArea, RichMenuBounds, MessageAction

load_dotenv(encoding="utf-8-sig")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_ACCESS_TOKEN:
    raise SystemExit("請在環境變數或 .env 設定 LINE_CHANNEL_ACCESS_TOKEN")

api = LineBotApi(CHANNEL_ACCESS_TOKEN)

# 1x3 小版型：2500 x 843
W, H = 2500, 1686
COL = W // 3  # 833

rich_menu = RichMenu(
    size={"width": W, "height": H},
    selected=True,
    name="astro_img_help",
    chat_bar_text="選單 ▾",
    areas=[
        # 占星 → /塔羅
        RichMenuArea(
            bounds=RichMenuBounds(x=0, y=0, width=COL, height=H),
            action=MessageAction(label="占星", text="/塔羅")
        ),
        # 生圖 → /圖 可愛柴犬（你可改成 /圖 台北101 夜景 等）
        RichMenuArea(
            bounds=RichMenuBounds(x=COL, y=0, width=COL, height=H),
            action=MessageAction(label="生圖", text="/圖")
        ),
        # 使用教學 → /help
        RichMenuArea(
            bounds=RichMenuBounds(x=COL*2, y=0, width=COL, height=H),
            action=MessageAction(label="使用教學", text="/help")
        ),
    ]
)

rm_id = api.create_rich_menu(rich_menu=rich_menu)
print("rich_menu_id:", rm_id)

# 上傳你的底圖（放同目錄，檔名 richmenu.png 或 .jpg）
image_path = "richmenu.png"
ctype = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
with open(image_path, "rb") as f:
    api.set_rich_menu_image(rm_id, ctype, f)

# 設為預設 Rich Menu（所有 1:1 用戶可見）
api.set_default_rich_menu(rm_id)
print("Rich menu is ready!")