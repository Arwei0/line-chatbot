from linebot import LineBotApi
from dotenv import load_dotenv
import os

load_dotenv(encoding="utf-8-sig")
api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))

for m in api.get_rich_menu_list():
    api.delete_rich_menu(m.rich_menu_id)

print("All old rich menus deleted.")