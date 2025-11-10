#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ====== 读取环境变量 ======
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")

WP_BASE_URL = os.getenv("WP_BASE_URL")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# ====== Google Sheets Setup ======
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_info(json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=SCOPES)
service = build("sheets", "v4", credentials=creds)
SHEET = service.spreadsheets()

# ====== WordPress API Endpoint ======
WP_API = f"{WP_BASE_URL.rstrip('/')}/wp-json/wp/v2/posts"

def read_sheet():
    """读取 Google Sheet 所有未发布的文章"""
    result = SHEET.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{WORKSHEET_NAME}!A2:D"
    ).execute()

    return result.get("values", [])

def update_row(row_number, data):
    """更新回 Google Sheet"""
    SHEET.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{WORKSHEET_NAME}!A{row_number}:D",
        valueInputOption="RAW",
        body={"values": [data]}
    ).execute()

def publish_to_wordpress(title, content, categories, tags):
    """发布文章到 WordPress"""
    headers = {"Content-Type": "application/json"}
    auth = (WP_USER, WP_APP_PASSWORD)

    payload = {
        "title": title,
        "content": content,
        "status": "draft",
        "categories": [],
        "tags": []
    }

    # 分类，以 “,” 分隔
    if categories:
        payload["categories"] = [
            c.strip() for c in categories.replace("，", ",").split(",") if c.strip()
        ]

    # 标签
    if tags:
        payload["tags"] = [
            t.strip() for t in tags.replace("，", ",").split(",") if t.strip()
        ]

    res = requests.post(WP_API, auth=auth, json=payload, timeout=60)

    if res.status_code == 201:
        post_id = res.json()["id"]
        print(f"✅ WordPress 发布成功 post_id={post_id}")
        return post_id
    else:
        print("❌ WordPress 发布失败：", res.text)
        return None

def main():
    rows = read_sheet()
    if not rows:
        print("没有可发布的文章 ✅")
        return

    for index, row in enumerate(rows, start=2):
        title, content, categories, tags = (row + ["", "", "", ""])[:4]

        if not title:
            print(f"⚠️ 第 {index} 行标题为空，跳过")
            continue

        print(f"\n🚀 发布: {title}")

        # 发布到 WP
        post_id = publish_to_wordpress(title, content, categories, tags)

        # 回写 Sheet（记录 WP post_id）
        if post_id:
            update_row(index, [title, content, f"✅ 已发布 / PostID:{post_id}", ""])
            print(f"📌 Google Sheets 更新成功 row={index}")

        time.sleep(1)

if __name__ == "__main__":
    main()
