import os
import logging
import urllib.request
import urllib.error
import json

logger = logging.getLogger("bea-tidy")


def _post_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def _post_text(url, text, headers=None):
    data = text.encode()
    req  = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "text/plain")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def send_file_added(ideal_path):
    """Fire a per-file notification when a file is successfully added to Plex."""
    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    ntfy_url    = os.getenv("NTFY_URL", "").strip()

    if not discord_url and not ntfy_url:
        return

    category = ideal_path.split("/")[0] if "/" in ideal_path else ""
    icons = {
        "Movies":       "🎬",
        "Anime Movies": "🎬",
        "TV":           "📺",
        "Anime Series": "📺",
    }
    icon  = icons.get(category, "📁")
    title = f"{icon} Added to Plex"

    if discord_url:
        try:
            colours = {
                "Movies":       0x5865F2,
                "Anime Movies": 0xEB459E,
                "TV":           0x57F287,
                "Anime Series": 0xFEE75C,
            }
            payload = {
                "embeds": [{
                    "title":       title,
                    "description": f"`{ideal_path}`",
                    "color":       colours.get(category, 0x95A5A6),
                }]
            }
            _post_json(discord_url, payload)
        except Exception as e:
            logger.warning(f"Discord file-added notification failed: {e}")

    if ntfy_url:
        try:
            _post_text(ntfy_url, ideal_path, {"Title": title, "Tags": "white_check_mark"})
        except Exception as e:
            logger.warning(f"ntfy file-added notification failed: {e}")


def send_run_summary(processed, skipped, failed, failures=None):
    """
    Fire run summary to Discord and/or ntfy, whichever env vars are set.
    Silent no-op if neither DISCORD_WEBHOOK_URL nor NTFY_URL is configured.
    """
    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    ntfy_url    = os.getenv("NTFY_URL", "").strip()

    if not discord_url and not ntfy_url:
        return

    status_emoji = "✅" if failed == 0 else "⚠️"
    title        = f"{status_emoji} bea-tidy run complete"
    summary      = f"Processed: {processed} | Skipped: {skipped} | Failed: {failed}"
    failure_text = ""
    if failures:
        failure_text = "\n**Failed files:**\n" + "\n".join(f"• {f}" for f in failures)

    if discord_url:
        try:
            colour  = 0x57F287 if failed == 0 else 0xFEE75C  # green or yellow
            payload = {
                "embeds": [{
                    "title":       title,
                    "description": summary + (failure_text.replace("**", "") if failure_text else ""),
                    "color":       colour,
                }]
            }
            _post_json(discord_url, payload)
            logger.info("Discord notification sent.")
        except urllib.error.URLError as e:
            logger.warning(f"Discord notification failed: {e}")
        except Exception as e:
            logger.warning(f"Discord notification error: {e}")

    if ntfy_url:
        try:
            body    = summary + ("\n\nFailed files:\n" + "\n".join(failures) if failures else "")
            headers = {
                "Title":    title,
                "Priority": "default" if failed == 0 else "high",
                "Tags":     "white_check_mark" if failed == 0 else "warning",
            }
            _post_text(ntfy_url, body, headers)
            logger.info("ntfy notification sent.")
        except urllib.error.URLError as e:
            logger.warning(f"ntfy notification failed: {e}")
        except Exception as e:
            logger.warning(f"ntfy notification error: {e}")
