# richmenu_setup.py
import os
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import (
    RichMenu, RichMenuArea, RichMenuBounds,
    MessageAction
)


# 放在檔案開頭 imports 附近：
# from PIL import Image, ImageDraw, ImageFont

# 替換 image_path 段落：
image_path = "richmenu.png"
if not os.path.exists(image_path):
    from PIL import Image, ImageDraw
    W, H = 2500, 1686
    img = Image.new("RGB", (W, H), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.text((60, 60), "占星 / 生圖 / 使用教學", fill=(255,255,255))
    tmp_path = "richmenu_tmp.png"
    img.save(tmp_path)
    image_path = tmp_path

    
load_dotenv(encoding="utf-8-sig")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_ACCESS_TOKEN:
    raise SystemExit("請在 .env 或環境變數設置 LINE_CHANNEL_ACCESS_TOKEN")

api = LineBotApi(CHANNEL_ACCESS_TOKEN)

# 2x3 版型：2500x1686（官方建議尺寸）
W, H = 2500, 1686
ROW_H = H // 2            # 843
COL_W1 = 833              # 833 + 834 + 833 = 2500
COL_W2 = 834
COL_W3 = 833

areas = [
    # 第一列（上排）
    RichMenuArea(bounds=RichMenuBounds(x=0,           y=0,        width=COL_W1, height=ROW_H),
                 action=MessageAction(label="占星", text="/塔羅")),
    RichMenuArea(bounds=RichMenuBounds(x=COL_W1,      y=0,        width=COL_W2, height=ROW_H),
                 action=MessageAction(label="生圖", text="/圖 可愛柴犬")),
    RichMenuArea(bounds=RichMenuBounds(x=COL_W1+COL_W2, y=0,      width=COL_W3, height=ROW_H),
                 action=MessageAction(label="使用教學", text="/help")),

    # 第二列（下排）— 先放暫時功能
    RichMenuArea(bounds=RichMenuBounds(x=0,           y=ROW_H,    width=COL_W1, height=ROW_H),
                 action=MessageAction(label="即將推出", text="更多功能")),
    RichMenuArea(bounds=RichMenuBounds(x=COL_W1,      y=ROW_H,    width=COL_W2, height=ROW_H),
                 action=MessageAction(label="即將推出", text="更多功能")),
    RichMenuArea(bounds=RichMenuBounds(x=COL_W1+COL_W2, y=ROW_H,  width=COL_W3, height=ROW_H),
                 action=MessageAction(label="即將推出", text="更多功能")),
]

rich_menu = RichMenu(
    size={"width": W, "height": H},
    selected=True,
    name="main_menu_2x3",
    chat_bar_text="選單 ▾",
    areas=areas
)

# 1) 建立 Rich Menu
rm_id = api.create_rich_menu(rich_menu=rich_menu)
print("rich_menu_id:", rm_id)

# 2) 上傳背景圖（richmenu.png / richmenu.jpg）
image_path = "richmenu.png"
content_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
with open(image_path, "rb") as f:
    api.set_rich_menu_image(rm_id, content_type, f)

# 3) 設成預設 Rich Menu（所有 1:1 使用者都會看到）
api.set_default_rich_menu(rm_id)
print("Rich menu is ready!")