#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, re
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ====== 环境变量 ======
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")
WP_BASE_URL = os.getenv("WP_BASE_URL")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# 只发布标记为 ready 的行；发布后写回 done
STATUS_READY = "ready"
STATUS_DONE  = "done"

# ====== Google Sheets ======
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_info(json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=SCOPES)
sheets = build("sheets", "v4", credentials=creds)
SHEET = sheets.spreadsheets()

# ====== WordPress ======
WP_API = f"{WP_BASE_URL.rstrip('/')}/wp-json/wp/v2/posts"

def get_values(a1):
    return SHEET.values().get(spreadsheetId=SPREADSHEET_ID, range=a1).execute().get("values", [])

def set_values(a1, values):
    SHEET.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=a1,
        valueInputOption="RAW",
        body={"values": values}
    ).execute()

def find_col_index_map(headers):
    """
    根据表头自动识别列：支持常见写法（不区分大小写，去掉空格）
    建议表头：Status, Title, Content, Categories, Tags, PostID（可选）
    """
    key_map = {
        "status": ["status", "状态"],
        "title": ["title", "标题"],
        "content": ["content", "正文", "内文", "内容"],
        "categories": ["categories", "category", "分类"],
        "tags": ["tags", "tag", "标签"],
        "postid": ["postid", "post_id", "wpid", "发布id", "文章id"],
    }
    idx = {}
    norm = [re.sub(r"\s+", "", h or "").lower() for h in headers]
    for k, aliases in key_map.items():
        for i, h in enumerate(norm):
            if h in aliases:
                idx[k] = i
                break
    return idx

def parse_title_content(raw_title, raw_content):
    """
    规则：
    1) 如果表里给了 Title，就用 Title
    2) 否则从 Content 里取第一行作为 Title（去掉空格和标点装饰）
    3) Content 优先截取以“【华语社区”开头的第一段；找不到就用整段 Content
    """
    title = (raw_title or "").strip()
    content = (raw_content or "").strip()

    # 标题缺失时，用正文第一行
    if not title:
        first_line = content.splitlines()[0] if content else ""
        title = re.sub(r"^\s*[-—•\*#\d\.（）()\[\]]*\s*", "", first_line).strip()

    # 取“【华语社区”开头的第一段
    if "【华语社区" in content:
        # 按空行分段
        paras = re.split(r"\n\s*\n", content.strip())
        picked = None
        for p in paras:
            if p.strip().startswith("【华语社区"):
                picked = p.strip()
                break
        if picked:
            content = picked

    return title, content

def to_list(s):
    """逗号/中文逗号分隔转列表，去空"""
    if not s: return []
    return [x.strip() for x in re.split(r"[，,]", s) if x.strip()]

def publish_to_wp(title, content, categories, tags):
    payload = {
        "title": title,
        "content": content,
        "status": "draft",
        # 这里 categories/tags 接受的是“名称字符串数组”，你的 WP 需要有配套插件或自定义钩子支持名称创建。
        # 如果你的站点必须用 taxonomy 的 term_id，请改造成 ID 列（如 cat_ids, tag_ids）再传整数数组。
        "categories": categories,
        "tags": tags,
    }
    res = requests.post(
        WP_API,
        auth=(WP_USER, WP_APP_PASSWORD),
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    if res.status_code == 201:
        return res.json().get("id")
    else:
        print("❌ WP 失败：", res.status_code, res.text)
        return None

def main():
    # 读取表：第一行表头，后面数据
    values = get_values(f"{WORKSHEET_NAME}!A1:Z")
    if not values:
        print("表为空")
        return
    headers = values[0]
    rows = values[1:]

    col = find_col_index_map(headers)
    required = ["status", "title", "content", "categories", "tags"]
    for key in required:
        if key not in col:
            print(f"⚠️ 缺少表头: {key}（建议添加 {required} 和可选的 PostID）")
    # PostID 可选
    has_postid = "postid" in col

    for i, row in enumerate(rows, start=2):
        def get(k):
            idx = col.get(k)
            return (row[idx].strip() if (idx is not None and idx < len(row)) else "")

        status = get("status").lower()
        if status != STATUS_READY:
            # 跳过非 ready（包含 done）
            continue

        raw_title  = get("title")
        raw_content= get("content")
        raw_cats   = get("categories")
        raw_tags   = get("tags")

        title, content = parse_title_content(raw_title, raw_content)
        cats = to_list(raw_cats)
        tags = to_list(raw_tags)

        if not title:
            print(f"⚠️ 第 {i} 行标题为空，跳过")
            continue

        print(f"🚀 发布：row {i} | {title[:40]}")
        post_id = publish_to_wp(title, content, cats, tags)

        # 写回表：Status 改 done；PostID 写入（如果有）
        if post_id:
            row_out = list(row)  # 复制原行
            # status
            if col.get("status") is not None:
                at = col["status"]
                if at >= len(row_out):
                    row_out += [""] * (at + 1 - len(row_out))
                row_out[at] = STATUS_DONE
            # postid
            if has_postid:
                at = col["postid"]
                if at >= len(row_out):
                    row_out += [""] * (at + 1 - len(row_out))
                row_out[at] = str(post_id)

            # 只更新该行（整行 A:Z）
            set_values(f"{WORKSHEET_NAME}!A{i}:Z{i}", [row_out])
            print(f"✅ row {i} → done | post_id={post_id}")

        time.sleep(1)

if __name__ == "__main__":
    main()
