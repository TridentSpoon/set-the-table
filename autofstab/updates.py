"""Check GitHub for a newer release of Set the Table.

Deliberately stdlib-only (urllib, json) so installing the app never needs
extra Python packages, and deliberately manual -- nothing here runs unless
the user clicks "Check for Updates", so the app never phones home on its
own just from being launched.
"""

import json
import urllib.error
import urllib.request
from typing import NamedTuple, Optional, Tuple

# The one place to change if the project moves.
GITHUB_OWNER = "TridentSpoon"
GITHUB_REPO = "set-the-table"

RELEASES_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

# Statuses a check can end in.
UP_TO_DATE = "up-to-date"
UPDATE_AVAILABLE = "update-available"
NO_RELEASES = "no-releases"
ERROR = "error"


class UpdateResult(NamedTuple):
    status: str
    latest_version: Optional[str]
    message: str
    url: str = RELEASES_URL


def _version_tuple(text: str) -> Tuple[int, ...]:
    """Turn a version/tag string into comparable numbers.

    Tolerates the usual tag shapes ("v1.2.0", "1.2", "1.2.0-beta") by
    taking the leading digits of each dot-separated part; anything
    non-numeric contributes 0 rather than blowing up, since a failed
    update check should never be worse than a quiet "couldn't tell".
    """
    parts = []
    for chunk in text.strip().lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """True if `latest` is a strictly higher version than `current`."""
    a, b = _version_tuple(latest), _version_tuple(current)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


def check_for_update(current_version: str, timeout: float = 10.0) -> UpdateResult:
    """Ask GitHub for the latest published release and compare versions.

    Blocks on network I/O, so callers in the GUI must run this off the
    main thread or the window freezes while it waits.
    """
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub's API rejects requests without a User-Agent.
            "User-Agent": f"set-the-table/{current_version}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return UpdateResult(
                NO_RELEASES, None,
                "No releases have been published yet, so there's nothing newer to install.",
            )
        if e.code in (403, 429):
            return UpdateResult(
                ERROR, None,
                "GitHub is rate-limiting update checks right now. Try again a bit later.",
            )
        return UpdateResult(ERROR, None, f"GitHub returned an error (HTTP {e.code}).")
    except urllib.error.URLError as e:
        return UpdateResult(ERROR, None, f"Couldn't reach GitHub — check your connection. ({e.reason})")
    except (TimeoutError, OSError) as e:
        return UpdateResult(ERROR, None, f"Couldn't reach GitHub — check your connection. ({e})")
    except json.JSONDecodeError:
        return UpdateResult(ERROR, None, "GitHub sent a response this app couldn't read.")

    latest = (payload.get("tag_name") or payload.get("name") or "").strip()
    if not latest:
        return UpdateResult(ERROR, None, "GitHub didn't report a version for the latest release.")

    url = payload.get("html_url") or RELEASES_URL

    if is_newer(latest, current_version):
        return UpdateResult(
            UPDATE_AVAILABLE, latest,
            f"Version {latest} is available — you have {current_version}.",
            url,
        )
    return UpdateResult(
        UP_TO_DATE, latest,
        f"You're up to date — {current_version} is the latest version.",
        url,
    )
