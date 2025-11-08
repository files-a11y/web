import os
import json
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from openai import OpenAI

# -------------------------
# ENVIRONMENT VARIABLES
# -------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WP_BASE_URL = os.getenv("WP_BASE_URL")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# GOOGLE SHEETS
# -------------------------
def get_sheet_data():
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )

    sheets_service = build("sheets", "v4", credentials=creds)
    sheet = sheets_service.spreadsheets()

    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{WORKSHEET_NAME}!A:Z"
    ).execute()

    return result.get("values", [])

# -------------------------
# CHATGPT — FACEBOOK CAPTION GENERATION
# -------------------------
def generate_fb_caption(title, content):
    prompt = f"""
你是菲律宾华文媒体编辑。请将以下新闻内容缩短为适合 Facebook 的 caption：
- 必须是简体中文
- 保持新闻事实性
- 添加 5-10 个相关 Hashtags

标题：{title}
内容：{content}

输出格式：
Caption:
Hashtags:
"""

    response = client.chat.completions.create(
        model="gpt-5",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.choices[0].message["content"]

# -------------------------
# WORDPRESS POST
# -------------------------
def publish_to_wordpress(title, content, categories, tags):
    wp_url = f"{WP_BASE_URL}/wp-json/wp/v2/posts"

    post = {
        "title": title,
        "content": content,
        "status": "publish",
        "categories": categories,
        "tags": tags,
    }

    res = requests.post(
        wp_url,
        json=post,
        auth=(WP_USER, WP_APP_PASSWORD)
    )

    return res.json()

# -------------------------
# MAIN PROCESS
# -------------------------
def main():
    rows = get_sheet_data()

    header = rows[0]
    col_index = {col: idx for idx, col in enumerate(header)}

    for row in rows[1:]:
        status = row[col_index["status"]].strip()
        if status.lower() != "ready":
            continue

        title = row[col_index["title"]]
        content = row[col_index["content"]]
        categories = row[col_index["categories"]]
        tags = row[col_index["tags"]]

        print(f"🔄 Publishing: {title}")

        # ✅ Generate Facebook caption
        fb_caption = generate_fb_caption(title, content)

        # ✅ Publish to WP
        wp_response = publish_to_wordpress(title, content, categories, tags)

        print("✅ WP Published:", wp_response.get("link"))

        # ✅ Update row to DONE (optional - 可加)

    print("✅ All done.")

if __name__ == "__main__":
    main()
