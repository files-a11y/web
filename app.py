import os
import time
import json
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

### ========== 读取环境变量 ==========
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")
WP_BASE_URL = os.getenv("WP_BASE_URL")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")
LARK_WEBHOOK_URL = os.getenv("LARK_WEBHOOK_URL")

# ✅ Facebook
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
FB_API_VERSION = os.getenv("FB_API_VERSION", "v19.0")

### ========== Google Sheets ==========
def get_sheet():
    creds_json = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
    creds = Credentials.from_service_account_info(creds_json, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sheet = build("sheets", "v4", credentials=creds).spreadsheets()
    return sheet

### ========== 提取内容 (用于制作 FB caption short) ==========
def extract_first_paragraph(html: str, limit=200):
    try:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n")
        return text[:limit].replace("\n", "").strip()
    except:
        return ""

### ========== 发布到 WordPress ==========
def publish_to_wordpress(title, content, wp_status="publish", tags=None, categories=None):
    url = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    data = {
        "title": title,
        "content": content,
        "status": wp_status
    }

    if tags:
        data["tags"] = tags
    if categories:
        data["categories"] = categories

    res = requests.post(url, json=data, auth=(WP_USER, WP_APP_PASSWORD))
    res.raise_for_status()
    return res.json()

### ========== FB 贴文函数 ==========
def build_fb_caption(title: str, summary: str, link: str, header: str):
    caption = f"""{header}{title}
{summary}
原文阅读：{link}"""
    return caption[:1800]  # FB 限制 2000 字

def post_to_facebook(page_id, token, caption, link):
    time.sleep(1800)  # ✅ 延迟30分钟
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/feed"
    data = {
        "message": caption,
        "link": link,
        "access_token": token,
    }
    r = requests.post(url, data=data)
    r.raise_for_status()
    return r.json()

### ========== Lark 通知 ==========
def notify_lark(msg: str):
    requests.post(LARK_WEBHOOK_URL, json={"msg_type": "text", "content": {"text": msg}})

### ========== 主流程 ==========
def main():
    sheet = get_sheet()
    result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=f"{WORKSHEET_NAME}!A2:F999").execute()
    rows = result.get("values", [])

    for idx, row in enumerate(rows, start=2):
        title = row[0].strip()
        content = row[1].strip()
        status = row[2].strip().lower()
        fb_header = row[3] if len(row) > 3 else "【华语社区PH】"
        fb_caption_short = row[4] if len(row) > 4 else extract_first_paragraph(content)

        if status != "ready":
            continue

        post = publish_to_wordpress(title, content, "publish", tags=[10], categories=[3])
        wp_link = post["link"]

        caption = build_fb_caption(title, fb_caption_short, wp_link, fb_header)

        post_to_facebook(FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN, caption, wp_link)

        sheet.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{WORKSHEET_NAME}!C{idx}",
            valueInputOption="RAW",
            body={"values": [["done"]]}
        ).execute()

        notify_lark(f"✅ WP + FB 完成：{title}")

if __name__ == "__main__":
    main()
