# -*- coding: utf-8 -*-
"""
Sheets -> WordPress (draft) -> (delay) Facebook
- 不调用 OpenAI
- FB 文案 = 标题 + 正文中“【华语社区/華語社區 …”的第一段（否则用第二段）
- 仅处理 status == "ready" 的行，完成后写回 post_id / wp_link / last_synced，并将 status 改为 "done"
- 支持分类/标签【名称 或 ID】；名称不存在时自动创建

需要的 Secrets / 环境变量：
WP_BASE_URL          例如：https://filchioc.com
WP_USER              WordPress 用户名
WP_APP_PASSWORD      WordPress 应用密码
SPREADSHEET_ID       Google Sheet ID
WORKSHEET_NAME       Sheet 名称（例如：Sheet1）
GOOGLE_SERVICE_ACCOUNT_JSON  Google Service Account JSON（整段）

FB_PAGE_ID
FB_PAGE_ACCESS_TOKEN
FB_API_VERSION       （可选，默认 v21.0）
FB_DELAY_MINUTES     （可选，默认 30；用于等待你发布成正式文章）

可选开关：
WP_AUTO_CREATE_TERMS=true/false   （默认 true，自动创建不存在的分类/标签）
"""

import os
import re
import json
import time
import datetime
import requests
from typing import Dict, Any, List, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build


# ======================
# 读取环境变量
# ======================
WP_URL = (os.getenv("WP_BASE_URL") or "").rstrip("/")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")
SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
FB_API_VERSION = os.getenv("FB_API_VERSION", "v21.0")
FB_DELAY_MINUTES = int(os.getenv("FB_DELAY_MINUTES", "30") or "30")

WP_AUTO_CREATE_TERMS = (os.getenv("WP_AUTO_CREATE_TERMS", "true").lower() == "true")


# ======================
# Google Sheets 客户端
# ======================
if not SA_JSON:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON 未设置")

creds_info = json.loads(SA_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
sheets_service = build("sheets", "v4", credentials=creds)
SHEET = sheets_service.spreadsheets()


# ======================
# 工具函数
# ======================
def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)   # 去 HTML
    s = re.sub(r"\s+", " ", s)      # 压空白
    return s.strip()

def split_paras(raw: str) -> List[str]:
    if not raw:
        return []
    lines = [l.strip() for l in raw.splitlines()]
    # 合并空行：段落
    paras = []
    buf = []
    for ln in lines:
        if ln:
            buf.append(ln)
        else:
            if buf:
                paras.append(" ".join(buf).strip())
                buf = []
    if buf:
        paras.append(" ".join(buf).strip())
    return [p for p in paras if p]


# ======================
# 提取标题和正文（无 AI）
# ======================
def pick_title_and_body(raw: str, title: str, content: str) -> Tuple[str, str]:
    """
    规则：
    - 如果 raw 存在且 title/content 为空：raw 的第一段作为标题；
      正文取 raw 中第一个以“【华语社区/華語社區”开头的段落；若找不到用第二段；都没有则空。
    - 否则直接用传入的 title/content（都会清洗）。
    """
    raw = (raw or "").strip()
    title = clean_text(title or "")
    content = clean_text(content or "")

    if raw and not (title or content):
        paras = split_paras(raw)
        if paras:
            title = paras[0]
            chosen = ""
            for p in paras[1:]:
                if p.startswith("【华语社区") or p.startswith("【華語社區"):
                    chosen = p
                    break
            if not chosen and len(paras) > 1:
                chosen = paras[1]
            content = clean_text(chosen or "")
    return (title or "(untitled)"), (content or "")


# ======================
# WordPress API 基础
# ======================
def wp_request(method: str, endpoint: str, *, json_body: Dict[str, Any] = None, params: Dict[str, Any] = None):
    url = f"{WP_URL}/wp-json/wp/v2/{endpoint}"
    r = requests.request(method, url, json=json_body, params=params, auth=(WP_USER, WP_APP_PASSWORD), timeout=60)
    if not r.ok:
        raise RuntimeError(f"[WP {method} {endpoint}] {r.status_code}: {r.text}")
    return r.json()

def wp_get_post(post_id: int) -> Dict[str, Any]:
    return wp_request("GET", f"posts/{post_id}")

_TERM_CACHE: Dict[Tuple[str, str], int] = {}

def _wp_search_terms(taxonomy: str, term: str) -> int | None:
    key = (taxonomy, term.lower().strip())
    if key in _TERM_CACHE:
        return _TERM_CACHE[key]
    r = requests.get(
        f"{WP_URL}/wp-json/wp/v2/{taxonomy}",
        params={"search": term, "per_page": 100},
        auth=(WP_USER, WP_APP_PASSWORD),
        timeout=30,
    )
    if r.ok:
        for item in r.json():
            name = (item.get("name") or "").strip().lower()
            slug = (item.get("slug") or "").strip().lower()
            if term.lower().strip() in (name, slug):
                _TERM_CACHE[key] = int(item["id"])
                return int(item["id"])
    return None

def _wp_create_term(taxonomy: str, name: str) -> int | None:
    if not WP_AUTO_CREATE_TERMS:
        return None
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/{taxonomy}",
        json={"name": name},
        auth=(WP_USER, WP_APP_PASSWORD),
        timeout=30,
    )
    if r.ok:
        tid = int(r.json()["id"])
        _TERM_CACHE[(taxonomy, name.lower().strip())] = tid
        return tid
    return None

def resolve_term_ids(term_str: str, taxonomy: str) -> List[int]:
    """支持：'新闻, Philippines, 10'（名称/slug/数字混填）"""
    if not term_str:
        return []
    ids: List[int] = []
    for token in term_str.replace("，", ",").split(","):
        t = token.strip()
        if not t:
            continue
        if t.isdigit():
            ids.append(int(t))
            continue
        tid = _wp_search_terms(taxonomy, t)
        if tid:
            ids.append(tid)
            continue
        tid = _wp_create_term(taxonomy, t)
        if tid:
            ids.append(tid)
    # 去重保持顺序
    return list(dict.fromkeys(ids))


# ======================
# 组装 Facebook 文案 & 发布
# ======================
def build_fb_caption(title: str, body: str, url: str) -> str:
    # 只取 body 第一段
    para = (body or "").split("\n")[0].strip()
    snippet = para[:380] if para else ""
    tags = "#菲律宾华社 #FilChiOC"
    return f"【{title}】\n{snippet}\n\n原文阅读：{url}\n{tags}".strip()

def wait_until_published(post_id: int, minutes: int) -> bool:
    """轮询 WordPress 文章是否已发布；在 minutes 内每 15 秒检查一次"""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            obj = wp_get_post(post_id)
            status = obj.get("status")
            if status == "publish":
                return True
        except Exception as e:
            print(f"[poll publish] {e}")
        time.sleep(15)
    return False

def publish_to_facebook(caption: str):
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PAGE_ID}/feed"
    r = requests.post(url, data={"message": caption, "access_token": FB_TOKEN}, timeout=60)
    print("[FB] resp:", r.text)
    r.raise_for_status()
    return r.json()


# ======================
# Sheets 读写
# ======================
def get_sheet_rows() -> List[List[str]]:
    resp = SHEET.values().get(spreadsheetId=SPREADSHEET_ID, range=f"{WORKSHEET_NAME}!A1:Z").execute()
    values = resp.get("values", [])
    return values

def update_row(row_index_1based: int, row_data: List[str]):
    """把整行写回（A:Z），row_index_1based 从 1 开始；注意我们会只更新本行已有列宽度"""
    rng = f"{WORKSHEET_NAME}!A{row_index_1based}:Z{row_index_1based}"
    body = {"values": [row_data]}
    SHEET.values().update(spreadsheetId=SPREADSHEET_ID, range=rng, valueInputOption="RAW", body=body).execute()


# ======================
# 主流程
# ======================
def main():
    if not all([WP_URL, WP_USER, WP_APP_PASSWORD, SPREADSHEET_ID, WORKSHEET_NAME, FB_PAGE_ID, FB_TOKEN]):
        raise RuntimeError("环境变量不完整，请检查 WP/Sheets/FB 相关配置。")

    rows = get_sheet_rows()
    if not rows:
        print("No data in sheet.")
        return

    header = rows[0]
    # 允许表头中文，只要匹配下面这些键即可
    # 建议表头：status, raw, title, content, categories, tags, post_id, wp_link, last_synced
    name_to_idx = {name.strip().lower(): i for i, name in enumerate(header)}

    def col(name: str) -> int:
        return name_to_idx.get(name, -1)

    idx_status     = col("status")
    idx_raw        = col("raw")
    idx_title      = col("title")
    idx_content    = col("content")
    idx_categories = col("categories")
    idx_tags       = col("tags")
    idx_post_id    = col("post_id")
    idx_wp_link    = col("wp_link")
    idx_synced     = col("last_synced")

    # 从第 2 行开始处理
    for r_i in range(1, len(rows)):
        row = rows[r_i] + [""] * (len(header) - len(rows[r_i]))  # pad

        status_val = (row[idx_status] if idx_status >= 0 and idx_status < len(row) else "").strip().lower()
        if status_val != "ready":
            continue

        raw_val        = row[idx_raw]        if idx_raw        >= 0 and idx_raw        < len(row) else ""
        title_cell     = row[idx_title]      if idx_title      >= 0 and idx_title      < len(row) else ""
        content_cell   = row[idx_content]    if idx_content    >= 0 and idx_content    < len(row) else ""
        categories_raw = row[idx_categories] if idx_categories >= 0 and idx_categories < len(row) else ""
        tags_raw       = row[idx_tags]       if idx_tags       >= 0 and idx_tags       < len(row) else ""
        post_id_cell   = row[idx_post_id]    if idx_post_id    >= 0 and idx_post_id    < len(row) else ""

        # 1) 提取标题/正文
        title, body = pick_title_and_body(raw_val, title_cell, content_cell)

        # 2) 分类/标签解析
        categories = resolve_term_ids(categories_raw, "categories")
        tags = resolve_term_ids(tags_raw, "tags")

        # 3) 发 WP（草稿）
        payload = {
            "title": title,
            "content": body,
            "status": "draft",
            "categories": categories,
            "tags": tags,
        }

        try:
            if str(post_id_cell).isdigit():
                # 更新草稿
                post_obj = wp_request("POST", f"posts/{int(post_id_cell)}", json_body=payload)
            else:
                # 新建草稿
                post_obj = wp_request("POST", "posts", json_body=payload)
        except Exception as e:
            print(f"[WP error] row {r_i+1}: {e}")
            # 写回 last_synced 以便查看错误
            if idx_synced >= 0:
                row[idx_synced] = f"WP error: {e} @ {now_str()}"
                update_row(r_i + 1, row)
            continue

        post_id = int(post_obj.get("id"))
        post_link = post_obj.get("link") or ""

        # 4) 等待你在 WP 后台把草稿“发布”为正式文章，然后再发 FB
        published = wait_until_published(post_id, FB_DELAY_MINUTES)

        # 5) 组装并发 Facebook（如果已发布）
        if published:
            fb_caption = build_fb_caption(title, body, post_link)
            try:
                publish_to_facebook(fb_caption)
                fb_note = "FB posted"
            except Exception as e:
                fb_note = f"FB error: {e}"
        else:
            fb_note = f"FB skipped: not published in {FB_DELAY_MINUTES} min"

        # 6) 写回表格
        if idx_post_id >= 0:
            row[idx_post_id] = str(post_id)
        if idx_wp_link >= 0:
            row[idx_wp_link] = post_link
        if idx_synced >= 0:
            row[idx_synced] = f"{fb_note} @ {now_str()}"
        if idx_status >= 0:
            row[idx_status] = "done"

        update_row(r_i + 1, row)
        print(f"Row {r_i+1} OK -> post_id={post_id}, {fb_note}")


if __name__ == "__main__":
    main()
