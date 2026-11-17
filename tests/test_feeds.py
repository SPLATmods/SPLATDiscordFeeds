import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feeds

html_text = (Path(__file__).parent / "sample_changelog.html").read_text()
title, entries = feeds.parse_changelog(html_text)
assert title == "SPLATAC", title
assert [e["ts"] for e in entries] == [1794246276, 1793000000], entries
assert entries[0]["headline"] == "Update: Nov 16 @ 9:44pm"
n = entries[0]["notes"]
print(n); print("---")
assert "**Update 1.2**" in n and "• Plates: new texture & \"stuff\"." in n and "https://github.com/SPLATmods" in n and "linkfilter" not in n
assert entries[1]["notes"] == ""

feed = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
<entry><yt:videoId>AAA</yt:videoId><title>Older</title><link rel="alternate" href="https://www.youtube.com/watch?v=AAA"/><published>2026-11-01T00:00:00+00:00</published></entry>
<entry><yt:videoId>BBB</yt:videoId><title>Newest &amp; best</title><link rel="alternate" href="https://www.youtube.com/watch?v=BBB"/><published>2026-11-10T00:00:00+00:00</published></entry>
</feed>'''
v = feeds.parse_youtube_feed(feed)
assert [x["id"] for x in v] == ["BBB", "AAA"] and v[0]["title"] == "Newest & best"

# end-to-end flow with fake network + fake discord
posts = []
feeds.is_youtube_short = lambda vid: vid == "CCC"
feeds.post_to_discord = lambda url, p: posts.append((url, p)) or True
pages = {"yt": feed, "steam": html_text}
feeds.http_get = lambda url, timeout=30: pages["yt"] if "youtube" in url else pages["steam"]
cfg = {"youtube": {"channel_id": "UCx"}, "workshop_mods": [{"id": "123", "name": "SPLATAC"}]}
state = {}
feeds.check_youtube(cfg, state, "W1", False); feeds.check_workshop(cfg, state, "W2", False)
assert posts == [], "first run must not post"
# new video + new steam update
pages["yt"] = feed.replace("<entry>", '<entry><yt:videoId>CCC</yt:videoId><title>Brand new</title><published>2026-11-17T00:00:00+00:00</published></entry><entry>', 1)
pages["steam"] = html_text.replace('<div class="detailBox', '<div class="changelog headline">Update: Nov 17</div><p id="1795000000">Hotfix<br></p><div class="commentsLink"></div><div class="detailBox', 1)
feeds.check_youtube(cfg, state, "W1", False); feeds.check_workshop(cfg, state, "W2", False)
assert len(posts) == 2, posts
assert posts[0][1]["content"].startswith("📱 **New Short!**") and "youtube.com/shorts/CCC" in posts[0][1]["content"], posts[0]
assert posts[1][1]["embeds"][0]["description"] == "Hotfix", posts[1]
# nothing new -> nothing posted
feeds.check_youtube(cfg, state, "W1", False); feeds.check_workshop(cfg, state, "W2", False)
assert len(posts) == 2
print(json.dumps(posts, indent=1)[:800])
print("ALL TESTS PASSED")

# ---- Steam API gating + 429 retry ----
import urllib.error
calls = {"page": 0}
def fake_get(url, timeout=30):
    if "youtube" in url: return pages["yt"]
    calls["page"] += 1
    return pages["steam"]
feeds.http_get = fake_get
newest = 1795000000
feeds.get_workshop_update_times = lambda ids: {"123": newest}
state2 = json.loads(json.dumps(state)); posts.clear(); calls["page"] = 0
feeds.check_workshop(cfg, state2, "W2", False)          # page fetched once, nothing new, API update marked handled
assert calls["page"] == 1 and posts == [] and state2["workshop"]["123"]["checked_update"] == newest, state2
feeds.check_workshop(cfg, state2, "W2", False)          # API unchanged -> page NOT fetched
assert calls["page"] == 1, calls
# new update appears in API but the page is still cached/old -> no post, keep checking
feeds.get_workshop_update_times = lambda ids: {"123": 1796000000}
import time as _t; feeds.time.time = lambda: 1796000100
feeds.check_workshop(cfg, state2, "W2", False)
assert posts == [] and state2["workshop"]["123"]["checked_update"] == newest
# page catches up -> posts once, then marked handled
pages["steam"] = pages["steam"].replace('<div class="changelog headline">Update: Nov 17</div>', '<div class="changelog headline">Update: Nov 18</div><p id="1796000000">Temp gauge fix<br></p><div class="commentsLink"></div><div class="changelog headline">Update: Nov 17</div>', 1)
feeds.check_workshop(cfg, state2, "W2", False)
assert len(posts) == 1 and posts[0][1]["embeds"][0]["description"] == "Temp gauge fix" and state2["workshop"]["123"]["checked_update"] == 1796000000, (posts, state2)
feeds.check_workshop(cfg, state2, "W2", False)
assert len(posts) == 1
# 429 then success
seq = [urllib.error.HTTPError("u", 429, "x", {}, None), "<html></html>"]
def flaky(url, timeout=30):
    r = seq.pop(0)
    if isinstance(r, Exception): raise r
    return r
feeds.http_get = flaky; feeds.time.sleep = lambda s: None
assert feeds.fetch_changelog_page("https://x") == "<html></html>"
print("API GATING + RETRY TESTS PASSED")
