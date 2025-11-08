#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FilChiOC 一体化发布脚本（单文件版）
--------------------------------
功能：
1) 读取 Google Sheets（支持列：status/raw/title/content/categories/tags/post_id/wp_link/last_synced/fb_header/fb_caption_short）
2) 若 raw 有内容：自动按“首段=标题；以【华语社区PH 开头为内文”的规则分离 title/content
3) 依 sheets 的 status 流转：
   - ready  ->（可设为 publish 或 draft）发到 WP，回写 post_id、wp_link、last_synced、status=done_wp
   - done   ->（表示你已在 WP 审核并发布好）延迟 30 分钟后发 Facebook，并回写 last_synced、status=done_all
4) 调用 OpenAI（REST）生成 Facebook caption（简体中文+hashtags），并合成三行格式：
   第一行：fb_header + 标题
   第二行：精简内容
   第三行：原文阅读：wp_link
5) 可选：向 Lark webhook 报告流程结果

必须的环境变量（GitHub Secrets 或本地）：
- GOOGLE_SERVICE_ACCOUNT_JSON  Google 服务账号 JSON（整段）
- SPREADSHEET_ID               表格ID
- WORKSHEET_NAME               工作表名
- WP_BASE_URL                  WordPress 站点，如 https://filchioc.com
- WP_USER                      WP 用户（建议专用机器人账号）
- WP_APP_PASSWORD              WP 应用密码
- OPENAI_API_KEY               OpenAI API Key
- OPENAI_MODEL                 （可选，默认 gpt-4o-mini）
- FB_PAGE_ID                   Facebook Page ID
- FB_PAGE_ACCESS_TOKEN         Facebook Page 长效 Token
- FB_API_VERSION               （可选，默认 v19.0）
- LARK_WEBHOOK_URL             （可选）Lark 群机器人 webhook

依赖：
pip install google-api-python-client google-auth google-auth-httplib2 requests beautifulsoup4
"""

import os
import json
import time
import datetime as dt
from typing import Dict, List, Any, Tuple

import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ---------------------
# 环境变量
# ---------------------
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

WP_BASE_URL = os.getenv("WP_BASE_URL")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")
FB_API_VERSION = os.getenv("FB_API_VERSION", "v19.0")

LARK_WEBHOOK_URL = os.getenv("LARK_WEBHOOK_URL")

# 发布控制
WP_DEFAULT_STATUS = os.getenv("WP_DEFAULT_STATUS", "draft")  # 可改为 "publish"
def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

FB_DELAY_MINUTES = env_int("FB_DELAY_MINUTES", 30)  # Facebook 延迟发布分钟数



# ---------------------
# 工具
# ---------------------
def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def notify_lark(text: str):
    if not LARK_WEBHOOK_URL:
        return
    try:
        requests.post(LARK_WEBHOOK_URL, json={"msg_type": "text", "content": {"text": text}}, timeout=15)
    except Exception:
        pass


def ensure_env():
    missing = []
    for k in ["SPREADSHEET_ID", "WORKSHEET_NAME", "GOOGLE_SERVICE_ACCOUNT_JSON",
              "WP_BASE_URL", "WP_USER", "WP_APP_PASSWORD",
              "OPENAI_API_KEY",
              "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"]:
        if not globals().get(k):
            missing.append(k)
    if missing:
        raise RuntimeError(f"缺少必要环境变量：{', '.join(missing)}")


# ---------------------
# Google Sheets
# ---------------------
def get_sheet_service():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds).spreadsheets()


def read_rows() -> Tuple[List[str], List[List[str]]]:
    sheet = get_sheet_service()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{WORKSHEET_NAME}!A:Z"
    ).execute()
    values = result.get("values", [])
    if not values:
        return [], []
    header = values[0]
    rows = values[1:]
    return header, rows


def write_cells(range_a1: str, values: List[List[Any]]):
    sheet = get_sheet_service()
    sheet.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_a1,
        valueInputOption="RAW",
        body={"values": values}
    ).execute()


# ---------------------
# 数据处理
# ---------------------
def header_index_map(header: List[str]) -> Dict[str, int]:
    # 标题统一小写匹配
    return {name.strip().lower(): idx for idx, name in enumerate(header)}


def get(row: List[str], header_map: Dict[str, int], key: str, default="") -> str:
    idx = header_map.get(key)
    if idx is None or idx >= len(row):
        return default
    return row[idx].strip()


def split_raw_if_needed(raw: str) -> Tuple[str, str]:
    """把 raw 分拆为 title/content：
    - title 取 raw 第一段（遇到首个空行或换行）
    - content 从 '【华语社区PH' 开头至末尾；若未找到，则取 title 之后的剩余文本
    """
    if not raw:
        return "", ""

    # 标准化换行
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in text.split("\n")]

    # 首段为标题（直到遇到空行或下一段）
    title = ""
    body_lines = []
    for i, p in enumerate(parts):
        if i == 0:
            title = p
            continue
        body_lines.append(p)
    body = "\n".join(body_lines).strip()

    # 优先从【华语社区PH 开始截取
    anchor = "【华语社区PH"
    if anchor in text:
        body = text[text.find(anchor):].strip()

    return title, body


def html_strip_sample(html: str, limit=220) -> str:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        s = soup.get_text(" ").strip()
        return (s[:limit]).strip()
    except Exception:
        return (html or "")[:limit]


# ---------------------
# WordPress
# ---------------------
def wp_create_or_update(title: str, content: str, status: str = WP_DEFAULT_STATUS,
                        categories: List[int] = None, tags: List[int] = None,
                        post_id: str = "") -> Dict[str, Any]:
    base = f"{WP_BASE_URL}/wp-json/wp/v2/posts"
    data = {
        "title": title,
        "content": content,
        "status": status
    }
    if categories:
        data["categories"] = categories
    if tags:
        data["tags"] = tags

    if post_id:
        url = f"{base}/{post_id}"
        r = requests.post(url, json=data, auth=(WP_USER, WP_APP_PASSWORD), timeout=60)
    else:
        url = base
        r = requests.post(url, json=data, auth=(WP_USER, WP_APP_PASSWORD), timeout=60)
    r.raise_for_status()
    return r.json()


# ---------------------
# OpenAI（REST）
# ---------------------
def openai_chat(messages: List[Dict[str, str]], model: str = OPENAI_MODEL,
                temperature: float = 0.3, max_tokens: int = 400) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def gen_fb_short(title: str, content: str) -> str:
    prompt = f"""
你是菲律宾华文媒体编辑。请将以下新闻内容浓缩为适合 Facebook 的短文：
- 必须是简体中文，2~3行正文，通俗易懂、客观中性
- 再输出 5-10 个相关 Hashtags，统一放在最后一行，#与词之间不加空格
- 不要加emoji，不要加入网址

标题：{title}
正文原文：
{content}

输出格式：
Caption:
Hashtags:
""".strip()
    return openai_chat([{"role": "user", "content": prompt}])


# ---------------------
# Facebook
# ---------------------
def build_fb_caption(header_line: str, title: str, short_body: str, wp_link: str) -> str:
    short_body = (short_body or "").strip()
    return f"{header_line}{title}\n{short_body}\n原文阅读：{wp_link}"[:1800]


def fb_post_after_delay(page_id: str, token: str, message: str, link: str,
                        delay_minutes: int = FB_DELAY_MINUTES) -> Dict[str, Any]:
    if delay_minutes > 0:
        time.sleep(delay_minutes * 60)
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/feed"
    data = {
        "message": message,
        "link": link,
        "access_token": token
    }
    r = requests.post(url, data=data, timeout=60)
    r.raise_for_status()
    return r.json()


# ---------------------
# 主流程
# ---------------------
def process():
    ensure_env()
    header, rows = read_rows()
    if not header:
        print("表格为空。")
        return

    h = header_index_map(header)

    # 为兼容：没有某些列也不报错
    # 期望列（不区分大小写）：status, raw, title, content, categories, tags, post_id, wp_link,
    # last_synced, fb_header, fb_caption_short
    updates = []  # (row_idx, dict_of_updates)

    for i, row in enumerate(rows, start=2):  # 从第2行起（A2）
        status = get(row, h, "status").lower()

        raw = get(row, h, "raw")
        title = get(row, h, "title")
        content = get(row, h, "content")
        categories_raw = get(row, h, "categories")
        tags_raw = get(row, h, "tags")
        post_id = get(row, h, "post_id")
        wp_link = get(row, h, "wp_link")
        fb_header = get(row, h, "fb_header") or "【华语社区PH】"
        fb_caption_short = get(row, h, "fb_caption_short")

        # 1) 如果 raw 有内容，分拆
        if raw and (not title or not content):
            t, c = split_raw_if_needed(raw)
            title = title or t
            content = content or c

        # 2) 若缺少 fb_caption_short，尝试从正文摘取或用 GPT 生成
        if not fb_caption_short:
            fallback = html_strip_sample(content, 220)
            try:
                fb_caption_short = gen_fb_short(title, fallback or content)
            except Exception as e:
                fb_caption_short = fallback

        # 3) 解析分类与标签（数字ID或逗号分隔的数字）
        def parse_int_list(s: str) -> List[int]:
            out = []
            for x in (s or "").replace("，", ",").split(","):
                x = x.strip()
                if x.isdigit():
                    out.append(int(x))
            return out

        categories = parse_int_list(categories_raw)
        tags = parse_int_list(tags_raw)

        # 4) 根据状态流转
        # 状态：ready -> 发WP（按配置 draft/publish），回填 post_id/wp_link/status=done_wp
        #      done  ->（你人工在WP发布完并带好封面图）延迟30分钟发FB，status=done_all
        row_updates = {}

        try:
            if status == "ready":
                if not title or not content:
                    print(f"[第{i}行] 缺少标题或内容，跳过。")
                else:
                    resp = wp_create_or_update(title, content, WP_DEFAULT_STATUS, categories, tags, post_id)
                    new_id = str(resp.get("id") or "")
                    link = resp.get("link") or ""

                    row_updates["post_id"] = new_id
                    row_updates["wp_link"] = link
                    row_updates["last_synced"] = now_iso()
                    row_updates["status"] = "done_wp"

                    print(f"[第{i}行] WP已创建/更新：id={new_id} link={link}")

            elif status == "done":
                # 你已在WP后台人工发布好，并且 featured image/OG都准备妥当
                if not wp_link:
                    print(f"[第{i}行] 无 wp_link，跳过发FB。")
                else:
                    caption = build_fb_caption(fb_header, title, fb_caption_short, wp_link)
                    fb_resp = fb_post_after_delay(FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN, caption, wp_link, FB_DELAY_MINUTES)
                    row_updates["last_synced"] = now_iso()
                    row_updates["status"] = "done_all"
                    print(f"[第{i}行] Facebook 已发帖：{fb_resp}")

            else:
                # 其它状态不处理
                pass

        except Exception as e:
            row_updates["last_synced"] = f"ERROR: {str(e)}"
            notify_lark(f"❌ 第{i}行处理失败：{title or '(无标题)'}\n{e}")

        if row_updates:
            updates.append((i, row_updates))

    # 5) 批量回写（逐行写，简单稳妥）
    if updates:
        # 将列名→列号
        def col_letter(idx0: int) -> str:
            # 0-based -> A1 列名
            n = idx0 + 1
            s = ""
            while n:
                n, r = divmod(n - 1, 26)
                s = chr(65 + r) + s
            return s

        for row_idx, fields in updates:
            for key, val in fields.items():
                if key.lower() not in h:
                    continue
                col_idx = h[key.lower()]
                a1 = f"{WORKSHEET_NAME}!{col_letter(col_idx)}{row_idx}"
                write_cells(a1, [[val]])

    print("✅ 处理完成。")


if __name__ == "__main__":
    try:
        process()
        notify_lark("✅ FilChiOC 一体化发布：本轮处理完成")
    except Exception as e:
        notify_lark(f"❌ FilChiOC 发布任务失败：{e}")
        raise
