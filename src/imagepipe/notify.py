"""Send notifications through an Apprise API server (which forwards to ntfy etc.)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from imagepipe.config import NotifyConfig

log = logging.getLogger("imagepipe")

TYPES = {"info", "success", "warning", "failure"}


def send(cfg: NotifyConfig, event: str, title: str, body: str, kind: str = "info") -> bool:
    """POST to <apprise_url>/notify/<apprise_key>. Never raises; returns True on success."""
    if not cfg.enabled or event not in cfg.on:
        return False
    url = f"{cfg.apprise_url.rstrip('/')}/notify/{cfg.apprise_key}"
    payload = {"title": title, "body": body, "type": kind if kind in TYPES else "info"}
    if cfg.tags:
        payload["tag"] = cfg.tags
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Notification failed: %s", exc)
        return False
    if not ok:
        log.warning("Notification failed: HTTP %s", resp.status)
    return ok
