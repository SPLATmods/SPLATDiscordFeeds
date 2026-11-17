# SPLAT Discord feeds

Posts to the SPLAT Discord automatically:

- **New YouTube uploads** → `#new-uploads`
- **Steam Workshop change notes** for each mod → `#changelog`

It runs on GitHub Actions every 15 minutes, for free (public repo). It remembers what it has already posted in `state.json`, so nothing gets posted twice. On the very first run it only *remembers* your existing videos and updates; it starts posting from the next new one.

## Setup (one time)

1. **Create a new public repo** on GitHub, e.g. `SPLATmods/splat-discord-feeds`, and upload everything in this folder (including the hidden `.github` folder). Public repos get unlimited Actions minutes; a private repo would run out on the free plan at this schedule.
2. **Add your Steam Workshop IDs** in `config.json`. The ID is the number at the end of the mod's Workshop link, e.g. `https://steamcommunity.com/sharedfiles/filedetails/?id=1234567890` → `1234567890`.
3. **Add the two Discord webhooks as secrets.** In the repo: **Settings → Secrets and variables → Actions → New repository secret**
   - `DISCORD_WEBHOOK_UPLOADS` = webhook URL for `#new-uploads`
   - `DISCORD_WEBHOOK_CHANGELOG` = webhook URL for `#changelog`

   (Get each URL in Discord: channel ⚙️ **Edit Channel → Integrations → Webhooks →** pick the webhook → **Copy Webhook URL**.) Treat these like passwords: anyone with the URL can post in that channel.
4. **Turn it on:** go to the **Actions** tab, enable workflows if asked, open **Discord feeds**, and click **Run workflow**. The first run just saves `state.json`. After that it runs on its own.

## Adding another mod later

Add a line to `workshop_mods` in `config.json`:

```json
{ "name": "SplatVests", "id": "1234567890" }
```

Its existing change notes are skipped; only updates after that point get posted.

## Changing how posts look

- YouTube message: `youtube.message` in `config.json` (`{title}` and `{url}` get filled in).
- Changelog embed colour: `changelog_color` (hex), or `"color"` on a single mod.

## Notes

- Write your Workshop change notes as you normally would — headings, bold, bullet lists, and links all carry over.
- Very long notes are cut at ~4,000 characters with a link to the full notes on Steam.
- Shorts and premieres show up in the YouTube feed too, so they'll be posted as well.
- GitHub's schedule can run a few minutes late during busy times.
- If Steam or YouTube is down, the run just logs it and tries again next time.

Test locally without posting anything: `python3 feeds.py --dry-run`
