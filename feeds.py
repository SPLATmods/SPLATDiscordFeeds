#!/usr/bin/env python3
"""
SPLAT Discord feeds.

Checks:
  - a YouTube channel's upload feed  -> posts new videos to #new-uploads
  - Steam Workshop change notes      -> posts new change notes to #changelog

Runs on a schedule (GitHub Actions). Remembers what it already posted in
state.json so nothing is posted twice. Uses only the Python standard library.

Webhook URLs come from environment variables (GitHub secrets):
  DISCORD_WEBHOOK_UPLOADS    webhook for #new-uploads
  DISCORD_WEBHOOK_CHANGELOG  webhook for #changelog
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

USER_AGENT = "Mozilla/5.0 (compatible; SPLAT-Discord-Feeds/1.0)"
EMBED_DESC_LIMIT = 4000        # Discord allows 4096 in an embed description
MAX_POSTS_PER_SOURCE = 5       # safety cap per run so a glitch can't spam the channel
KEEPALIVE_DAYS = 30            # GitHub disables idle schedules after 60 days


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def truncate(text, limit):
    if len(text) <= limit:
        return text
    cut = text[: limit - 40].rsplit("\n", 1)[0]
    return cut + "\n\n…*(full notes on Steam)*"


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

def post_to_discord(webhook_url, payload):
    """Send one message to a Discord webhook. Retries once on rate limit."""
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                log(f"Discord rate limit, waiting {wait:.1f}s")
                time.sleep(wait + 0.5)
                continue
            log(f"Discord error {e.code}: {e.read().decode(errors='replace')[:300]}")
            return False
        except urllib.error.URLError as e:
            log(f"Discord connection error: {e}")
            return False
    return False


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def parse_youtube_feed(xml_text):
    """Return a list of {id, title, url, published}, newest first."""
    root = ET.fromstring(xml_text)
    videos = []
    for entry in root.findall(f"{ATOM}entry"):
        vid = entry.findtext(f"{YT}videoId")
        if not vid:
            continue
        link = entry.find(f"{ATOM}link")
        videos.append({
            "id": vid,
            "title": (entry.findtext(f"{ATOM}title") or "").strip(),
            "url": link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}",
            "published": entry.findtext(f"{ATOM}published") or "",
        })
    videos.sort(key=lambda v: v["published"], reverse=True)
    return videos


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def is_youtube_short(video_id):
    """True if the video is a Short.

    youtube.com/shorts/<id> loads directly (200) for a Short and redirects
    to /watch for a normal video. If the check fails for any reason the
    video is treated as a normal one.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        f"https://www.youtube.com/shorts/{video_id}",
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        method="HEAD",
    )
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False  # 3xx (redirect to /watch) or an error
    except Exception as e:
        log(f"YouTube: Shorts check failed for {video_id} ({e}); posting as a video")
        return False


def check_youtube(cfg, state, webhook, dry_run):
    yt = cfg.get("youtube") or {}
    channel_id = yt.get("channel_id")
    if not channel_id:
        return
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        videos = parse_youtube_feed(http_get(feed_url))
    except Exception as e:
        log(f"YouTube: could not read feed ({e}); will try again next run")
        return

    yt_state = state.setdefault("youtube", {})
    seen = set(yt_state.get("seen_ids", []))

    if not yt_state.get("initialized"):
        # First run: remember what's already there, don't post old videos.
        yt_state["seen_ids"] = [v["id"] for v in videos]
        yt_state["initialized"] = True
        log(f"YouTube: first run, remembered {len(videos)} existing videos (nothing posted)")
        return

    new = [v for v in videos if v["id"] not in seen]
    if not new:
        log("YouTube: no new videos")
        return

    video_template = yt.get("message", "🎥 **New video!** {title}\n{url}")
    short_template = yt.get("shorts_message", "📱 **New Short!** {title}\n{url}")
    posted_ids = []
    for v in reversed(new[:MAX_POSTS_PER_SOURCE]):  # oldest first
        if is_youtube_short(v["id"]):
            kind = "Short"
            content = short_template.format(title=v["title"], url=f"https://youtube.com/shorts/{v['id']}")
        else:
            kind = "video"
            content = video_template.format(title=v["title"], url=f"https://youtu.be/{v['id']}")
        log(f"YouTube: new {kind} {v['id']} - {v['title']}")
        if dry_run:
            print(json.dumps({"content": content}, indent=2, ensure_ascii=False))
            ok = True
        elif not webhook:
            log("YouTube: DISCORD_WEBHOOK_UPLOADS is not set, skipping post")
            return
        else:
            ok = post_to_discord(webhook, {
                "content": content,
                "allowed_mentions": {"parse": []},
            })
        if ok:
            posted_ids.append(v["id"])
        else:
            break  # stop; unposted videos will be retried next run

    # Mark posted ones as seen. If we hit the safety cap, mark the rest seen too
    # so a flood of old videos never gets posted.
    skipped = [v["id"] for v in new[MAX_POSTS_PER_SOURCE:]]
    yt_state["seen_ids"] = list(dict.fromkeys(posted_ids + skipped + yt_state.get("seen_ids", [])))[:200]


# --------------------------------------------------------------------------- #
# Steam Workshop change notes
# --------------------------------------------------------------------------- #

ENTRY_RE = re.compile(
    r'<div class="changelog headline">\s*(?P<headline>.*?)\s*</div>'
    r'.*?<p id="(?P<ts>\d+)">(?P<body>.*?)</p>\s*(?:<div class="commentsLink|</div>)',
    re.S,
)
TITLE_RE = re.compile(r'<div class="workshopItemTitle">(.*?)</div>', re.S)


def steam_html_to_markdown(fragment):
    """Turn Steam's rendered BBCode HTML into Discord-flavoured markdown."""
    s = fragment
    s = re.sub(r"[\t\r]", "", s)
    s = s.replace("\n", "")
    # links: keep the visible text (Steam shows the full URL as the text)
    s = re.sub(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
               lambda m: _link(m.group(1), m.group(2)), s, flags=re.S)
    # headings
    s = re.sub(r'<div class="bb_h[1-3]">(.*?)</div>', r"\n**\1**\n", s, flags=re.S)
    # list items
    s = re.sub(r"(<br\s*/?>\s*)+</li>", "</li>", s)
    s = re.sub(r"<li[^>]*>", "\n• ", s)
    s = re.sub(r"</li>", "", s)
    s = re.sub(r"</?ul[^>]*>|</?ol[^>]*>", "\n", s)
    # inline formatting
    s = re.sub(r"</?(b|strong)>", "**", s)
    s = re.sub(r"</?(i|em)>", "*", s)
    s = re.sub(r"</?u>", "__", s)
    s = re.sub(r"</?(strike|s)>", "~~", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<hr[^>]*>", "\n───\n", s)
    s = re.sub(r"</?(div|p|span|blockquote|code|pre)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)          # anything left
    s = html.unescape(s)
    # tidy whitespace
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(lines)
    s = re.sub(r"\n• \n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.replace("****", "")
    return s.strip()


def _link(href, text):
    href = html.unescape(href)
    m = re.match(r"https?://steamcommunity\.com/linkfilter/\?u=(.*)", href)
    if m:
        href = urllib.request.unquote(m.group(1))
    text = re.sub(r"<[^>]+>", "", text).strip()
    text = html.unescape(text)
    if not text or text == href:
        return href
    return f"[{text}]({href})"


def parse_changelog(page_html):
    """Return (mod_title, [ {ts, headline, notes} ]) newest first."""
    title_m = TITLE_RE.search(page_html)
    title = html.unescape(title_m.group(1).strip()) if title_m else None
    entries = []
    for m in ENTRY_RE.finditer(page_html):
        entries.append({
            "ts": int(m.group("ts")),
            "headline": html.unescape(re.sub(r"\s+", " ", m.group("headline"))).strip(),
            "notes": steam_html_to_markdown(m.group("body")),
        })
    entries.sort(key=lambda e: e["ts"], reverse=True)
    return title, entries


STEAM_API_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
CACHE_LAG_GRACE = 3 * 3600   # keep re-checking the page this long after an update shows in the API


def get_workshop_update_times(mod_ids):
    """Ask Steam's Web API when each item was last updated -> {id: time_updated}.

    This API is a different service from the community pages and is not
    rate-limited the same way, so it's safe to call on every run. The
    change-notes page is only fetched when this says something changed.
    """
    form = {"itemcount": str(len(mod_ids))}
    for i, mid in enumerate(mod_ids):
        form[f"publishedfileids[{i}]"] = mid
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(STEAM_API_DETAILS, data=data, method="POST",
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out = {}
    for d in payload.get("response", {}).get("publishedfiledetails", []):
        if d.get("result") == 1 and d.get("time_updated"):
            out[str(d["publishedfileid"])] = int(d["time_updated"])
    return out


def fetch_changelog_page(url):
    """Fetch a change-notes page, waiting and retrying if Steam says 429."""
    delays = [20, 45]
    for attempt in range(len(delays) + 1):
        try:
            return http_get(f"{url}?l=english&_={int(time.time())}")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < len(delays):
                wait = delays[attempt]
                try:
                    wait = max(wait, int(e.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                log(f"Steam rate limit (429), waiting {wait}s and retrying")
                time.sleep(wait)
                continue
            raise


def check_workshop(cfg, state, webhook, dry_run):
    mods = [m for m in (cfg.get("workshop_mods") or []) if str(m.get("id", "")).strip().isdigit()]
    for m in cfg.get("workshop_mods") or []:
        if m not in mods:
            log(f"Workshop: skipping mod with bad id: {m!r}")
    if not mods:
        return
    ws_state = state.setdefault("workshop", {})

    try:
        api_times = get_workshop_update_times([str(m["id"]).strip() for m in mods])
    except Exception as e:
        log(f"Workshop: Steam API unavailable ({e}); checking change-notes pages directly")
        api_times = {}

    now = int(time.time())
    for mod in mods:
        mod_id = str(mod["id"]).strip()
        name = mod.get("name") or mod_id
        mstate = ws_state.setdefault(mod_id, {})
        api_updated = api_times.get(mod_id)

        # Skip the page entirely if the API says nothing changed since we last looked.
        if api_updated and mstate.get("checked_update") and api_updated <= mstate["checked_update"]:
            log(f"Workshop {name}: no new updates")
            continue

        url = f"https://steamcommunity.com/sharedfiles/filedetails/changelog/{mod_id}"
        try:
            title, entries = parse_changelog(fetch_changelog_page(url))
        except Exception as e:
            log(f"Workshop {name}: could not read change notes ({e}); will try again next run")
            continue

        name = mod.get("name") or title or mod_id
        if not entries:
            log(f"Workshop {name}: no change notes found on page (is the item public?)")
            continue

        newest = entries[0]["ts"]
        last_ts = mstate.get("last_ts")
        if last_ts is None:
            mstate["last_ts"] = newest
            if api_updated:
                mstate["checked_update"] = api_updated
            log(f"Workshop {name}: first run, remembered latest update {newest} (nothing posted)")
            continue

        new = [e for e in entries if e["ts"] > last_ts]
        if not new:
            log(f"Workshop {name}: no new change notes on page (newest: {newest} {entries[0]['headline']}; last posted: {last_ts}; API updated: {api_updated})")
        all_posted = True
        for e in list(reversed(new[:MAX_POSTS_PER_SOURCE])):  # oldest first
            notes = e["notes"] or "*(No change notes were written for this update.)*"
            embed = {
                "title": f"🛠️ {name} updated",
                "url": url,
                "description": truncate(notes, EMBED_DESC_LIMIT),
                "color": int(str(mod.get("color", cfg.get("changelog_color", "E67E22"))).lstrip("#"), 16),
                "timestamp": datetime.fromtimestamp(e["ts"], tz=timezone.utc).isoformat(),
                "footer": {"text": "Steam Workshop change notes"},
            }
            payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
            log(f"Workshop {name}: new update {e['ts']} ({e['headline']})")
            if dry_run:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                ok = True
            elif not webhook:
                log("Workshop: DISCORD_WEBHOOK_CHANGELOG is not set, skipping post")
                return
            else:
                ok = post_to_discord(webhook, payload)
            if not ok:
                all_posted = False
                break
            mstate["last_ts"] = e["ts"]

        # Mark this API update as handled, unless the page looks like it's still
        # catching up (API shows a newer update than the page) - then look again
        # next run, for up to CACHE_LAG_GRACE.
        if api_updated and all_posted:
            page_behind = newest < api_updated - 60
            if not page_behind or now - api_updated > CACHE_LAG_GRACE:
                mstate["checked_update"] = api_updated


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    dry_run = "--dry-run" in sys.argv
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})

    check_youtube(cfg, state, os.environ.get("DISCORD_WEBHOOK_UPLOADS", "").strip(), dry_run)
    check_workshop(cfg, state, os.environ.get("DISCORD_WEBHOOK_CHANGELOG", "").strip(), dry_run)

    # Keep the repo "active" so GitHub doesn't pause the schedule after 60 quiet days.
    now = int(time.time())
    if now - int(state.get("keepalive", 0)) > KEEPALIVE_DAYS * 86400:
        state["keepalive"] = now

    if not dry_run:
        save_json(STATE_PATH, state)
    log("Done")


if __name__ == "__main__":
    main()
