#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import base64
import html
import re
import time
from typing import Dict, List, Tuple, Any, Optional

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# -----------------------
# 环境变量（GitHub Secrets）
# -----------------------
SHEET_ID = os.getenv("SPREADSHEET_ID")                          # 必填
SHEET_RANGE = os.getenv("WORKSHEET_NAME", "Sheet1")             # 只写工作表名即可
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

WP_BASE_URL = os.getenv("WP_BASE_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")

# 可选：Category & Tags 的默认值（当行里没填时）
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "").strip()     # 例：Philippines
DEFAULT_TAGS = os.getenv("DEFAULT_TAGS", "").strip()             # 例：菲律宾、菲律宾新闻

# 列名约定（首行表头）
COL_RAW      = "RAW"            # 一整段原文（含标题 + 正文）
COL_STATUS   = "status"         # ready 才会处理；处理后写 done
COL_WP_ID    = "wp_id"          # 成功后写入 post id
COL_CATEGORY = "category"       # 可填中文名或英文名，多个用逗号
COL_TAGS     = "tags"           # 多个用顿号/中文逗号/英文逗号
COL_IMAGES   = "images"         # 可选：逗号分隔的图片 URL，会追加到正文尾部

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

def read_sheet_as_dicts(svc, spreadsheet_id: str, sheet_name: str) -> Tuple[List[str], List[Dict[str, str]]]:
    rng = f"{sheet_name}!A1:ZZ9999"
    resp = svc.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng).execute()
    values = resp.get("values", [])
    if not values:
        return [], []
    header = [h.strip() for h in values[0]]
    rows = []
    for i, raw in enumerate(values[1:], start=2):  # Excel 行号
        row_dict = {}
        for j, h in enumerate(header):
            row_dict[h] = raw[j].strip() if j < len(raw) else ""
        row_dict["_row_index"] = i  # 记住行号，回写用
        rows.append(row_dict)
    return header, rows

def update_sheet_row(svc, spreadsheet_id: str, sheet_name: str, row_index: int, header: List[str], new_values: Dict[str, str]):
    """按列名定点更新这一行的多个单元格"""
    if not new_values:
        return
    # 找出要更新的列
    data = []
    for col_name, val in new_values.items():
        if col_name not in header:
            continue
        col_idx = header.index(col_name)  # 0-based
        col_letter = col_idx_to_letter(col_idx)
        rng = f"{sheet_name}!{col_letter}{row_index}:{col_letter}{row_index}"
        data.append({
            "range": rng,
            "values": [[val]],
        })
    if not data:
        return
    body = {"valueInputOption": "RAW", "data": data}
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=body
    ).execute()

def col_idx_to_letter(idx: int) -> str:
    """0->A, 25->Z, 26->AA"""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s

# -----------------------
# 分段逻辑：从 RAW 里拆标题与正文
# -----------------------

def split_from_raw(raw: str) -> Tuple[str, str]:
    """
    规则：
    1) 标题 = 第一段（第一个非空段落）
    2) 正文 = 从首个以“【华语社区”开头的段落开始到结尾
       - 如果找不到“【华语社区”，则从第二段开始
    3) 段落 -> <p>包裹
    """
    if not raw:
        return "", ""

    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    # 按“空行”或“换行”分段：既兼容空行，也兼容单换行
    # 先按照双换行切，再把单行残余合并
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        return "", ""

    # 标题：第一段
    title = blocks[0]

    # 找到从“【华语社区”开头的段
    focus_idx = None
    for i, b in enumerate(blocks):
        if b.startswith("【华语社区"):
            focus_idx = i
            break

    if focus_idx is None:
        # 没找到，则正文 = 第二段开始（如果只有一段，就为空）
        body_blocks = blocks[1:] if len(blocks) > 1 else []
    else:
        body_blocks = blocks[focus_idx:]

    # 转 HTML 段落
    body_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in body_blocks)
    return title, body_html

def append_images_to_html(html_body: str, images_csv: str) -> str:
    """可选：把 images 列的 URL 附到正文尾部"""
    if not images_csv.strip():
        return html_body
    urls = [u.strip() for u in re.split(r"[,\n，；;]", images_csv) if u.strip()]
    if not urls:
        return html_body
    imgs = "\n".join([f'<p><img src="{html.escape(u)}" referrerpolicy="no-referrer" /></p>' for u in urls])
    return html_body + "\n" + imgs

# -----------------------
# WordPress
# -----------------------

def wp_auth_header(user: str, app_password: str) -> Dict[str, str]:
    token = base64.b64encode(f"{user}:{app_password}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}

def wp_ensure_term_ids(kind: str, names: List[str]) -> List[int]:
    """
    kind: 'categories' | 'tags'
    names: 通过 name 精确匹配；找不到就尝试创建（需要 WP 允许创建分类/标签）
    """
    ids = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        # 查
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
        # 创建（若无权限会 401/403，届时让它抛错或忽略）
        cr = requests.post(
            f"{WP_BASE_URL}/wp-json/wp/v2/{kind}",
            headers=wp_auth_header(WP_USER, WP_APP_PASSWORD),
            json={"name": name},
            timeout=30
        )
        if cr.status_code in (200, 201):
            ids.append(int(cr.json()["id"]))
        else:
            # 创建失败就跳过，不阻塞整篇文章
            try:
                cr.raise_for_status()
            except Exception:
                pass
    return ids

def wp_create_or_update_post(title: str, content_html: str, categories_csv: str, tags_csv: str) -> int:
    if not title.strip():
        raise RuntimeError("Title is empty.")

    # 处理分类/标签
    cats = [c.strip() for c in re.split(r"[,\n，/；;、]", categories_csv or DEFAULT_CATEGORY) if c.strip()]
    tgs  = [t.strip() for t in re.split(r"[,\n，/；;、]", tags_csv or DEFAULT_TAGS) if t.strip()]

    cat_ids = wp_ensure_term_ids("categories", cats) if cats else []
    tag_ids = wp_ensure_term_ids("tags", tgs) if tgs else []

    payload = {
        "title": title,
        "content": content_html,
        "status": "draft",         # 保持草稿，由你手动发布
        "categories": cat_ids,
        "tags": tag_ids,
    }
    r = requests.post(
        f"{WP_BASE_URL}/wp-json/wp/v2/posts",
        headers={
            **wp_auth_header(WP_USER, WP_APP_PASSWORD),
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=60
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP create failed: {r.status_code} {r.text}")
    return int(r.json()["id"])

# -----------------------
# 主流程
# -----------------------

def normalize_status(s: str) -> str:
    return s.strip().lower()

def main():
    if not (SHEET_ID and WP_BASE_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("Missing required env: SPREADSHEET_ID / WP_BASE_URL / WP_USER / WP_APP_PASSWORD")

    sheets = get_sheets_service()
    header, rows = read_sheet_as_dicts(sheets, SHEET_ID, SHEET_RANGE)
    if not header:
        print("Sheet is empty.")
        return

    required_cols = [COL_RAW, COL_STATUS, COL_WP_ID]
    for c in required_cols:
        if c not in header:
            raise RuntimeError(f"Sheet missing required column: {c}")

    processed = 0
    for row in rows:
        status = normalize_status(row.get(COL_STATUS, ""))
        if status != STATUS_READY:
            continue
        raw_text = row.get(COL_RAW, "").strip()
        if not raw_text:
            print(f"Row {row['_row_index']}: RAW empty, skip.")
            continue

        # 1) 拆分：标题 + 正文（从“【华语社区”开始）
        title, body_html = split_from_raw(raw_text)

        # 2) 追加 images（可选）
        images_csv = row.get(COL_IMAGES, "")
        body_html = append_images_to_html(body_html, images_csv)

        # 3) 发到 WP
        try:
            post_id = wp_create_or_update_post(
                title=title,
                content_html=body_html,
                categories_csv=row.get(COL_CATEGORY, ""),
                tags_csv=row.get(COL_TAGS, ""),
            )
        except Exception as e:
            print(f"Row {row['_row_index']} create WP failed: {e}")
            continue

        # 4) 回写：wp_id & status=done
        update_sheet_row(
            sheets, SHEET_ID, SHEET_RANGE, row["_row_index"], header,
            {COL_WP_ID: str(post_id), COL_STATUS: STATUS_DONE}
        )
        processed += 1
        print(f"Row {row['_row_index']} OK -> post_id={post_id}")

    print(f"RUN SUCCESS: processed={processed}")

if __name__ == "__main__":
    main()
