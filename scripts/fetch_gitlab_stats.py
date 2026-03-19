#!/usr/bin/env python3
"""
Fetch GitLab contribution stats from one or more accounts and generate
combined SVG cards for the GitHub profile README.

Configuration via environment variables:
  GITLAB_ACCOUNTS  — JSON array of account objects, each with:
                     {"name": "...", "url": "...", "token": "...", "user_id": null}
  (Legacy single-account env vars GITLAB_URL / GITLAB_TOKEN / GITLAB_USER_ID
   are still supported for backward compatibility.)

Generates themed SVG cards in assets/ and maintains assets/gitlab-meta.json
with per-account breakdowns so that a single failing account never wipes out
data from the others.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
META_FILE = ASSETS_DIR / "gitlab-meta.json"
TIMEOUT = 30
PER_PAGE = 100

COLORS = {
    "bg": "#0d1117",
    "card_bg": "#161b22",
    "border": "#30363d",
    "title": "#818cf8",
    "text": "#c9d1d9",
    "muted": "#8b949e",
    "accent": "#818cf8",
    "fire": "#f97583",
    "bar_colors": ["#818cf8", "#c4b5fd", "#6366f1", "#a78bfa", "#7c3aed", "#4f46e5"],
}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def gitlab_get(session: requests.Session, base_url: str, path: str, params: dict | None = None):
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    resp = session.get(url, params=params or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def paginate(session: requests.Session, base_url: str, path: str, params: dict | None = None, max_pages: int = 50):
    params = dict(params or {})
    params.setdefault("per_page", PER_PAGE)
    items: list = []
    page = 1
    while page <= max_pages:
        params["page"] = page
        resp = gitlab_get(session, base_url, path, params)
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return items


# ---------------------------------------------------------------------------
# Data fetching (single account)
# ---------------------------------------------------------------------------

def fetch_user(session, base_url, user_id=None):
    if user_id:
        return gitlab_get(session, base_url, f"/api/v4/users/{user_id}").json()
    return gitlab_get(session, base_url, "/api/v4/user").json()


def fetch_events(session, base_url, user_id):
    return paginate(session, base_url, f"/api/v4/users/{user_id}/events")


def fetch_contributed_projects(session, base_url, user_id):
    return paginate(session, base_url, f"/api/v4/users/{user_id}/contributed_projects")


def fetch_merge_request_count(session, base_url, user_id):
    resp = gitlab_get(session, base_url, "/api/v4/merge_requests", {
        "author_id": user_id,
        "scope": "all",
        "per_page": 1,
        "page": 1,
    })
    total = resp.headers.get("x-total", "0")
    return int(total)


def fetch_languages(session, base_url, project_ids):
    lang_totals: dict[str, float] = {}
    for pid in project_ids[:30]:
        try:
            data = gitlab_get(session, base_url, f"/api/v4/projects/{pid}/languages").json()
            for lang, pct in data.items():
                lang_totals[lang] = lang_totals.get(lang, 0) + pct
        except requests.RequestException:
            continue
    total = sum(lang_totals.values()) or 1
    return {k: round(v / total * 100, 1) for k, v in sorted(lang_totals.items(), key=lambda x: -x[1])[:8]}


def event_dates_from_events(events):
    dates = set()
    for ev in events:
        dt = datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
        dates.add(dt.date().isoformat())
    return sorted(dates)


# ---------------------------------------------------------------------------
# Per-account fetch with error handling (returns None on failure)
# ---------------------------------------------------------------------------

def fetch_account_stats(account: dict) -> dict | None:
    """Fetch all stats for a single GitLab account. Returns dict or None on error."""
    name = account.get("name", account["url"])
    url = account["url"]
    token = account["token"]
    user_id_cfg = account.get("user_id") or None

    session = requests.Session()
    session.headers["PRIVATE-TOKEN"] = token

    try:
        user = fetch_user(session, url, user_id_cfg)
        user_id = user["id"]
        username = user.get("username", "user")
        print(f"[INFO] [{name}] Fetching stats for {username} (id={user_id})")

        events = fetch_events(session, url, user_id)
        print(f"[INFO] [{name}] {len(events)} events from the last year")

        projects = fetch_contributed_projects(session, url, user_id)
        project_ids = [p["id"] for p in projects]
        print(f"[INFO] [{name}] {len(projects)} projects contributed to")

        mr_count = fetch_merge_request_count(session, url, user_id)
        print(f"[INFO] [{name}] {mr_count} merge requests")

        languages = fetch_languages(session, url, project_ids)
        print(f"[INFO] [{name}] Top languages: {languages}")

        return {
            "contributions": len(events),
            "merge_requests": mr_count,
            "projects": len(projects),
            "languages": languages,
            "event_dates": event_dates_from_events(events),
        }

    except requests.HTTPError as e:
        print(f"[ERROR] [{name}] HTTP {e.response.status_code}: {e}")
        return None
    except requests.ConnectionError as e:
        print(f"[ERROR] [{name}] Connection failed: {e}")
        return None
    except requests.Timeout:
        print(f"[ERROR] [{name}] Request timed out")
        return None
    except (KeyError, ValueError) as e:
        print(f"[ERROR] [{name}] Unexpected response: {e}")
        return None


# ---------------------------------------------------------------------------
# Stats aggregation across accounts
# ---------------------------------------------------------------------------

def compute_streaks_from_dates(date_strings):
    if not date_strings:
        return 0, 0

    from datetime import date as date_type
    dates = sorted({date_type.fromisoformat(d) for d in date_strings})

    longest_streak = streak = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            streak += 1
        else:
            longest_streak = max(longest_streak, streak)
            streak = 1
    longest_streak = max(longest_streak, streak)

    today = datetime.now(timezone.utc).date()
    if today in dates or (today - timedelta(days=1)) in dates:
        streak = 1
        for i in range(len(dates) - 2, -1, -1):
            if (dates[i + 1] - dates[i]).days == 1:
                streak += 1
            else:
                break
        current_streak = streak
    else:
        current_streak = 0

    return current_streak, longest_streak


def merge_languages(lang_dicts: list[dict]) -> dict:
    combined: dict[str, float] = {}
    for ld in lang_dicts:
        for lang, pct in ld.items():
            combined[lang] = combined.get(lang, 0) + pct
    total = sum(combined.values()) or 1
    return {k: round(v / total * 100, 1) for k, v in sorted(combined.items(), key=lambda x: -x[1])[:8]}


# ---------------------------------------------------------------------------
# SVG generation
# ---------------------------------------------------------------------------

def svg_stats_card(contributions, merge_requests, projects, account_count):
    title = "GitLab Stats" if account_count == 1 else f"GitLab Stats ({account_count} accounts)"
    rows = [
        ("Total Contributions", str(contributions)),
        ("Merge Requests", str(merge_requests)),
        ("Projects Contributed To", str(projects)),
    ]
    row_svg = ""
    for i, (label, value) in enumerate(rows):
        y = 70 + i * 32
        row_svg += f"""
    <text x="25" y="{y}" fill="{COLORS['text']}" font-size="13">{label}</text>
    <text x="370" y="{y}" fill="{COLORS['title']}" font-size="13" font-weight="600" text-anchor="end">{value}</text>"""

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="395" height="175" viewBox="0 0 395 175">
  <rect width="395" height="175" rx="6" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-width="1"/>
  <text x="25" y="35" fill="{COLORS['title']}" font-size="16" font-weight="700" font-family="'Segoe UI', Ubuntu, sans-serif">{title}</text>
  <line x1="25" y1="48" x2="370" y2="48" stroke="{COLORS['border']}" stroke-width="0.5"/>{row_svg}
</svg>"""


def svg_streak_card(current_streak, longest_streak):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="395" height="175" viewBox="0 0 395 175">
  <rect width="395" height="175" rx="6" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-width="1"/>
  <text x="197" y="35" fill="{COLORS['title']}" font-size="16" font-weight="700" font-family="'Segoe UI', Ubuntu, sans-serif" text-anchor="middle">GitLab Streak</text>
  <line x1="25" y1="48" x2="370" y2="48" stroke="{COLORS['border']}" stroke-width="0.5"/>
  <text x="120" y="90" fill="{COLORS['fire']}" font-size="36" font-weight="800" font-family="'Segoe UI', Ubuntu, sans-serif" text-anchor="middle">{current_streak}</text>
  <text x="120" y="115" fill="{COLORS['muted']}" font-size="12" font-family="'Segoe UI', Ubuntu, sans-serif" text-anchor="middle">Current Streak</text>
  <line x1="197" y1="60" x2="197" y2="145" stroke="{COLORS['border']}" stroke-width="0.5"/>
  <text x="275" y="90" fill="{COLORS['accent']}" font-size="36" font-weight="800" font-family="'Segoe UI', Ubuntu, sans-serif" text-anchor="middle">{longest_streak}</text>
  <text x="275" y="115" fill="{COLORS['muted']}" font-size="12" font-family="'Segoe UI', Ubuntu, sans-serif" text-anchor="middle">Longest Streak</text>
</svg>"""


def svg_langs_card(languages):
    if not languages:
        return _placeholder_svg("No language data available")

    bar_width = 345
    bar_y = 55
    bar_height = 12
    segments = ""
    x_offset = 25
    colors = COLORS["bar_colors"]

    for i, (lang, pct) in enumerate(languages.items()):
        w = max(pct / 100 * bar_width, 1)
        color = colors[i % len(colors)]
        segments += f'  <rect x="{x_offset}" y="{bar_y}" width="{w}" height="{bar_height}" rx="2" fill="{color}"/>\n'
        x_offset += w

    legend = ""
    lx, ly = 25, 90
    for i, (lang, pct) in enumerate(languages.items()):
        color = colors[i % len(colors)]
        legend += f"""  <circle cx="{lx}" cy="{ly}" r="5" fill="{color}"/>
  <text x="{lx + 10}" y="{ly + 4}" fill="{COLORS['text']}" font-size="11" font-family="'Segoe UI', Ubuntu, sans-serif">{lang} {pct}%</text>
"""
        lx += 100
        if lx > 340:
            lx = 25
            ly += 22

    height = ly + 20
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="395" height="{height}" viewBox="0 0 395 {height}">
  <rect width="395" height="{height}" rx="6" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-width="1"/>
  <text x="25" y="35" fill="{COLORS['title']}" font-size="16" font-weight="700" font-family="'Segoe UI', Ubuntu, sans-serif">GitLab — Top Languages</text>
  <line x1="25" y1="46" x2="370" y2="46" stroke="{COLORS['border']}" stroke-width="0.5"/>
{segments}
{legend}
</svg>"""


def svg_activity_graph(date_strings):
    """Generate a bar chart of daily contributions for the last 31 days.

    Uses only rect, text, and line elements — these are the SVG primitives
    that GitHub's markdown renderer does NOT sanitize away.
    """
    from collections import Counter

    if not date_strings:
        return _placeholder_svg_wide("No contribution data available")

    date_counts = Counter(date_strings)
    today = datetime.now(timezone.utc).date()
    num_days = 31

    days = []
    for i in range(num_days):
        d = today - timedelta(days=num_days - 1 - i)
        days.append((d, date_counts.get(d.isoformat(), 0)))

    max_count = max(c for _, c in days) or 1

    width = 840
    height = 200
    pad_left = 50
    pad_right = 20
    pad_top = 45
    pad_bottom = 30
    graph_w = width - pad_left - pad_right
    graph_h = height - pad_top - pad_bottom
    bar_gap = 3
    bar_w = (graph_w - bar_gap * (num_days - 1)) / num_days
    baseline = pad_top + graph_h

    grid = ""
    for frac in [0.25, 0.5, 0.75, 1.0]:
        gy = round(baseline - frac * graph_h, 1)
        val = int(max_count * frac)
        grid += f'  <line x1="{pad_left}" y1="{gy}" x2="{width - pad_right}" y2="{gy}" stroke="{COLORS["border"]}" stroke-width="0.5"/>\n'
        grid += f'  <text x="{pad_left - 8}" y="{gy + 4}" fill="{COLORS["muted"]}" font-size="9" text-anchor="end" font-family="\'Segoe UI\', Ubuntu, sans-serif">{val}</text>\n'

    bars = ""
    labels = ""
    for i, (d, count) in enumerate(days):
        x = round(pad_left + i * (bar_w + bar_gap), 2)
        bar_h = round((count / max_count) * graph_h, 2) if count else 0
        y = round(baseline - bar_h, 2)
        color = COLORS["accent"] if count > 0 else COLORS["border"]
        bars += f'  <rect x="{x}" y="{y}" width="{round(bar_w, 2)}" height="{bar_h}" rx="2" fill="{color}"/>\n'
        if d.day == 1 or i == 0 or i == num_days - 1:
            label = d.strftime("%b %d")
            lx = round(x + bar_w / 2, 2)
            labels += f'  <text x="{lx}" y="{height - 8}" fill="{COLORS["muted"]}" font-size="9" text-anchor="middle" font-family="\'Segoe UI\', Ubuntu, sans-serif">{label}</text>\n'

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="{width}" height="{height}" rx="6" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-width="1"/>
  <text x="{pad_left}" y="28" fill="{COLORS['title']}" font-size="14" font-weight="700" font-family="'Segoe UI', Ubuntu, sans-serif">GitLab Contribution Graph — Last 31 Days</text>
  <line x1="{pad_left}" y1="{baseline}" x2="{width - pad_right}" y2="{baseline}" stroke="{COLORS['border']}" stroke-width="0.5"/>
{grid}{bars}{labels}</svg>"""


def _placeholder_svg_wide(message):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="840" height="200" viewBox="0 0 840 200">
  <rect width="840" height="200" rx="6" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-width="1"/>
  <text x="420" y="105" fill="{COLORS['muted']}" font-size="14" font-family="'Segoe UI', Ubuntu, sans-serif" text-anchor="middle">{message}</text>
</svg>"""


def _placeholder_svg(message):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="395" height="175" viewBox="0 0 395 175">
  <rect width="395" height="175" rx="6" fill="{COLORS['bg']}" stroke="{COLORS['border']}" stroke-width="1"/>
  <text x="197" y="92" fill="{COLORS['muted']}" font-size="14" font-family="'Segoe UI', Ubuntu, sans-serif" text-anchor="middle">{message}</text>
</svg>"""


# ---------------------------------------------------------------------------
# Fail-safe helpers
# ---------------------------------------------------------------------------

def load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"accounts": {}, "combined": {}}


def save_meta(meta: dict):
    meta["last_updated"] = datetime.now(timezone.utc).isoformat()
    META_FILE.write_text(json.dumps(meta, indent=2) + "\n")


def is_suspicious(combined: dict, old_combined: dict | None) -> bool:
    if not old_combined:
        return False
    if combined["total_contributions"] == 0 and old_combined.get("total_contributions", 0) > 0:
        print("[WARN] Combined contributions dropped to 0 but old data had contributions — suspicious, skipping")
        return True
    return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_accounts() -> list[dict]:
    """Parse account config from GITLAB_ACCOUNTS JSON or legacy single env vars."""
    raw = os.environ.get("GITLAB_ACCOUNTS", "").strip()
    if raw:
        try:
            accounts = json.loads(raw)
            if isinstance(accounts, list) and accounts:
                for i, acct in enumerate(accounts):
                    if "url" not in acct or "token" not in acct:
                        print(f"[ERROR] Account at index {i} missing 'url' or 'token'")
                        sys.exit(0)
                    acct.setdefault("name", f"Account {i + 1}")
                return accounts
        except json.JSONDecodeError as e:
            print(f"[ERROR] Failed to parse GITLAB_ACCOUNTS JSON: {e}")
            sys.exit(0)

    url = os.environ.get("GITLAB_URL", "").strip()
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if url and token:
        return [{
            "name": "GitLab",
            "url": url,
            "token": token,
            "user_id": os.environ.get("GITLAB_USER_ID", "").strip() or None,
        }]

    print("[ERROR] Set GITLAB_ACCOUNTS (JSON array) or GITLAB_URL + GITLAB_TOKEN")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    accounts = load_accounts()
    print(f"[INFO] Configured {len(accounts)} GitLab account(s): {[a['name'] for a in accounts]}")

    old_meta = load_meta()
    old_account_data = old_meta.get("accounts", {})

    fresh_account_data: dict[str, dict] = {}
    failed_accounts: list[str] = []

    for acct in accounts:
        name = acct["name"]
        result = fetch_account_stats(acct)
        if result is not None:
            fresh_account_data[name] = result
        else:
            failed_accounts.append(name)
            if name in old_account_data:
                print(f"[INFO] [{name}] Using last-known-good data from previous run")
                fresh_account_data[name] = old_account_data[name]
            else:
                print(f"[WARN] [{name}] No fresh data and no cached data — skipping this account")

    if not fresh_account_data:
        print("[ERROR] All accounts failed and no cached data available — keeping old SVGs")
        sys.exit(0)

    total_contributions = sum(a["contributions"] for a in fresh_account_data.values())
    total_mrs = sum(a["merge_requests"] for a in fresh_account_data.values())
    total_projects = sum(a["projects"] for a in fresh_account_data.values())

    all_dates = []
    for a in fresh_account_data.values():
        all_dates.extend(a.get("event_dates", []))
    current_streak, longest_streak = compute_streaks_from_dates(all_dates)

    all_langs = [a.get("languages", {}) for a in fresh_account_data.values()]
    combined_langs = merge_languages(all_langs)

    combined = {
        "total_contributions": total_contributions,
        "merge_requests": total_mrs,
        "projects": total_projects,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
    }

    old_combined = old_meta.get("combined")
    if is_suspicious(combined, old_combined):
        sys.exit(0)

    active_account_count = len(fresh_account_data)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    (ASSETS_DIR / "gitlab-stats.svg").write_text(
        svg_stats_card(total_contributions, total_mrs, total_projects, active_account_count))
    (ASSETS_DIR / "gitlab-streak.svg").write_text(
        svg_streak_card(current_streak, longest_streak))
    (ASSETS_DIR / "gitlab-langs.svg").write_text(
        svg_langs_card(combined_langs))
    (ASSETS_DIR / "gitlab-activity-graph.svg").write_text(
        svg_activity_graph(all_dates))

    new_meta = {
        "accounts": fresh_account_data,
        "combined": combined,
    }
    save_meta(new_meta)

    status = "all succeeded" if not failed_accounts else f"{len(failed_accounts)} failed (used cached)"
    print(f"[INFO] SVGs updated — {active_account_count} account(s), {status}")


if __name__ == "__main__":
    main()
