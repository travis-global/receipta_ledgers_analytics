import os
import requests
from datetime import datetime, timedelta
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
    """Returns previous Monday and Saturday (inclusive)"""
    today = datetime.utcnow().date()
    # If today is Sunday (weekday 6), previous week is Mon-Sat just ended
    days_since_monday = today.weekday()  # 0=Mon ... 6=Sun
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
    """Facebook Page level insights"""
    metrics = [
        "page_impressions",
        "page_impressions_unique",
        "page_post_engagements",
        "page_fans",
        "page_views_total",
    ]
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

def get_ig_account_insights(since, until):
    """Instagram account level insights"""
    metrics = "impressions,reach,profile_views,follower_count"
    data = graph_get(
        f"{IG_USER_ID}/insights",
        {
            "metric": metrics,
            "period": "day",
            "since": since.strftime("%Y-%m-%d"),
            "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
        },
    )
    return data.get("data", [])

def get_fb_posts(since, until):
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

def get_ig_media(since, until):
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

def get_post_insights(post_id, is_instagram=False):
    if is_instagram:
        metrics = "impressions,reach,likes,comments,shares,saved,plays"
    else:
        metrics = "post_impressions,post_impressions_unique,post_engaged_users,post_clicks,post_reactions_by_type_total"
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
            values = item.get("values", [])
            if values:
                return values[-1].get("value", default)
    return default

# ====================== GOOGLE SHEETS ======================
def get_worksheet(name):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(name)

def append_rows(sheet_name, rows):
    if not rows:
        return
    ws = get_worksheet(sheet_name)
    ws.append_rows(rows, value_input_option="USER_ENTERED")

# ====================== MAIN ======================
def main():
    week_start, week_end = get_previous_week()
    print(f"Processing week: {week_start} → {week_end}")

    # ---------- PAGE LEVEL ----------
    page_rows = []

    # Facebook
    fb_insights = get_page_insights(week_start, week_end)
    fb_reach = sum_metric(fb_insights, "page_impressions_unique")
    fb_impressions = sum_metric(fb_insights, "page_impressions")
    fb_engagements = sum_metric(fb_insights, "page_post_engagements")
    fb_followers = safe_get(fb_insights, "page_fans")
    fb_profile_visits = sum_metric(fb_insights, "page_views_total")
    fb_eng_rate = round((fb_engagements / fb_reach * 100), 2) if fb_reach else 0

    page_rows.append([
        str(week_start), str(week_end), "Facebook",
        fb_reach, fb_impressions, fb_profile_visits, fb_followers,
        fb_engagements, fb_eng_rate, "", ""  # Link_Clicks & Notes empty for now
    ])

    # Instagram
    ig_insights = get_ig_account_insights(week_start, week_end)
    ig_reach = sum_metric(ig_insights, "reach")
    ig_impressions = sum_metric(ig_insights, "impressions")
    ig_profile_visits = sum_metric(ig_insights, "profile_views")
    ig_followers = safe_get(ig_insights, "follower_count")
    # Engagements not directly available at account level in the same way
    page_rows.append([
        str(week_start), str(week_end), "Instagram",
        ig_reach, ig_impressions, ig_profile_visits, ig_followers,
        "", "", "", ""
    ])

    append_rows("Weekly_Page_Summary", page_rows)
    print("Page summary written")

    # ---------- POST LEVEL ----------
    post_rows = []

    # Facebook posts
    for post in get_fb_posts(week_start, week_end):
        insights = get_post_insights(post["id"], is_instagram=False)
        reach = safe_get(insights, "post_impressions_unique")
        impressions = safe_get(insights, "post_impressions")
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
            reach, impressions, "", "", "", "", "",  # likes/comments etc limited on FB
            eng_rate, clicks, post.get("permalink_url", "")
        ])

    # Instagram media
    for media in get_ig_media(week_start, week_end):
        insights = get_post_insights(media["id"], is_instagram=True)
        reach = safe_get(insights, "reach")
        impressions = safe_get(insights, "impressions")
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
            caption, reach, impressions, likes, comments, shares, saves,
            video_views, eng_rate, "", media.get("permalink", "")
        ])

    append_rows("Post_Performance", post_rows)
    print(f"Wrote {len(post_rows)} posts")
    print("Done!")

if __name__ == "__main__":
    main()
