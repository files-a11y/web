import os, json, time, base64, datetime
import requests, pytz, gspread
from google.oauth2.service_account import Credentials

# ========= ENV from GitHub Secrets ==========
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Posts")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

WP_BASE_URL = os.environ["WP_BASE_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
DEFAULT_AUTHOR_FALLBACK = int(os.environ.get("DEFAULT_AUTHOR_FALLBACK", "1"))

LARK_WEBHOOK_URL = os.environ["LARK_WEBHOOK_URL"]

TZ = pytz.timezone("Asia/Manila")

# ========= Google Sheets Auth ==========
creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds = Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gsheets = gspread.authorize(creds)
ws = gsheets.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)


# ========= Helpers ==========
def now_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S%z")

def to_list(v):
    if not v:
        return []
    return [x.strip() for x in str(v).split(",") if x.strip()]

def b64_auth(user, app_password):
    return "Basic " + base64.b64encode(f"{user}:{app_password}".encode()).decode()

AUTH_HEADER = {
    "Authorization": b64_auth(WP_USER, WP_APP_PASSWORD),
    "Content-Type": "application/json"
}


# ========= WordPress REST API ==========
def wp_get(path, params=None):
    url = f"{WP_BASE_URL}/wp-json/wp/v2/{path}"
    r = requests.get(url, headers={"Authorization": AUTH_HEADER["Authorization"]}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def wp_post(path, payload, files=None, headers=None):
    url = f"{WP_BASE_URL}/wp-json/wp/v2/{path}"
    hdrs = headers or AUTH_HEADER

    if files:
        # upload media
        r = requests.post(url, headers={"Authorization": AUTH_HEADER["Authorization"], **(headers or {})},
                          files=files, data=payload, timeout=120)
    else:
        r = requests.post(url, headers=hdrs, data=json.dumps(payload), timeout=60)

    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text}")

    return r.json()


def ensure_term_ids(taxonomy, names):
    ids = []
    for name in names:
        if not name:
            continue

        found = wp_get(f"{taxonomy}", params={"search": name})
        matched = next((t for t in found if t.get("name", "").lower() == name.lower()), None)

        if matched:
            ids.append(matched["id"])
        else:
            created = wp_post(taxonomy, {"name": name})
            ids.append(created["id"])
    return ids


def find_author_id(name):
    if not name:
        return DEFAULT_AUTHOR_FALLBACK

    try:
        res = wp_get("users", params={"search": name})
        if res:
            for u in res:
                if u.get("name", "").lower() == name.lower() or u.get("slug", "").lower() == name.lower():
                    return u["id"]
            return res[0]["id"]
    except Exception:
        pass

    return DEFAULT_AUTHOR_FALLBACK


def upload_featured_media(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()

    filename = url.split("/")[-1].split("?")[0] or f"image-{int(time.time())}.jpg"
    files = {"file": (filename, r.content, r.headers.get("Content-Type", "image/jpeg"))}
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Authorization": AUTH_HEADER["Authorization"]
    }

    return wp_post("media", payload={}, files=files, headers=headers)["id"]


def get_post_by_slug(slug):
    res = wp_get("posts", params={"slug": slug, "status": "any"})
    return res[0] if isinstance(res, list) and res else None


def create_or_update_post(row):
    title = row.get("title", "").strip()
    content = row.get("content", "")
    categories = to_list(row.get("categories", ""))
    tags = to_list(row.get("tags", ""))
    slug = str(row.get("slug", "")).strip()
    author_name = row.get("author", "")
    date = row.get("date", "").strip()
    featured_image_url = row.get("featured_image_url", "").strip()
    post_id = str(row.get("post_id", "")).strip()

    if not title or not content:
        raise ValueError("title / content required")

    category_ids = ensure_term_ids("categories", categories)
    tag_ids = ensure_term_ids("tags", tags)
    author_id = find_author_id(author_name)

    payload = {
        "title": title,
        "content": content,
        "status": "draft",
        "categories": category_ids,
        "tags": tag_ids,
        "author": author_id,
    }

    if slug:
        payload["slug"] = slug
    if date:
        payload["date"] = date

    if featured_image_url:
        try:
            media_id = upload_featured_media(featured_image_url)
            payload["featured_media"] = media_id
        except Exception as e:
            print(f"[warn] featured_image upload failed: {e}")

    if post_id.isdigit():
        res = wp_post(f"posts/{post_id}", payload)
        return res["id"], res["link"]

    if slug:
        existing = get_post_by_slug(slug)
        if existing:
            res = wp_post(f"posts/{existing['id']}", payload)
            return res["id"], res["link"]

    res = wp_post("posts", payload)
    return res["id"], res["link"]


# ========= Lark webhook ==========
def send_lark(summary_text, items=None):
    msg = summary_text
    if items:
        msg += "\n" + "\n".join([f"• {x}" for x in items])

    payload = {"msg_type": "text", "content": {"text": msg}}
    r = requests.post(LARK_WEBHOOK_URL, json=payload, timeout=15)

    if r.status_code >= 300:
        print(f"[warn] lark send failed: {r.status_code} {r.text}")


# ========= Main job ==========
def main():
    rows = ws.get_all_records()
    created, updated, skipped, errors = 0, 0, 0, 0
    sent_titles = []

    for idx, row in enumerate(rows, start=2):  # row index starts at 2 (row 1 is header)
        status = str(row.get("status", "")).lower()
        if status not in ("ready",):
            skipped += 1
            continue

        try:
            pid, link = create_or_update_post(row)

            ws.update_cell(idx, _col("post_id"), pid)
            ws.update_cell(idx, _col("wp_link"), link)
            ws.update_cell(idx, _col("last_synced"), now_str())
            ws.update_cell(idx, _col("status"), "done")

            created += 1 if not row.get("post_id") else 0
            updated += 1 if row.get("post_id") else 0

            sent_titles.append(row.get("title"))

        except Exception as e:
            errors += 1
            print(f"[error] row {idx}: {e}")

    summary = f"【Sheets→WP】完成：新建 {created}，更新 {updated}，跳过 {skipped}，错误 {errors}"
    send_lark(summary, sent_titles[:20])


def _col(name):
    header = ws.row_values(1)
    name = name.strip().lower()
    for i, h in enumerate(header, start=1):
        if str(h).strip().lower() == name:
            return i
    raise RuntimeError(f"Column '{name}' not found")


if __name__ == "__main__":
    main()
