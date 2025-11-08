import os
import time
import json
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from openai import OpenAI

# ======================
# ✅ 环境变量读取
# ======================

WP_URL = os.getenv("WP_BASE_URL").rstrip("/")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")
LARK_WEBHOOK_URL = os.getenv("LARK_WEBHOOK_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_API_VERSION = os.getenv("FB_API_VERSION", "v21.0")

FB_DELAY_MINUTES = int(os.getenv("FB_DELAY_MINUTES", "0"))  # 默认不延迟

WP_AUTO_CREATE_TERMS = True  # ✅ 分类/标签不存在自动创建

client = OpenAI(api_key=OPENAI_API_KEY)

# ======================
# ✅ Google Service Account 登录 Sheets
# ======================

creds_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
creds = service_account.Credentials.from_service_account_info(
    creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
)
sheets_service = build("sheets", "v4", credentials=creds)
sheet = sheets_service.spreadsheets()

# ======================
# ✅ WordPress 基础请求
# ======================
def wp_request(method, endpoint, json_data=None):
    url = f"{WP_URL}/wp-json/wp/v2/{endpoint}"
    res = requests.request(
        method, url, json=json_data, auth=(WP_USER, WP_APP_PASSWORD)
    )
    if not res.ok:
        raise Exception(f"WP Error {res.status_code}: {res.text}")
    return res.json()


# ======================
# ✅ 分类/标签解析 & 自动创建
# ======================
_term_cache = {}

def _resolve_term_from_wp(taxonomy: str, name_or_slug: str):
    if (taxonomy, name_or_slug) in _term_cache:
        return _term_cache[(taxonomy, name_or_slug)]

    r = requests.get(
        f"{WP_URL}/wp-json/wp/v2/{taxonomy}?search={name_or_slug}",
        auth=(WP_USER, WP_APP_PASSWORD),
    )

    if r.ok and len(r.json()) > 0:
        tid = r.json()[0]["id"]
        _term_cache[(taxonomy, name_or_slug)] = tid
        return tid
    return None


def _create_term(taxonomy: str, name: str):
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/{taxonomy}",
        json={"name": name},
        auth=(WP_USER, WP_APP_PASSWORD),
    )
    if r.ok:
        tid = r.json()["id"]
        return tid
    return None


def resolve_term_ids(value: str, taxonomy: str) -> list[int]:
    """解析分类/标签，支持 ID + 名称，不存在则创建"""
    if not value:
        return []

    names = value.replace("，", ",").split(",")
    ids = []

    for token in names:
        token = token.strip()
        if not token:
            continue

        # ✅ 如果本来就是数字
        if token.isdigit():
            ids.append(int(token))
            continue

        # ✅ 尝试 WordPress 是否已有
        tid = _resolve_term_from_wp(taxonomy, token)
        if tid:
            ids.append(tid)
            continue

        # ✅ 自动创建
        if WP_AUTO_CREATE_TERMS:
            tid = _create_term(taxonomy, token)
            if tid:
                print(f"✅ WP created term: {taxonomy} -> {token} (ID: {tid})")
                ids.append(tid)

    return ids


# ======================
# ✅ AI 自动生成 FB Caption
# ======================
def generate_fb_caption(title, content, url):
    prompt = f"""
    你是一名社交媒体编辑。根据新闻内容生成 Facebook Caption：

    标题：{title}
    内容：{content[:2800]}...

    要求：
    - 简短，吸引人
    - 可包含 Emoji
    - 加上网站链接 {url}

    输出格式：
    Caption:
    """

    res = client.chat.completions.create(
        model="gpt-5",
        messages=[{"role": "user", "content": prompt}],
    )

    return res.choices[0].message["content"]


# ======================
# ✅ 发布到 Facebook（可延迟执行）
# ======================
def publish_to_facebook(caption):
    time.sleep(FB_DELAY_MINUTES * 60)

    url = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PAGE_ID}/feed"
    payload = {"message": caption, "access_token": FB_PAGE_ACCESS_TOKEN}

    r = requests.post(url, data=payload)
    print("FB Response:", r.text)
    return r.json()


# ======================
# ✅ Main：Sheets → WordPress → Facebook
# ======================
def main():
    print("🚀 Start Google Sheets → WordPress")

    data = (
        sheet.values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{WORKSHEET_NAME}!A2:F")
        .execute()
        .get("values", [])
    )

    for row in data:
        title, content, categories_raw, tags_raw, url, status = (row + [""] * 6)[:6]

        if status.strip().lower() == "posted":
            continue

        categories = resolve_term_ids(categories_raw, "categories")
        tags = resolve_term_ids(tags_raw, "tags")

        payload = {
            "title": title,
            "content": content,
            "status": "publish",
            "categories": categories,
            "tags": tags,
        }

        wp_res = wp_request("POST", "posts", payload)
        post_url = wp_res["link"]

        caption = generate_fb_caption(title, content, post_url)
        publish_to_facebook(caption)

        print(f"✅ Published to WP & FB: {title}")


if __name__ == "__main__":
    main()
