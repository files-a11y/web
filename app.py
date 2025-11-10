#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sheets ➜ WordPress 发布器（无 ChatGPT 版）
- 解析 RAW：标题 = 第一段；正文 = 以“【华语社区”起的段落，如果找不到则用首段之外的所有段落
- 双保险：RAW 拆不出时，使用手填 TITLE/CONTENT 列
- WP 分类/标签可用“名称”，脚本会自动按名称查找或创建，并提交 post 草稿
- 回写：STATUS=done、WP_POST_ID、EXPORTED_TITLE、EXPORTED_FIRST_P（正文第一段）

表头建议（不区分大小写）：
  STATUS, RAW, TITLE, CONTENT, CATEGORY, TAGS, WP_POST_ID,
  EXPORTED_TITLE, EXPORTED_FIRST_P, ERROR
"""

import os
import json
import time
import html
import requests
from typing import Dict, List, Tuple, Optional

# ------------------------
# 读取环境变量
# ------------------------
SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID", "").strip()
WORKSHEET_NAME   = os.getenv("WORKSHEET_NAME", "").strip()
GOOGLE_SA_JSON   = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

WP_BASE_URL      = os.getenv("WP_BASE_URL", "").rstrip("/")
WP_USER          = os.getenv("WP_USER", "").strip()
WP_APP_PASSWORD  = os.getenv("WP_APP_PASSWORD", "").strip()

# 可选默认项（当表格该行为空时使用）
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "").strip()  # 可以写名称，如 “Philippines”
DEFAULT_TAGS     = os.getenv("DEFAULT_TAGS", "").strip()      # 逗号分隔，如 “菲律宾、菲律宾新闻”

# 网络超时
HTTP_TIMEOUT = (15, 60)  # (连接, 读)


# ------------------------
# Google Sheets 客户端
# ------------------------
def _build_sheets():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not (SPREADSHEET_ID and WORKSHEET_NAME and GOOGLE_SA_JSON):
        raise RuntimeError("缺少 Google Sheets 相关环境变量：SPREADSHEET_ID / WORKSHEET_NAME / GOOGLE_SERVICE_ACCOUNT_JSON")

    try:
        sa_info = json.loads(GOOGLE_SA_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON 不是合法 JSON") from e

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
    service = build("sheets", "v4", credentials=creds)
    return service


def read_sheet(service) -> Tuple[List[str], List[Dict[str, str]]]:
    rng = f"{WORKSHEET_NAME}!A:ZZ"
    resp = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=rng).execute()
    values = resp.get("values", [])
    if not values:
        return [], []

    header = [h.strip() for h in values[0]]
    rows: List[Dict[str, str]] = []
    for i, raw_row in enumerate(values[1:], start=2):  # i=真实行号
        row_dict = {}
        for j, h in enumerate(header):
            val = raw_row[j] if j < len(raw_row) else ""
            row_dict[h] = val
        row_dict["_row_index"] = i   # 保存真实行号
        rows.append(row_dict)
    return header, rows


def _col_letter(idx: int) -> str:
    """0-based index -> Excel 列字母"""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def batch_update_row(service, header: List[str], row_index: int, updates: Dict[str, str]):
    """按字段名更新某一行的多个列（一次 batch）"""
    # 头部转小写匹配
    lower_map = {name.lower(): i for i, name in enumerate(header)}
    data = []
    for k, v in updates.items():
        if k is None:
            continue
        key = k.lower()
        if key not in lower_map:
            # 不存在该列就跳过（或可考虑自动扩展列）
            continue
        col_idx = lower_map[key]
        col_letter = _col_letter(col_idx)
        rng = f"{WORKSHEET_NAME}!{col_letter}{row_index}"
        data.append({
            "range": rng,
            "values": [[str(v) if v is not None else ""]]
        })
    if not data:
        return
    body = {"valueInputOption": "RAW", "data": data}
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body=body
    ).execute()


# ------------------------
# 解析 RAW / 双保险
# ------------------------
def split_from_raw(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """
    从 RAW 文本中拆出 title 和 body：
    - 标题 = 第一段（第一个非空行）
    - 正文 = 从“【华语社区”开头的段落起一直到结尾；
            若未找到“【华语社区”，则用除第一段外的其余段落
    """
    if not raw:
        return None, None

    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    if not paras:
        return None, None

    title = paras[0]

    start_idx = None
    for i, p in enumerate(paras):
        if p.startswith("【华语社区"):
            start_idx = i
            break

    if start_idx is not None:
        body = "\n\n".join(paras[start_idx:])
    else:
        body = "\n\n".join(paras[1:]) if len(paras) > 1 else ""

    return title, body


def first_paragraph(body: str) -> str:
    if not body:
        return ""
    parts = [p.strip() for p in body.replace("\r\n", "\n").replace("\r", "\n").split("\n") if p.strip()]
    return parts[0] if parts else ""


def pick_title_body(row: Dict[str, str]) -> Tuple[str, str, str]:
    """
    优先从 RAW 拆分；RAW 无法得到时，回退到手填列（Title/Content）
    返回：title, body_html, exported_first_p
    """
    # 不区分大小写取列
    def g(*names):
        for n in names:
            if n in row and str(row[n]).strip():
                return str(row[n]).strip()
            # 兼容大小写
            for k in row.keys():
                if k.lower() == n.lower() and str(row[k]).strip():
                    return str(row[k]).strip()
        return ""

    raw = g("RAW", "Raw")
    parsed_title, parsed_body = split_from_raw(raw) if raw else (None, None)

    manual_title = g("TITLE", "Title")
    manual_body  = g("CONTENT", "Body", "Content")

    title = (parsed_title or manual_title or "").strip()
    body  = (parsed_body  or manual_body  or "").strip()

    if not title or not body:
        raise ValueError("无法从 RAW 或手填列获得有效的标题/正文")

    # 构造 HTML（保持换行为 <p> 段落）
    paras = [html.escape(p).replace("  ", "&nbsp; ") for p in body.replace("\r\n", "\n").replace("\r", "\n").split("\n") if p.strip()]
    body_html = "".join([f"<p>{p}</p>\n" for p in paras])
    return title, body_html, first_paragraph(body)


# ------------------------
# WordPress 工具
# ------------------------
def wp_session():
    if not (WP_BASE_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("缺少 WordPress 相关环境变量：WP_BASE_URL / WP_USER / WP_APP_PASSWORD")
    s = requests.Session()
    s.auth = (WP_USER, WP_APP_PASSWORD)
    s.headers.update({"Content-Type": "application/json; charset=utf-8"})
    return s


def wp_get_or_create_term(session: requests.Session, taxonomy: str, name: str) -> Optional[int]:
    """按名称获取（不存在则创建）分类/标签 ID。taxonomy 取 'categories' 或 'tags'。"""
    if not name:
        return None
    api = f"{WP_BASE_URL}/wp-json/wp/v2/{taxonomy}"
    # 先查
    r = session.get(api, params={"search": name, "per_page": 100}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    found = [t for t in r.json() if str(t.get("name", "")).lower() == name.lower()]
    if found:
        return int(found[0]["id"])
    # 创建
    r = session.post(api, json={"name": name}, timeout=HTTP_TIMEOUT)
    if r.status_code in (200, 201):
        return int(r.json().get("id"))
    # 可能因为重名或权限导致失败，降级再查一次
    r2 = session.get(api, params={"search": name, "per_page": 100}, timeout=HTTP_TIMEOUT)
    if r2.ok:
        again = [t for t in r2.json() if str(t.get("name", "")).lower() == name.lower()]
        if again:
            return int(again[0]["id"])
    return None


def create_wp_post(title: str, body_html: str, categories: List[str], tags: List[str]) -> int:
    s = wp_session()

    # 分类/标签转 ID
    cat_ids: List[int] = []
    tag_ids: List[int] = []

    for cname in categories:
        cid = wp_get_or_create_term(s, "categories", cname.strip())
        if cid:
            cat_ids.append(cid)

    for tname in tags:
        tid = wp_get_or_create_term(s, "tags", tname.strip())
        if tid:
            tag_ids.append(tid)

    payload = {
        "title": title,
        "content": body_html,
        "status": "draft",  # 草稿
    }
    if cat_ids:
        payload["categories"] = cat_ids
    if tag_ids:
        payload["tags"] = tag_ids

    api = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    r = s.post(api, json=payload, timeout=HTTP_TIMEOUT)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"WP 创建文章失败：{r.status_code} {r.text}")

    return int(r.json().get("id"))


# ------------------------
# 主流程
# ------------------------
def norm(value: str) -> str:
    return (value or "").strip()


def get_list_from_cell(value: str) -> List[str]:
    """把 'a,b；c、d' 这类多分隔符字符串切成列表"""
    s = norm(value)
    for sep in ["；", "、", "，", ";"]:
        s = s.replace(sep, ",")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts


def main():
    print("🚀 Start Google Sheets ➜ WordPress")
    service = _build_sheets()
    header, rows = read_sheet(service)
    if not header:
        print("Sheet 为空，退出")
        return

    # 头部转小写映射，便于读取列
    header_lower = [h.lower() for h in header]

    total = 0
    success = 0
    skipped = 0

    for row in rows:
        total += 1
        i = int(row["_row_index"])  # 真实行号

        # 读 STATUS（大小写不敏感）
        status = ""
        for k, v in row.items():
            if k.lower() == "status":
                status = str(v).strip().lower()
        if status != "ready":
            skipped += 1
            continue

        # 已经有 WP_POST_ID 就跳过
        wp_post_id = ""
        for k, v in row.items():
            if k.lower() == "wp_post_id":
                wp_post_id = str(v).strip()
        if wp_post_id:
            skipped += 1
            continue

        # 分类、标签（行内优先，其次用默认）
        raw_cat = ""
        raw_tags = ""
        for k, v in row.items():
            if k.lower() == "category":
                raw_cat = str(v)
            if k.lower() == "tags":
                raw_tags = str(v)
        categories = get_list_from_cell(raw_cat) or ([DEFAULT_CATEGORY] if DEFAULT_CATEGORY else [])
        tags = get_list_from_cell(raw_tags) or get_list_from_cell(DEFAULT_TAGS)

        try:
            # 1) 解析标题与正文
            title, body_html, fb_first_p = pick_title_body(row)

            # 2) 创建 WP 草稿
            post_id = create_wp_post(title, body_html, categories, tags)

            # 3) 回写表格
            updates = {
                "WP_POST_ID": post_id,
                "STATUS": "done",
                "EXPORTED_TITLE": title,
                "EXPORTED_FIRST_P": fb_first_p,
                "ERROR": "",
            }
            batch_update_row(service, header, i, updates)
            success += 1

            print(f"Row {i} OK ➜ post_id={post_id}")

            # 轻微节流
            time.sleep(0.5)

        except Exception as e:
            err = str(e)
            updates = {"ERROR": err}
            batch_update_row(service, header, i, updates)
            print(f"Row {i} FAILED: {err}")

    print(f"✅ DONE. total={total}, success={success}, skipped={skipped}")


if __name__ == "__main__":
    main()
