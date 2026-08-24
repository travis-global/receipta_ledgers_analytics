import os
import requests
from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials
import gspread

# ====================== CONFIG ======================
META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
FB_PAGE_ID = "1051491648050502"
IG_USER_ID = "17841436366436526"
SPREADSHEET_ID = "1N827qAtiP6_xOe7ypJ-YffrlOCVZbQTTaGlgJxAQ7cc"
SERVICE_ACCOUNT_FILE = "service_account.json"

GRAPH_VERSION = "v21.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"

# ====================== DATE RANGE ======================
def get_previous_week():
    today = datetime.now(timezone.utc).date()
    days_since_monday = today.weekday()
    last_saturday = today - timedelta(days=(days_since_monday + 1) % 7)
    last_monday = last_saturday - timedelta(days=5)
    return last_monday, last_saturday

# ====================== META API ======================
def graph_get(endpoint, params=None):
    params = params or {}
    params["access_token"] = META_ACCESS_TOKEN
    url = f"{BASE_URL}/{endpoint}"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def get_page_insights(since, until):
    metrics = [
        "page_media_view",
        "page_total_media_view_unique",
        "page_post_engagements",
        "page_follows",
        "page_views_total",
    ]
    try:
        data = graph_get(
            f"{FB_PAGE_ID}/insights",
            {
                "metric": ",".join(metrics),
                "period": "day",
                "since": since.strftime("%Y-%m-%d"),
                "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            },
        )
        return data.get("data", [])
    except Exception as e:
        print(f"Facebook Page insights error: {e}")
        return []

def get_ig_account_insights(since, until):
    """
    Instagram requires metric_type=total_value for most modern metrics.
    We request only the most reliable ones.
    """
    try:
        data = graph_get(
            f"{IG_USER_ID}/insights",
            {
                "metric": "reach,follower_count,profile_views,total_interactions",
                "period": "day",
                "metric_type": "total_value",
                "since": since.strftime("%Y-%m-%d"),
                "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            },
        )
        return data.get("data", [])
    except Exception as e:
        print(f"Instagram account insights error: {e}")
        if hasattr(e, "response") and e.response is not None:
            print("Response:", e.response.text)
        return []

def get_fb_posts(since, until):
    try:
        data = graph_get(
            f"{FB_PAGE_ID}/posts",
            {
                "fields": "id,message,created_time,permalink_url,attachments{media_type}",
                "since": since.strftime("%Y-%m-%d"),
                "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
                "limit": 50,
            },
        )
        return data.get("data", [])
    except Exception as e:
        print(f"Error fetching FB posts: {e}")
        return []

def get_ig_media(since, until):
    try:
        data = graph_get(
            f"{IG_USER_ID}/media",
            {
                "fields": "id,caption,timestamp,permalink,media_type,like_count,comments_count",
                "since": since.strftime("%Y-%m-%d"),
                "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
                "limit": 50,
            },
        )
        return data.get("data", [])
    except Exception as e:
        print(f"Error fetching IG media: {e}")
        return []

def get_post_insights(post_id, is_instagram=False):
    if is_instagram:
        metrics = "reach,likes,comments,shares,saved,plays,total_interactions"
    else:
        metrics = "post_media_view,post_total_media_view_unique,post_engaged_users,post_clicks"
    try:
        data = graph_get(f"{post_id}/insights", {"metric": metrics})
        return data.get("data", [])
    except Exception:
        return []

# ====================== HELPERS ======================
def sum_metric(insights_list, metric_name):
    total = 0
    for item in insights_list:
        if item.get("name") == metric_name:
            # Handle both time_series and total_value responses
            if "total_value" in item:
                val = item["total_value"].get("value", 0)
                total += val or 0
            else:
                for v in item.get("values", []):
                    val = v.get("value", 0)
                    if isinstance(val, dict):
                        total += sum(val.values())
                    else:
                        total += val or 0
    return total

def safe_get(insights, name, default=0):
    for item in insights:
        if item.get("name") == name:
            if "total_value" in item:
                return item["total_value"].get("value", default)
            values = item.get("values", [])
            if values:
                return values[-1].get("value", default)
    return default

# ====================== GOOGLE SHEETS ======================
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds)

def get_or_create_worksheet(sh, preferred_name, fallback_index=0):
    """Try exact name first, otherwise use sheet by index"""
    try:
        return sh.worksheet(preferred_name)
    except gspread.WorksheetNotFound:
        print(f"Worksheet '{preferred_name}' not found. Using sheet index {fallback_index}")
        worksheets = sh.worksheets()
        if len(worksheets) > fallback_index:
            return worksheets[fallback_index]
        raise

def append_rows(sh, preferred_name, rows, fallback_index=0):
    if not rows:
        print(f"No rows to write for {preferred_name}")
        return
    ws = get_or_create_worksheet(sh, preferred_name, fallback_index)
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Wrote {len(rows)} rows to '{ws.title}'")

# ====================== MAIN ======================
def main():
    week_start, week_end = get_previous_week()
    print(f"Processing week: {week_start} → {week_end}")

    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    # Show available sheets for debugging
    print("Available worksheets:", [ws.title for ws in sh.worksheets()])

    # ---------- PAGE LEVEL ----------
    page_rows = []

    # Facebook
    fb_insights = get_page_insights(week_start, week_end)
    fb_reach = sum_metric(fb_insights, "page_total_media_view_unique")
    fb_impressions = sum_metric(fb_insights, "page_media_view")
    fb_engagements = sum_metric(fb_insights, "page_post_engagements")
    fb_followers = safe_get(fb_insights, "page_follows")
    fb_profile_visits = sum_metric(fb_insights, "page_views_total")
    fb_eng_rate = round((fb_engagements / fb_reach * 100), 2) if fb_reach else 0

    page_rows.append([
        str(week_start), str(week_end), "Facebook",
        fb_reach, fb_impressions, fb_profile_visits, fb_followers,
        fb_engagements, fb_eng_rate, "", ""
    ])

    # Instagram
    ig_insights = get_ig_account_insights(week_start, week_end)
    ig_reach = sum_metric(ig_insights, "reach")
    ig_profile_visits = sum_metric(ig_insights, "profile_views")
    ig_followers = safe_get(ig_insights, "follower_count")
    ig_engagements = sum_metric(ig_insights, "total_interactions")

    page_rows.append([
        str(week_start), str(week_end), "Instagram",
        ig_reach, "", ig_profile_visits, ig_followers,
        ig_engagements, "", "", ""
    ])

    append_rows(sh, "Weekly_Page_Summary", page_rows, fallback_index=0)

    # ---------- POST LEVEL ----------
    post_rows = []

    for post in get_fb_posts(week_start, week_end):
        insights = get_post_insights(post["id"], is_instagram=False)
        reach = safe_get(insights, "post_total_media_view_unique")
        impressions = safe_get(insights, "post_media_view")
        engagements = safe_get(insights, "post_engaged_users")
        clicks = safe_get(insights, "post_clicks")
        eng_rate = round((engagements / reach * 100), 2) if reach else 0

        caption = (post.get("message") or "")[:100]
        post_type = "post"
        if post.get("attachments", {}).get("data"):
            post_type = post["attachments"]["data"][0].get("media_type", "post")

        post_rows.append([
            str(week_start), "Facebook", post["id"],
            post.get("created_time", "")[:10], post_type, caption,
            reach, impressions, "", "", "", "", "",
            eng_rate, clicks, post.get("permalink_url", "")
        ])

    for media in get_ig_media(week_start, week_end):
        insights = get_post_insights(media["id"], is_instagram=True)
        reach = safe_get(insights, "reach")
        likes = media.get("like_count") or safe_get(insights, "likes")
        comments = media.get("comments_count") or safe_get(insights, "comments")
        shares = safe_get(insights, "shares")
        saves = safe_get(insights, "saved")
        video_views = safe_get(insights, "plays")
        eng = (likes or 0) + (comments or 0) + (shares or 0) + (saves or 0)
        eng_rate = round((eng / reach * 100), 2) if reach else 0

        caption = (media.get("caption") or "")[:100]
        post_rows.append([
            str(week_start), "Instagram", media["id"],
            media.get("timestamp", "")[:10], media.get("media_type", "IMAGE"),
            caption, reach, "", likes, comments, shares, saves,
            video_views, eng_rate, "", media.get("permalink", "")
        ])

    append_rows(sh, "Post_Performance", post_rows, fallback_index=1)

    print("Done!")

if __name__ == "__main__":
    main()
