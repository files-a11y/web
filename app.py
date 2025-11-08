# -*- coding: utf-8 -*-
"""
Sheets -> WordPress (draft) -> (after publish) Facebook
- 不调用 OpenAI
- FB 文案 = 标题 + 正文中第一段（优先找以“【华语社区/華語社區”开头的段落），然后加“原文阅读：链接”与常用标签
- 仅处理 status == "ready" 的行，完成后写回 post_id / wp_link / last_synced，并将 status 改为 "done"
- 支持分类/标签【名称 或 ID】；名称不存在时可自动创建（WP_AUTO_CREATE_TERMS=true）

需要的环境变量（GitHub Secrets 里提供）：
WP_BASE_URL, WP_USER, WP_APP_PASSWORD
SPREADSHEET_ID, WORKSHEET_NAME, GOOGLE_SERVICE_ACCOUNT_JSON
FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN
可选：FB_API_VERSION(默认 v21.0), FB_DELAY_MINUTES(默认 30), WP_AUTO_CREATE_TERMS(true/false)
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

# ========== 环境变量 ==========
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

# ========== Google Sheets 客户端 ==========
if not SA_JSON:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON 未设置")

creds_info = json.loads(SA_JSON)
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
sheets_service = build("sheets", "v4", credentials=creds)
SHEET = sheets_service.spreadsheets()

# ========== 工具函数 ==========
def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def split_paras(raw: str) -> List[str]:
    if not raw:
        return []
    lines = [l.strip() for l in raw.splitlines()]
    paras, buf = [], []
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

# ========== 提取标题/正文 ==========
def pick_title_and_body(raw: str, title: str, content: str) -> Tuple[str, str]:
    """
    - 若 raw 存在且 title/content 为空：raw 第一段为标题；正文优先 raw 中第一个以“【华语社区/華語社區”开头的段落；
      若没有则用第二段；都没有则空。
    - 否则用传入的 title/content（清洗）。
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

# ========== WordPress API ==========
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
    return list(dict.fromkeys(ids))

# ========== Facebook ==========
def build_fb_caption(title: str, body: str, url: str) -> str:
    para = (body or "").split("\n")[0].strip()
    snippet = para[:380] if para else ""
    tags = "#菲律宾华社 #FilChiOC"
    return f"【{title}】\n{snippet}\n\n原文阅读：{url}\n{tags}".strip()

def wait_until_published(post_id: int, minutes: int) -> bool:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            obj = wp_get_post(post_id)
            if obj.get("status") == "publish":
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

# ========== Sheets 读写（带稳健重试） ==========
def _col_index_to_letter(idx0: int) -> str:
    """0-based -> Excel 列字母，例如 0->A, 25->Z, 26->AA"""
    s = ""
    x = idx0
    while True:
        s = chr(x % 26 + 65) + s
        x = x // 26 - 1
        if x < 0:
            break
    return s

def get_sheet_rows() -> List[List[str]]:
    resp = SHEET.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{WORKSHEET_NAME}!A1:Z"
    ).execute(num_retries=5)
    return resp.get("values", [])

def update_row(row_index_1based: int, row_data: List[str]):
    """仅写入本行实际需要的列；对网络/TLS 抖动做重试"""
    last_non_empty = 0
    for i, v in enumerate(row_data):
        if v not in (None, ""):
            last_non_empty = i
    end_col_letter = _col_index_to_letter(max(last_non_empty, 0))
    rng = f"{WORKSHEET_NAME}!A{row_index_1based}:{end_col_letter}{row_index_1based}"
    body = {"values": [row_data[:last_non_empty + 1]]}

    backoff = 1.5
    for attempt in range(6):
        try:
            SHEET.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=rng,
                valueInputOption="RAW",
                body=body
            ).execute(num_retries=5)
            return
        except Exception as e:
            msg = str(e)
            transient = any(k in msg for k in [
                "SSLEOFError",
                "Connection reset",
                "Broken pipe",
                "Remote end closed",
                "deadline exceeded",
                "Service unavailable",
                "Rate Limit",
            ])
            if attempt < 5 and transient:
                sleep_s = backoff ** attempt
                print(f"[Sheets retry {attempt+1}/6] {msg}; sleep {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue
            raise

# ========== 主流程 ==========
def main():
    if not all([WP_URL, WP_USER, WP_APP_PASSWORD, SPREADSHEET_ID, WORKSHEET_NAME, FB_PAGE_ID, FB_TOKEN]):
        raise RuntimeError("环境变量不完整，请检查 WP/Sheets/FB 相关配置。")

    rows = get_sheet_rows()
    if not rows:
        print("No data in sheet.")
        return

    header = rows[0]
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

    for r_i in range(1, len(rows)):
        row = rows[r_i] + [""] * (len(header) - len(rows[r_i]))  # pad

        status_val = (row[idx_status] if 0 <= idx_status < len(row) else "").strip().lower()
        if status_val != "ready":
            continue

        raw_val        = row[idx_raw]        if 0 <= idx_raw        < len(row) else ""
        title_cell     = row[idx_title]      if 0 <= idx_title      < len(row) else ""
        content_cell   = row[idx_content]    if 0 <= idx_content    < len(row) else ""
        categories_raw = row[idx_categories] if 0 <= idx_categories < len(row) else ""
        tags_raw       = row[idx_tags]       if 0 <= idx_tags       < len(row) else ""
        post_id_cell   = row[idx_post_id]    if 0 <= idx_post_id    < len(row) else ""

        # 1) 标题/正文
        title, body = pick_title_and_body(raw_val, title_cell, content_cell)

        # 2) 分类/标签
        categories = resolve_term_ids(categories_raw, "categories")
        tags = resolve_term_ids(tags_raw, "tags")

        # 3) 发 WP（草稿 或 更新草稿）
        payload = {
            "title": title,
            "content": body,
            "status": "draft",
            "categories": categories,
            "tags": tags,
        }

        try:
            if str(post_id_cell).isdigit():
                post_obj = wp_request("POST", f"posts/{int(post_id_cell)}", json_body=payload)
            else:
                post_obj = wp_request("POST", "posts", json_body=payload)
        except Exception as e:
            print(f"[WP error] row {r_i+1}: {e}")
            if idx_synced >= 0:
                row[idx_synced] = f"WP error: {e} @ {now_str()}"
                update_row(r_i + 1, row)
            continue

        post_id = int(post_obj.get("id"))
        post_link = post_obj.get("link") or ""

        # 4) 等你在 WP 后台把草稿“发布”为正式文章
        published = wait_until_published(post_id, FB_DELAY_MINUTES)

        # 5) 发 Facebook（仅当已发布）
        if published:
            fb_caption = build_fb_caption(title, body, post_link)
            try:
                publish_to_facebook(fb_caption)
                fb_note = "FB posted"
            except Exception as e:
                fb_note = f"FB error: {e}"
        else:
            fb_note = f"FB skipped: not published in {FB_DELAY_MINUTES} min"

        # 6) 回写表格
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
