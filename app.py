#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import base64
import html
import re
from typing import Dict, List, Tuple

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# -----------------------
# 环境变量（GitHub Secrets）
# -----------------------
SHEET_ID = os.getenv("SPREADSHEET_ID")                          # 必填
SHEET_RANGE = os.getenv("WORKSHEET_NAME", "Sheet1")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

WP_BASE_URL = os.getenv("WP_BASE_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")

# 默认分类/标签（当该行未填时）
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "").strip()
DEFAULT_TAGS     = os.getenv("DEFAULT_TAGS", "").strip()

# -----------------------
# Google Sheet 列名（首行表头）
# -----------------------
COL_RAW      = "RAW"        # 一整段原文（含标题+正文）
COL_STATUS   = "status"     # ready -> 处理，处理成功写 done
COL_WP_ID    = "wp_id"      # 回写 WordPress post id
COL_CATEGORY = "category"   # 可留空，用 DEFAULT_CATEGORY
COL_TAGS     = "tags"       # 可留空，用 DEFAULT_TAGS
COL_IMAGES   = "images"     # 可选：逗号分隔图片 URL，会追加到正文尾部

# ★ 双保险手动输入列（可选，有则启用）：
COL_TITLE_MAN   = "title"    # 备用标题（当 RAW 无法拆分/为空时使用）
COL_CONTENT_MAN = "content"  # 备用正文（当 RAW 无法拆分/为空时使用）

STATUS_READY = "ready"
STATUS_DONE  = "done"

# -----------------------
# Google Sheets
# -----------------------

def get_sheets_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")
    data = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def read_sheet_as_dicts(svc, spreadsheet_id: str, sheet_name: str):
    rng = f"{sheet_name}!A1:ZZ9999"
    resp = svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng).execute()
    values = resp.get("values", []) or []
    if not values:
        return [], []
    header = [h.strip() for h in values[0]]
    rows = []
    for i, raw in enumerate(values[1:], start=2):
        row_dict = {}
        for j, h in enumerate(header):
            row_dict[h] = raw[j].strip() if j < len(raw) else ""
        row_dict["_row_index"] = i
        rows.append(row_dict)
    return header, rows

def update_sheet_row(svc, spreadsheet_id: str, sheet_name: str, row_index: int, header: List[str], new_values: Dict[str, str]):
    if not new_values:
        return
    data = []
    for col_name, val in new_values.items():
        if col_name not in header:
            continue
        col_idx = header.index(col_name)
        col_letter = col_idx_to_letter(col_idx)
        rng = f"{sheet_name}!{col_letter}{row_index}:{col_letter}{row_index}"
        data.append({"range": rng, "values": [[val]]})
    if not data:
        return
    body = {"valueInputOption": "RAW", "data": data}
    svc.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()

def col_idx_to_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s

# -----------------------
# 分段逻辑
# -----------------------

def split_from_raw(raw: str) -> Tuple[str, str]:
    """
    规则：
    1) 标题 = 第一段（第一个非空段落）
    2) 正文 = 从首个以“【华语社区”开头的段落开始；若找不到，就从第二段开始
    3) 段落 -> <p>包裹
    """
    if not raw:
        return "", ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        return "", ""
    title = blocks[0]
    focus_idx = next((i for i, b in enumerate(blocks) if b.startswith("【华语社区")), None)
    body_blocks = blocks[1:] if focus_idx is None else blocks[focus_idx:]
    body_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in body_blocks)
    return title, body_html

def append_images_to_html(html_body: str, images_csv: str) -> str:
    if not images_csv.strip():
        return html_body
    urls = [u.strip() for u in re.split(r"[,\n，；;]", images_csv) if u.strip()]
    if not urls:
        return html_body
    imgs = "\n".join([f'<p><img src="{html.escape(u)}" referrerpolicy="no-referrer" /></p>' for u in urls])
    return html_body + ("\n" if html_body else "") + imgs

# -----------------------
# WordPress
# -----------------------

def wp_auth_header(user: str, app_password: str) -> Dict[str, str]:
    token = base64.b64encode(f"{user}:{app_password}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}

def wp_ensure_term_ids(kind: str, names: List[str]) -> List[int]:
    ids = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        r = requests.get(
            f"{WP_BASE_URL}/wp-json/wp/v2/{kind}",
            params={"search": name, "per_page": 100},
            headers=wp_auth_header(WP_USER, WP_APP_PASSWORD),
            timeout=30
        )
        r.raise_for_status()
        found = None
        for it in r.json():
            if it.get("name", "").strip().lower() == name.lower():
                found = it
                break
        if found:
            ids.append(int(found["id"]))
            continue
        cr = requests.post(
            f"{WP_BASE_URL}/wp-json/wp/v2/{kind}",
            headers=wp_auth_header(WP_USER, WP_APP_PASSWORD),
            json={"name": name},
            timeout=30
        )
        if cr.status_code in (200, 201):
            ids.append(int(cr.json()["id"]))
        else:
            try:
                cr.raise_for_status()
            except Exception:
                pass
    return ids

def wp_create_post(title: str, content_html: str, categories_csv: str, tags_csv: str) -> int:
    if not title.strip():
        raise RuntimeError("Title is empty.")
    cats = [c.strip() for c in re.split(r"[,\n，/；;、]", categories_csv or DEFAULT_CATEGORY) if c.strip()]
    tgs  = [t.strip() for t in re.split(r"[,\n，/；;、]", tags_csv or DEFAULT_TAGS) if t.strip()]
    cat_ids = wp_ensure_term_ids("categories", cats) if cats else []
    tag_ids = wp_ensure_term_ids("tags", tgs) if tgs else []
    payload = {"title": title, "content": content_html, "status": "draft", "categories": cat_ids, "tags": tag_ids}
    r = requests.post(
        f"{WP_BASE_URL}/wp-json/wp/v2/posts",
        headers={**wp_auth_header(WP_USER, WP_APP_PASSWORD), "Content-Type": "application/json; charset=utf-8"},
        json=payload,
        timeout=60
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP create failed: {r.status_code} {r.text}")
    return int(r.json()["id"])

# -----------------------
# 主流程（含双保险）
# -----------------------

def normalize_status(s: str) -> str:
    return s.strip().lower()

def pick_title_body(row: Dict[str, str]) -> Tuple[str, str, str]:
    """
    返回: (title, body_html, reason)
    优先 RAW 拆分；若 RAW 为空或拆分失败，则改用手动列 title / content
    """
    raw = row.get(COL_RAW, "").strip()
    if raw:
        title, body = split_from_raw(raw)
        if title and body:
            return title, body, "from_raw"
    # 备选：手动输入
    manual_title = row.get(COL_TITLE_MAN, "").strip()
    manual_body  = row.get(COL_CONTENT_MAN, "").strip()
    if manual_title and manual_body:
        # 手动正文视为已是 HTML/文本，这里不转义，直接按段落包裹（若你已手写 HTML，可去掉 <p> 包裹）
        if not re.search(r"</\w+>", manual_body):  # 简单判断是否包含HTML标签
            body_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in re.split(r"\n\s*\n", manual_body.strip()) if p.strip())
        else:
            body_html = manual_body
        return manual_title, body_html, "from_manual"
    return "", "", "empty"

def main():
    if not (SHEET_ID and WP_BASE_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("Missing required env: SPREADSHEET_ID / WP_BASE_URL / WP_USER / WP_APP_PASSWORD")

    sheets = get_sheets_service()
    header, rows = read_sheet_as_dicts(sheets, SHEET_ID, SHEET_RANGE)
    if not header:
        print("Sheet is empty.")
        return

    # 至少需要这三列
    for c in [COL_STATUS, COL_WP_ID, COL_RAW]:
        if c not in header:
            raise RuntimeError(f"Sheet missing required column: {c}")
    # 手动列可选，无则自动忽略

    processed = 0
    for row in rows:
        if normalize_status(row.get(COL_STATUS, "")) != STATUS_READY:
            continue

        title, body_html, source = pick_title_body(row)

        if not title or not body_html:
            print(f"Row {row['_row_index']}: no usable content (RAW & manual both missing). Skip.")
            continue

        # 追加 images
        body_html = append_images_to_html(body_html, row.get(COL_IMAGES, ""))

        try:
            post_id = wp_create_post(
                title=title,
                content_html=body_html,
                categories_csv=row.get(COL_CATEGORY, ""),
                tags_csv=row.get(COL_TAGS, ""),
            )
        except Exception as e:
            print(f"Row {row['_row_index']} create WP failed: {e}")
            continue

        update_sheet_row(
            sheets, SHEET_ID, SHEET_RANGE, row["_row_index"], header,
            {COL_WP_ID: str(post_id), COL_STATUS: STATUS_DONE}
        )
        processed += 1
        print(f"Row {row['_row_index']} OK -> post_id={post_id} ({source})")

    print(f"RUN SUCCESS: processed={processed}")

if __name__ == "__main__":
    main()
