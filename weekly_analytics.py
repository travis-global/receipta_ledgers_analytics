"""
Weekly Meta (Facebook + Instagram) analytics -> Google Sheets

WHAT CHANGED FROM THE ORIGINAL SCRIPT (read this first)
---------------------------------------------------------
1. PAGINATION: get_fb_posts / get_ig_media only ever fetched the first
   `limit` posts and ignored the `paging.next` cursor. Any post beyond
   that cutoff was silently dropped. Now every listing call follows
   `paging.next` until exhausted (capped at MAX_PAGES as a safety net).

2. SILENT FAILURES -> VISIBLE ERRORS: if a post's /insights call failed
   (rate limit, permission issue, unsupported metric for that post type,
   etc.), the old script caught the exception and returned [], and
   safe_get()/sum_metric() then defaulted to 0. A failed fetch and a
   real "zero engagement" post were indistinguishable in the sheet.
   Now a failed fetch writes "ERR" into the affected cells and gets
   logged to the console with the post ID, so you can tell "nobody
   engaged with this post" apart from "we couldn't read this post."

3. RATE-LIMIT RETRIES: added a small retry-with-backoff wrapper around
   every Graph API call, since fetching insights per-post (1 call per
   post) is exactly the pattern that trips Meta's rate limits on a busy
   week.

4. WEEKLY REACH FIX (this is the most likely source of "numbers don't
   add up"): the original script pulled Facebook page reach as a
   *daily* unique-viewer metric (period=day) for 6 days and summed the
   6 daily values. Reach is a unique-person count, so summing days
   double-counts anyone who saw the page on more than one day in the
   week — it inflates weekly reach and understates engagement rate.
   This version requests it as a single total_value over the whole
   since/until range instead (the same pattern the IG call already
   used), which avoids double-counting. NOTE: I have not been able to
   test this live against your actual page — Meta's docs suggest
   metric_type=total_value is supported for these Page metrics, but if
   your Graph API version rejects it, the script falls back to the old
   day-summed method AND prints a warning so you know the reach number
   for that week is an upper bound, not exact.

5. MISSING CAPTIONS: posts with no message/caption now show
   "(no caption)" instead of a blank cell, so it's clear the row is
   real data and not a fetch failure.

6. CROSS-PLATFORM GAPS MADE EXPLICIT: fields that simply don't exist on
   one platform (e.g. "impressions" isn't an IG post concept, "saves"
   isn't an FB concept) are now written as "N/A" rather than blank, so
   blank (should have data but didn't), 0 (real zero), ERR (fetch
   failed), and N/A (doesn't apply here) are all visually distinct in
   the sheet.

7. RECONCILIATION CHECK: at the end of the run, the script sums the
   post-level numbers it collected and compares them to the page-level
   totals for the same week, and prints a warning if they're wildly
   out of line. This won't fix anything by itself but gives you an
   early flag when something's off, instead of finding out a month
   later.

WHAT THIS DOES NOT FIX: if your page/account genuinely has posts with
incomplete metadata (no caption, no attachment info) because of how
they were originally posted, that's a data-entry issue on the Meta
side, not something this script can back-fill.
"""

import os
import time
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

MAX_PAGES = 20          # safety cap on pagination loops
MAX_RETRIES = 3         # retries per API call on transient errors
RETRY_BACKOFF_SECONDS = 2

ERR = "ERR"             # sentinel: we tried to fetch this and failed
NA = "N/A"              # sentinel: this metric doesn't exist on this platform

# Tracks post IDs whose insights call failed, for the end-of-run summary
FAILED_INSIGHT_FETCHES = []

# ====================== DATE RANGE ======================
def get_previous_week():
    """Returns (monday, saturday) for the most recently completed Mon-Sat week."""
    today = datetime.now(timezone.utc).date()
    days_since_monday = today.weekday()
    last_saturday = today - timedelta(days=(days_since_monday + 1) % 7)
    last_monday = last_saturday - timedelta(days=5)
    return last_monday, last_saturday

# ====================== META API CORE ======================
def _is_transient_error(exc):
    """Rate limits and 5xx are worth retrying; permission/bad-request errors are not."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status >= 500:
            return True
        try:
            err = exc.response.json().get("error", {})
            # Meta rate-limit / transient error codes
            if err.get("code") in (4, 17, 32, 613):
                return True
        except Exception:
            pass
    return False

def _describe_http_error(e):
    """requests' default HTTPError str hides Meta's actual error message
    (e.g. which metric was invalid). Pull the real message/code out of the
    response body so failures are diagnosable from the console log alone."""
    if e.response is None:
        return str(e)
    try:
        err = e.response.json().get("error", {})
        parts = [err.get("message", str(e))]
        if err.get("error_subcode"):
            parts.append(f"subcode={err['error_subcode']}")
        if err.get("code"):
            parts.append(f"code={err['code']}")
        return " | ".join(parts)
    except Exception:
        return str(e)

def _get(url, params=None):
    """Single HTTP GET with retry/backoff on transient failures."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            last_exc = e
            if attempt < MAX_RETRIES and _is_transient_error(e):
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"  Transient error ({_describe_http_error(e)}), retrying in {wait}s "
                      f"[attempt {attempt}/{MAX_RETRIES}]...")
                time.sleep(wait)
                continue
            # Re-raise with Meta's actual message attached so callers that just
            # do str(e) still see the real cause, not a generic "400 Bad Request".
            e.args = (f"{_describe_http_error(e)}",)
            raise
    raise last_exc

def graph_get(endpoint, params=None):
    params = dict(params or {})
    params["access_token"] = META_ACCESS_TOKEN
    url = f"{BASE_URL}/{endpoint}"
    return _get(url, params)

def graph_get_paginated(endpoint, params):
    """Follows paging.next until exhausted (or MAX_PAGES hit). Returns combined list."""
    results = []
    data = graph_get(endpoint, params)
    results.extend(data.get("data", []))
    next_url = data.get("paging", {}).get("next")
    pages_fetched = 1

    while next_url and pages_fetched < MAX_PAGES:
        data = _get(next_url)  # paging.next is already a full authenticated URL
        results.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        pages_fetched += 1

    if next_url:
        print(f"  WARNING: hit MAX_PAGES={MAX_PAGES} cap for {endpoint}; "
              f"there may be more results than were fetched.")

    return results

# ====================== PAGE / ACCOUNT LEVEL ======================
def get_page_insights_total(since, until):
    """
    Page-level totals for the week as single aggregated values
    (metric_type=total_value), avoiding the double-counted-reach bug
    from summing daily unique values. Falls back to the old day-summed
    method if the API rejects total_value for these metrics.
    """
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
                "metric_type": "total_value",
                "period": "day",
                "since": since.strftime("%Y-%m-%d"),
                "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            },
        )
        return data.get("data", []), True  # True = trustworthy (not double-counted)
    except Exception as e:
        print(f"  page insights total_value failed ({e}); "
              f"falling back to daily-sum method (reach may be inflated).")
        return get_page_insights_daily_sum(since, until), False

def get_page_insights_daily_sum(since, until):
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
    results = []
    try:
        data = graph_get(
            f"{IG_USER_ID}/insights",
            {
                "metric": "reach,profile_views,total_interactions,accounts_engaged",
                "period": "day",
                "metric_type": "total_value",
                "since": since.strftime("%Y-%m-%d"),
                "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            },
        )
        results.extend(data.get("data", []))
    except Exception as e:
        print(f"IG total_value metrics error: {e}")

    try:
        data = graph_get(
            f"{IG_USER_ID}/insights",
            {
                "metric": "follower_count",
                "period": "day",
                "since": since.strftime("%Y-%m-%d"),
                "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            },
        )
        results.extend(data.get("data", []))
    except Exception as e:
        print(f"IG follower_count error: {e}")

    return results

# ====================== POST / MEDIA LISTS ======================
def get_fb_posts(since, until):
    # comments/reactions/shares pulled here (from the post object itself) rather
    # than from /insights, because post_engaged_users and post_reactions_by_type_total
    # are deprecated on the Insights endpoint (see NOTE in get_post_insights below) --
    # this also cuts API calls since it rides along with the listing request.
    return graph_get_paginated(
        f"{FB_PAGE_ID}/posts",
        {
            "fields": "id,message,created_time,permalink_url,attachments{media_type},"
                      "comments.summary(true).limit(0),reactions.summary(true).limit(0),shares",
            "since": since.strftime("%Y-%m-%d"),
            "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            "limit": 50,
        },
    )

def get_fb_videos(since, until):
    # Facebook Reels are NOT retrievable through /video_reels -- Meta's own docs
    # say reading is unsupported on that edge (it's publish-only). A Reel is
    # still a Video object underneath, so it's listed here via /videos instead.
    # NOTE: I haven't been able to confirm live that Reels crossposted to this
    # Page actually appear in this listing -- if this comes back empty on a
    # week you know had Reels, that's the first thing to check.
    return graph_get_paginated(
        f"{FB_PAGE_ID}/videos",
        {
            "fields": "id,description,created_time,permalink_url,length",
            "since": since.strftime("%Y-%m-%d"),
            "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            "limit": 50,
        },
    )

def get_ig_media(since, until):
    return graph_get_paginated(
        f"{IG_USER_ID}/media",
        {
            "fields": "id,caption,timestamp,permalink,media_type,like_count,comments_count,media_product_type",
            "since": since.strftime("%Y-%m-%d"),
            "until": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            "limit": 50,
        },
    )

def get_video_insights(video_id):
    """Videos/Reels use a completely different endpoint (video_insights, not
    insights) with their own metric names -- confirmed against Meta's official
    reference doc, not guessed. Reels and plain uploaded videos have DISJOINT
    metric sets and there's no reliable field on the video object itself to
    tell which one you've got before asking, so this tries the Reels set
    first (since that's what you're actually publishing) and falls back to
    the plain-video set on failure. Returns (insights_list, ok, kind) where
    kind is "reel", "video", or None if both attempts failed.
    """
    reel_metrics = ("blue_reels_play_count,fb_reels_total_plays,post_impressions_unique,"
                    "post_video_avg_time_watched,post_video_likes_by_reaction_type,"
                    "post_video_social_actions")
    video_metrics = ("total_video_views,total_video_views_unique,"
                     "total_video_impressions,total_video_reactions_by_type_total")
    try:
        data = graph_get(f"{video_id}/video_insights", {"metric": reel_metrics, "period": "lifetime"})
        return data.get("data", []), True, "reel"
    except Exception as e_reel:
        print(f"  reel-metrics fetch failed for video {video_id}: {e_reel}")
        try:
            data = graph_get(f"{video_id}/video_insights", {"metric": video_metrics, "period": "lifetime"})
            return data.get("data", []), True, "video"
        except Exception as e_video:
            print(f"  video-metrics fetch also failed for video {video_id}: {e_video}")
            FAILED_INSIGHT_FETCHES.append(video_id)
            return [], False, None

def get_post_insights(post_id, is_instagram=False):
    """Returns (insights_list, ok). ok=False means the fetch failed -- caller should
    write ERR into affected cells rather than treating an empty list as real zeros.

    NOTE on the 400 errors from the last run: Meta's Graph API rejects the ENTIRE
    call if even one metric in the comma-separated list is invalid for that
    endpoint/post -- it doesn't just drop the bad one. Two likely culprits, based
    on Meta's own deprecation notices:
      - FB: `post_engaged_users` and `post_reactions_by_type_total` were retired
        as part of the 2024 "New Page Experience" migration. Dropped below;
        engagement (likes/comments/shares) is now read off the post object
        itself in get_fb_posts() instead.
      - IG: `plays` was Meta's old video/Reels-only metric, but it was fully
        deprecated across all API versions in April 2025. It's been replaced
        by `views`, which now works uniformly across FEED, STORY, and REELS
        media -- no need to branch on media type anymore.
    I can't confirm these are the *only* issues without seeing Meta's actual
    error body, which is why _describe_http_error() now surfaces the real
    message -- check the console output on the next run; if it still fails,
    the printed message will name the exact rejected metric/permission.
    """
    if is_instagram:
        # `plays` was deprecated for ALL API versions as of April 21, 2025 --
        # `views` is the current replacement and works uniformly across
        # FEED, STORY, and REELS media_product_types, so no need to branch on type.
        metrics = "reach,likes,comments,shares,saved,views,total_interactions"
    else:
        metrics = "post_media_view,post_total_media_view_unique,post_clicks"
    try:
        data = graph_get(f"{post_id}/insights", {"metric": metrics})
        return data.get("data", []), True
    except Exception as e:
        print(f"  insights fetch failed for post {post_id}: {e}")
        FAILED_INSIGHT_FETCHES.append(post_id)
        return [], False

# ====================== HELPERS ======================
def sum_metric(insights_list, metric_name):
    total = 0
    for item in insights_list:
        if item.get("name") == metric_name:
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

def eng_rate(engagements, reach):
    """Returns '' if reach is ERR (can't compute), 0 if reach is genuinely 0."""
    if reach in (ERR, NA):
        return ERR
    if not reach:
        return 0
    return round((engagements / reach * 100), 2)

# ====================== GOOGLE SHEETS ======================
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds)

def get_or_create_worksheet(sh, preferred_name, fallback_index=0):
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
    print(f"Processing week: {week_start} -> {week_end}")

    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    print("Available worksheets:", [ws.title for ws in sh.worksheets()])

    # ---------- POST LEVEL (fetched first so page-level Link_Clicks can be
    # rolled up from post-level clicks -- FB has no page-wide link-click metric) ----------
    post_rows = []
    post_level_totals = {"fb_reach": 0, "fb_eng": 0, "fb_clicks": 0, "ig_reach": 0, "ig_eng": 0}

    fb_posts = get_fb_posts(week_start, week_end)
    print(f"Fetched {len(fb_posts)} Facebook posts for the week.")
    for post in fb_posts:
        insights, ok = get_post_insights(post["id"], is_instagram=False)
        # Engagement breakdown comes from the post object (comments/reactions/shares),
        # not from insights -- see NOTE in get_post_insights(). "Likes" below is
        # Meta's total reactions count (like+love+haha+etc combined), since Facebook
        # no longer separates a literal "like" count from other reaction types.
        n_comments = post.get("comments", {}).get("summary", {}).get("total_count", 0) or 0
        n_reactions = post.get("reactions", {}).get("summary", {}).get("total_count", 0) or 0
        n_shares = post.get("shares", {}).get("count", 0) or 0
        engagements = n_comments + n_reactions + n_shares

        if ok:
            reach = safe_get(insights, "post_total_media_view_unique")
            impressions = safe_get(insights, "post_media_view")
            clicks = safe_get(insights, "post_clicks")
            post_level_totals["fb_reach"] += reach or 0
            post_level_totals["fb_eng"] += engagements or 0
            post_level_totals["fb_clicks"] += clicks or 0
        else:
            reach = impressions = clicks = ERR

        caption = (post.get("message") or "").strip()[:120] or "(no caption)"
        post_type = "text post"
        if post.get("attachments", {}).get("data"):
            post_type = post["attachments"]["data"][0].get("media_type", "post")

        # Post_Performance columns: Week_Start, Platform, Post_ID, Post_Date, Post_Type,
        # Caption_Preview, Reach, Impressions, Likes, Comments, Shares, Saves,
        # Video_Views, Engagement_Rate, Link_Clicks, Permalink
        post_rows.append([
            str(week_start), "Facebook", post["id"],
            post.get("created_time", "")[:10], post_type, caption,
            reach, impressions,
            n_reactions if ok else ERR, n_comments if ok else ERR, n_shares if ok else ERR,
            NA, NA,  # Saves, Video_Views: not an FB post-level concept
            eng_rate(engagements, reach) if ok else ERR,
            clicks, post.get("permalink_url", ""),
        ])

    # Reels (and any other native uploaded videos) that /posts doesn't cover --
    # dedupe against permalinks already captured above so a video that's ALSO
    # a feed post doesn't get counted twice.
    already_captured_permalinks = {row[15] for row in post_rows if row[15]}
    fb_videos = get_fb_videos(week_start, week_end)
    print(f"Fetched {len(fb_videos)} Facebook videos/Reels for the week.")
    for video in fb_videos:
        permalink = video.get("permalink_url", "")
        if permalink and permalink in already_captured_permalinks:
            continue  # already counted as a feed post

        insights, ok, kind = get_video_insights(video["id"])
        if ok and kind == "reel":
            reach = safe_get(insights, "post_impressions_unique")
            plays = safe_get(insights, "blue_reels_play_count")
            total_plays = safe_get(insights, "fb_reels_total_plays")
            likes = safe_get(insights, "post_video_likes_by_reaction_type")
            # Meta bundles comments+shares into a single reel metric -- there's
            # no split available, so this combined total lands in Comments and
            # Shares is left N/A rather than guessing a split.
            comments_and_shares = safe_get(insights, "post_video_social_actions")
            engagements = (likes or 0) + (comments_and_shares or 0)
            post_level_totals["fb_reach"] += reach or 0
            post_level_totals["fb_eng"] += engagements or 0
            post_rows.append([
                str(week_start), "Facebook", video["id"],
                video.get("created_time", "")[:10], "reel",
                (video.get("description") or "").strip()[:120] or "(no caption)",
                reach, plays, likes, comments_and_shares, NA, NA, total_plays,
                eng_rate(engagements, reach), NA, permalink,
            ])
        elif ok and kind == "video":
            reach = safe_get(insights, "total_video_views_unique")
            views = safe_get(insights, "total_video_views")
            reactions = safe_get(insights, "total_video_reactions_by_type_total")
            impressions = safe_get(insights, "total_video_impressions")
            post_level_totals["fb_reach"] += reach or 0
            post_level_totals["fb_eng"] += reactions or 0
            post_rows.append([
                str(week_start), "Facebook", video["id"],
                video.get("created_time", "")[:10], "video",
                (video.get("description") or "").strip()[:120] or "(no caption)",
                reach, impressions, reactions, NA, NA, NA, views,
                eng_rate(reactions, reach), NA, permalink,
            ])
        else:
            post_rows.append([
                str(week_start), "Facebook", video["id"],
                video.get("created_time", "")[:10], "video/reel (unconfirmed)",
                (video.get("description") or "").strip()[:120] or "(no caption)",
                ERR, ERR, ERR, ERR, ERR, ERR, ERR, ERR, ERR, permalink,
            ])

    ig_media = get_ig_media(week_start, week_end)
    print(f"Fetched {len(ig_media)} Instagram media items for the week.")
    for media in ig_media:
        product_type = media.get("media_product_type") or media.get("media_type", "IMAGE")
        insights, ok = get_post_insights(media["id"], is_instagram=True)
        if ok:
            reach = safe_get(insights, "reach")
            likes = media.get("like_count")
            if likes is None:
                likes = safe_get(insights, "likes")
            comments = media.get("comments_count")
            if comments is None:
                comments = safe_get(insights, "comments")
            shares = safe_get(insights, "shares")
            saves = safe_get(insights, "saved")
            video_views = safe_get(insights, "views")
            engagements = (likes or 0) + (comments or 0) + (shares or 0) + (saves or 0)
            post_level_totals["ig_reach"] += reach or 0
            post_level_totals["ig_eng"] += engagements or 0
        else:
            reach = likes = comments = shares = saves = video_views = ERR
            engagements = ERR

        caption = (media.get("caption") or "").strip()[:120] or "(no caption)"

        post_rows.append([
            str(week_start), "Instagram", media["id"],
            media.get("timestamp", "")[:10], product_type, caption,
            reach, NA, likes, comments, shares, saves, video_views,
            eng_rate(engagements, reach) if ok else ERR,
            NA,  # Link_Clicks: not an organic IG post concept
            media.get("permalink", ""),
        ])

    append_rows(sh, "Post_Performance", post_rows, fallback_index=1)

    # ---------- PAGE LEVEL ----------
    page_rows = []

    fb_insights, fb_reach_trustworthy = get_page_insights_total(week_start, week_end)
    fb_reach = sum_metric(fb_insights, "page_total_media_view_unique")
    fb_impressions = sum_metric(fb_insights, "page_media_view")
    fb_engagements = sum_metric(fb_insights, "page_post_engagements")
    fb_followers = safe_get(fb_insights, "page_follows")
    fb_profile_visits = sum_metric(fb_insights, "page_views_total")
    fb_eng_rate = eng_rate(fb_engagements, fb_reach)
    fb_note = "OK" if fb_reach_trustworthy else "PARTIAL - reach may be inflated (see console)"

    # Weekly_Page_Summary columns: Week_Start, Week_End, Platform, Reach, Impressions,
    # Profile_Visits, Followers, Engagements, Engagement_Rate, Link_Clicks,
    # Accounts_Engaged, Notes
    page_rows.append([
        str(week_start), str(week_end), "Facebook",
        fb_reach, fb_impressions, fb_profile_visits, fb_followers,
        fb_engagements, fb_eng_rate,
        post_level_totals["fb_clicks"],  # rolled up from posts -- no page-wide metric for this
        NA,  # Accounts_Engaged is an IG-only concept
        fb_note,
    ])

    ig_insights = get_ig_account_insights(week_start, week_end)
    ig_reach = sum_metric(ig_insights, "reach")
    ig_profile_visits = sum_metric(ig_insights, "profile_views")
    ig_followers = safe_get(ig_insights, "follower_count")
    ig_engagements = sum_metric(ig_insights, "total_interactions")
    ig_accounts_engaged = sum_metric(ig_insights, "accounts_engaged")

    page_rows.append([
        str(week_start), str(week_end), "Instagram",
        ig_reach, NA, ig_profile_visits, ig_followers,
        ig_engagements, eng_rate(ig_engagements, ig_reach),
        NA,  # Link_Clicks: not tracked for organic IG in this call
        ig_accounts_engaged, "OK",
    ])

    append_rows(sh, "Weekly_Page_Summary", page_rows, fallback_index=0)

    # ---------- RECONCILIATION (console only, sanity check) ----------
    print("\n--- Reconciliation check ---")
    if isinstance(fb_reach, (int, float)) and post_level_totals["fb_reach"]:
        diff_pct = abs(fb_reach - post_level_totals["fb_reach"]) / max(fb_reach, 1) * 100
        print(f"FB page-level reach: {fb_reach} | sum of FB post-level reach: "
              f"{post_level_totals['fb_reach']} | diff: {diff_pct:.1f}%")
        if diff_pct > 50:
            print("  NOTE: page-level and post-level reach are not directly comparable "
                  "(page reach includes non-post views), a large gap alone isn't "
                  "necessarily a bug -- but check it if it looks off.")
    if isinstance(ig_reach, (int, float)) and post_level_totals["ig_reach"]:
        print(f"IG account-level reach: {ig_reach} | sum of IG post-level reach: "
              f"{post_level_totals['ig_reach']}")

    if FAILED_INSIGHT_FETCHES:
        print(f"\n{len(FAILED_INSIGHT_FETCHES)} post(s) failed insight fetches "
              f"(marked ERR in the sheet): {FAILED_INSIGHT_FETCHES}")

    print("\nDone!")

if __name__ == "__main__":
    main()
