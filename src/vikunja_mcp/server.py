"""
Vikunja MCP Server

MCP server that gives Claude full access to your Vikunja task management instance.
Works with any Vikunja instance - self-hosted, cloud, or local.

AUTO-GENERATED — edits to this file are overwritten by the next extraction.
It is extracted from a larger private server, so the fix for anything wrong here
has to be made upstream. Please open an issue rather than a PR against this file.

Source: https://github.com/ivantohelpyou/vikunja-mcp
PyPI: https://pypi.org/project/vikunja-mcp/
"""

import bisect
import contextvars
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import base64
import hashlib
import hmac
from vikunja_mcp.defer_logic import (  # noqa: E402
    _DEFER_META_PATTERN,
    _extract_defer_meta,
    _write_defer_meta,
    _task_defer_state,
    _defer_count,
)
from starlette.responses import JSONResponse, RedirectResponse, HTMLResponse, Response
import json

import markdown
import yaml
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from pydantic import Field
import requests
from starlette.requests import Request

logger = logging.getLogger("vikunja-mcp")
logger.setLevel(logging.DEBUG if os.environ.get("VIKUNJA_DEBUG") else logging.INFO)

# Only add handler if not already configured (avoid duplicate logs)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)

# Performance tracking: set VIKUNJA_PERF=1 to log API call times
PERF_LOGGING = os.environ.get("VIKUNJA_PERF", "").lower() in ("1", "true", "yes")

# The running package version, read from the installed wheel's metadata.
#
# Sourced from the built distribution (not pyproject.toml), so it reflects what is
# actually deployed — a stale/cached wheel reports the stale version, which is exactly
# the signal we want when verifying a deploy landed. Falls back to "unknown" when the
# package isn't installed as distribution metadata (e.g. run straight from source
# without an editable install).
#
# Resolved ONCE at import rather than per call. A running process cannot change the
# wheel underneath itself, so this is equivalent — and it has to be a module-level
# constant rather than a function call, because `mcp = FastMCP(...)` below needs it and
# the public extraction emits every @PUBLIC_SECTION ahead of every function.
try:
    from importlib.metadata import version as _importlib_version
    try:
        _SERVER_VERSION = _importlib_version("vikunja-mcp")
    except Exception:
        _SERVER_VERSION = "unknown"
except Exception:
    _SERVER_VERSION = "unknown"

_current_vikunja_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    '_current_vikunja_token', default=None
)

# Companion override for per-request Vikunja base URL (fa-bglr.7). When BOTH this and
# _current_vikunja_token are set, _get_instance_config returns them regardless of the
# requested instance name — so the today/triage/assign engine runs as the requesting
# CALENDAR user (their own Vikunja account), not the configured owner/bot instance.
# Request-scoped: the calendar endpoint sets these and resets them in a finally.
_current_vikunja_url: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    '_current_vikunja_url', default=None
)

_allow_instance_fallback: contextvars.ContextVar[bool] = contextvars.ContextVar(
    '_allow_instance_fallback', default=False
)

# Context variable for current user ID (Matrix/Slack user)
# This allows instance-aware functions to look up user-specific config from PostgreSQL
_current_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    '_current_user_id', default=None
)

# Context variable for bot mode (vikunja_chat_with_claude / @eis)
# When True, _request() uses env vars (VIKUNJA_URL + VIKUNJA_BOT_TOKEN) instead of YAML config
# This separates bot operations from MCP multi-instance configuration (solutions-zja1)
_bot_mode: contextvars.ContextVar[bool] = contextvars.ContextVar(
    '_bot_mode', default=False
)

# Context variable for requesting user (who triggered @eis)
# Used to auto-share newly created projects with the requester
_requesting_user: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    '_requesting_user', default=None
)
_requesting_user_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    '_requesting_user_id', default=None
)

# Context variables for project queue batching (solutions-eofy)
# Accumulates projects during one LLM turn, flushes at end
_pending_projects: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    '_pending_projects', default=None
)
_next_temp_id: contextvars.ContextVar[int] = contextvars.ContextVar(
    '_next_temp_id', default=-1
)

# Request-scoped instance override. Set by write wrappers (task_update, task_delete,
# task_move, batch ops, etc.) for the duration of one call so an explicit instance=
# argument wins over the sticky mcp_context default. Without this, those wrappers used
# _set_current_instance() (which sets `current_instance`), but _get_current_instance()
# checks mcp_context.instance FIRST — so edits silently routed to the active-context
# token instead of the requested instance, causing cross-instance 403s.
_forced_instance: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    '_forced_instance', default=None
)

KANBAN_TEMPLATES = {
    "gtd": [
        {"title": "📥 Inbox", "position": 10},
        {"title": "⏭️ Next", "position": 20, "limit": 5},
        {"title": "⏸️ Waiting", "position": 30},
        {"title": "💭 Someday", "position": 40},
        {"title": "✅ Done", "position": 50}
    ],
    "sprint": [
        {"title": "📋 Backlog", "position": 10},
        {"title": "📝 To Do", "position": 20},
        {"title": "🚀 In Progress", "position": 30, "limit": 3},
        {"title": "👀 Review", "position": 40, "limit": 2},
        {"title": "✅ Done", "position": 50}
    ],
    "kitchen": [
        {"title": "💡 Idea", "position": 10},
        {"title": "📝 To-Do", "position": 20},
        {"title": "📋 Planned", "position": 30},
        {"title": "🥣 Mise en Place", "position": 40},
        {"title": "🧊 Standby", "position": 50},
        {"title": "🔥 Cooking/Baking", "position": 60},
        {"title": "🎨 Decorating", "position": 70},
        {"title": "📦 Ready", "position": 80},
        {"title": "✅ Done", "position": 90}
    ],
    "payables": [
        {"title": "🤔 Decision Queue", "position": 10},
        {"title": "✅ Approved - Timing Payment", "position": 20},
        {"title": "💰 Paid", "position": 30}
    ],
    "talks": [
        {"title": "💡 Ideas", "position": 10},
        {"title": "📤 Submitted", "position": 20},
        {"title": "✅ Accepted", "position": 30},
        {"title": "📝 Preparing", "position": 40, "limit": 2},
        {"title": "🎤 Delivered", "position": 50}
    ]
}

CONFIG_DIR = Path(os.environ.get("VIKUNJA_MCP_CONFIG_DIR", str(Path.home() / ".vikunja-mcp")))
CONFIG_FILE = CONFIG_DIR / "config.yaml"

mcp = FastMCP(
    "vikunja",
    version=_SERVER_VERSION,
    instructions="""Task and calendar management system. This is the user's primary calendar and task manager.

CALENDAR: Use cal_* tools for all calendar operations. The user's Google Calendar and Outlook
are connected as external calendars — use cal_schedule() to see their full schedule (Vikunja
tasks + Google Calendar + Outlook combined). Do NOT look for a separate Google Calendar
integration. Use cal_add_event() to add events. Use cal_get_url() for subscription URLs.

TIMEZONE: cal_schedule() returns all times ALREADY converted to the user's local timezone.
Present times exactly as returned — do NOT convert or adjust them. The response includes
'timezone', 'today', and 'now' fields for context.

TODAY: When the user asks the open-ended daily question — "what should I do today?",
"what should I work on?", "what's on my plate", "help me plan my day" — call today_actions()
FIRST. It returns scored, clustered candidate actions (Must clear / Quick wins / Move a goal /
Context batches / Been waiting), each with a `why` trace, already timezone-correct. Present the
clusters as-is; do NOT fall back to raw task lists or task_query for this question. Then use
today_apply(task_id) / today_snooze(task_id) to act on the user's swipe-right / swipe-left
choices, and today_set_weights() to re-tune ranking if the user says the ordering is off.

ROUTINES: habits with a quota per day/week ("cardio 3x/week", "calcium every day") are
Factum Erit-native routine goals, NOT Vikunja tasks. "log cardio", "did yoga yesterday
(45 min)", "how am I doing this week?", "add a routine" → routine_log / routine_undo /
routine_status / routine_set. Never create repeating Vikunja tasks for a routine.

TASKS: Use task_* tools for narrower lookups. For a specific quick query ("what's due today?",
"what's overdue?", "give me a count"), use task_query(query='today'|'overdue'|'urgent'|'summary').
Prefer today_actions() over task_query for the broad "what should I do" question above.

INSTANCES: The user has multiple Vikunja instances (e.g., 'personal', 'business'). Each
project belongs to exactly one instance — use project_list_all() to see which. CRITICAL:
When writing to a project, you MUST use ctx_set(instance='...') first to switch to the
correct instance, or pass instance='...' on tools that support it. Using the wrong instance
token causes 403 Forbidden errors. If you get a 403 on a write, check whether the active
instance matches the project's instance before retrying.

DISCOVERY: Call help() to see all available tool domains. Call cal_help() or task_help()
for detailed tool listings and common workflows.

Tool prefixes: today_, cal_, task_, project_, view_, kanban_, batch_, label_, config_, instance_,
ctx_, comment_, search_. All tools are in this MCP server — do not look for external calendar
or task integrations."""
)

_project_instance_cache: dict = {}
_PROJECT_INSTANCE_CACHE_TTL_SECONDS = 60

# Default colors for special labels (used on first-time creation)
_SPECIAL_LABEL_COLORS = {
    "calendar": "#4285F4",
    "calendar-busy": "#4caf50",
    "calendar-private": "#9C27B0",
    "today": "#FF7043",  # today-actions (fa-gptz): swipe-right "do today" marker
    "anno": "#B8860B",  # fa-20lq: per-task opt-in to yearly auto-rollover of past-due occasions
}

_SPECIAL_LABEL_NAMES = frozenset(_SPECIAL_LABEL_COLORS.keys())

RESERVED_LABEL_KEYWORDS = {
    "calendar": "Surfaces the task on the calendar and in ICS feeds.",
    "calendar-busy": "Marks the calendar event busy/opaque.",
    "calendar-private": "Marks the calendar event private.",
    "today": "today-actions 'do today' marker (fa-gptz).",
    "anno": "Yearly auto-rollover — a past-due occasion rolls to next year (fa-20lq).",
}
# Invariant: a reserved label title is exactly a config-backed special label.
# If these diverge, the special-label resolver and the label_create guard would
# disagree, so keep them in lockstep (asserted at import).
assert set(RESERVED_LABEL_KEYWORDS) == _SPECIAL_LABEL_NAMES, (
    "RESERVED_LABEL_KEYWORDS must stay in lockstep with _SPECIAL_LABEL_NAMES"
)

# fa-20lq: 'anno' is the per-task opt-in label for yearly auto-rollover. When a
# task carries this label and its due date has fully passed, the poller sweep
# rolls it forward to the next occurrence (see _sweep_anno_rollovers_impl). The
# roll math is calendar-aware (same month/day next year), which — unlike relying
# on Vikunja's repeat_after=365d — never drifts across leap years.
_ANNO_LABEL = "anno"

# Canonical month/day is stashed as an HTML comment in the description so the
# TRUE date survives a Feb-29 -> Feb-28 clamp: real leap years still land on the
# 29th. HTML comments are stripped by the DOMPurify read path, so it's invisible
# in the UI.
_ANNO_MARKER_RE = re.compile(r"<!--\s*anno:(\d{2})-(\d{2})\s*-->")

_task_list_cache: dict = {}
_TASK_LIST_CACHE_TTL_SECONDS = 30  # 30 seconds - balances freshness vs speed

_TODAY_DEFAULT_WEIGHTS = {
    "W_OVERDUE": 10,      # per day overdue, capped at _W_OVERDUE_CAP
    "W_DUE_TODAY": 30,
    "W_PRIORITY": 6,      # priority 0..5 native scale
    "W_STALE": 4,         # staleness_bucket 0..3
    "W_GOAL": 8,
    "W_TIMEBLOCK": 12,
    "W_QUICK": 5,
    "W_DOOR": 60,         # irreversible-deadline peak; hyperbolic decay by _DOOR_HALFLIFE
    "W_DEFER": 15,        # per prior deferral: escalation bonus on return (fa-fhtl / spec 07 §3)
}
_W_OVERDUE_CAP = 50       # overdue dominates but never runs away
_W_DEFER_CAP = 45         # a 3x-deferred task lands in the overdue/door tier — deliberately
_STALE_WHY = ["", "7d+", "30d+", "90d+"]  # human label per staleness bucket
_DOOR_HALFLIFE = 7        # days at which the door bonus is half its peak (fa-3729 §1)

# Deferral reason taxonomy (fa-fhtl / spec 07). The picker sorts deferrals from
# defects: only `dread` is a true deferral (increments defer_count, needs a date).
_DEFER_REASONS = ("blocked", "too_big", "wrong_context", "not_mine", "dread")
_DEFER_COUNTING_REASONS = frozenset({"dread"})  # the only reasons that bump defer_count
_RULE_OF_THREE = 3        # on the 3rd `dread` defer, "Not today" → portfolio decision

_N_PER_CLUSTER = 7  # per-cluster cap — the §5 guardrail against surfacing "everything"

_OWNER_CLAIM_ACTOR = "__owner__"   # legacy owner/bot session with no user id (spec 06 "owner" mode)

_config_lock = threading.Lock()

# Wrapped in a section, not tagged individually: extract_public.py only emits tagged
# `def`/`async def` (see its line ~118), so a module constant and a `class` are both
# invisible to it. Without this the public package would ship _assign_apply_guarded and
# assign_apply while dropping the two names they close over, and every disposition would
# NameError at runtime (augment review, PR #197).

# The disposition vocabulary, named once. Both surfaces validate against this list.
ASSIGN_DISPOSITIONS = ("done", "today", "week", "someday", "delete")


class AssignRefused(Exception):
    """A disposition the shared guard refused. Carries the wire code and HTTP status so
    the calendar route and the MCP tool report the SAME refusal for the same reason."""

    def __init__(self, code: str, status: int = 403):
        super().__init__(code)
        self.code = code
        self.status = status

# Cache for ICS feed content with TTL (instance+label -> (content, timestamp)).
# Public because _invalidate_ics_cache is: every task mutation calls it, and without
# the backing dict it raises NameError AFTER the write has already landed (augment #250).
_ics_feed_cache: dict = {}
_ICS_CACHE_TTL_SECONDS = 300  # 5 minutes

# Cache for omnibus feed: instance -> (bytes, timestamp)
_omnibus_cache: dict = {}
_OMNIBUS_CACHE_TTL_SECONDS = 300  # 5 minutes

def _get_version() -> str:
    """The running package version. See `_SERVER_VERSION` for how it is resolved."""
    return _SERVER_VERSION


def _per_user_override() -> bool:
    """True when a per-user calendar request (fa-bglr.7) has pinned BOTH the URL and
    token override contextvars via `_acting_as_calendar_user`. While held:
      - every instance lookup (`_get_instance_config`) resolves to the requesting
        user's own Vikunja creds, regardless of the requested instance name; and
      - the task-fetch path MUST stay single-instance — the owner's multi-instance
        fan-out (`_fetch_*_from_all_instances`) would key results by the OWNER's
        instance names (dropping the user's tasks in the post-filter) and runs in
        ThreadPoolExecutor worker threads that don't inherit these contextvars."""
    return (
        _current_vikunja_url.get() is not None
        and _current_vikunja_token.get() is not None
    )


def mcp_tool_with_fallback(func):
    """Decorator for MCP tools that need instance token fallback.

    MCP/CLI tools don't have per-user authentication, so they need to use
    the instance token from VIKUNJA_TOKEN env var.

    SECURITY: Only use this decorator for MCP tools, never for user-facing handlers.
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        token = _allow_instance_fallback.set(True)
        try:
            return func(*args, **kwargs)
        finally:
            _allow_instance_fallback.reset(token)
    return wrapper


def _get_encryption_key() -> bytes:
    """Get or generate encryption key for API key storage.

    Uses VIKUNJA_MCP_ENCRYPTION_KEY env var if set, otherwise derives
    a key from a combination of stable machine identifiers.

    Returns:
        32-byte key suitable for Fernet encryption
    """
    key_from_env = os.environ.get("VIKUNJA_MCP_ENCRYPTION_KEY")
    if key_from_env:
        # Derive 32-byte key from provided value using SHA256
        return base64.urlsafe_b64encode(hashlib.sha256(key_from_env.encode()).digest())

    # Fallback: derive from hostname + config dir (stable per deployment)
    # Not cryptographically ideal but provides obfuscation for stored keys
    import socket
    stable_seed = f"{socket.gethostname()}:{CONFIG_DIR}"
    return base64.urlsafe_b64encode(hashlib.sha256(stable_seed.encode()).digest())


def _encrypt_api_key(api_key: str) -> str:
    """Encrypt an API key for storage.

    Args:
        api_key: Plaintext API key (e.g., sk-ant-xxx)

    Returns:
        Base64-encoded encrypted key
    """
    fernet = Fernet(_get_encryption_key())
    return fernet.encrypt(api_key.encode()).decode()


def _decrypt_api_key(encrypted: str) -> Optional[str]:
    """Decrypt a stored API key.

    Args:
        encrypted: Base64-encoded encrypted key

    Returns:
        Plaintext API key, or None if decryption fails
    """
    try:
        fernet = Fernet(_get_encryption_key())
        return fernet.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.warning(f"Failed to decrypt API key: {e}")
        return None


def _is_html(text: str) -> bool:
    """Check if text appears to be HTML (not markdown).

    Simple heuristic: starts with common HTML tags.
    """
    if not text:
        return False
    stripped = text.strip()
    html_starts = ('<p>', '<p ', '<div>', '<div ', '<ul>', '<ol>', '<h1>', '<h2>',
                   '<h3>', '<h4>', '<h5>', '<h6>', '<table>', '<blockquote>', '<!DOCTYPE')
    return stripped.lower().startswith(html_starts)


def md_to_html(text: str) -> str:
    """Convert markdown to HTML for Vikunja descriptions.

    If text is already HTML, returns it unchanged.
    """
    if not text:
        return text
    # If already HTML, don't convert
    if _is_html(text):
        return text
    return markdown.markdown(text)


def _sanitize_title(title: str) -> str:
    """Strip HTML tags from title, keeping only plain text.

    Security: Prevents HTML injection in task/project/label titles.
    Titles are displayed in UI and should not contain executable HTML.

    Args:
        title: Raw title string (may contain HTML)

    Returns:
        Sanitized title with HTML tags removed, max 256 chars

    Examples:
        >>> _sanitize_title("<script>alert(1)</script>Test")
        "Test"
        >>> _sanitize_title("<b>Bold Title</b>")
        "Bold Title"
    """
    if not title:
        return title
    # Remove script/style tags AND their content (security-critical)
    title = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', title, flags=re.IGNORECASE | re.DOTALL)
    # Remove all other HTML tags (keep content)
    title = re.sub(r'<[^>]+>', '', title)
    # Limit length (Vikunja has 250 char limit, use 256 for safety)
    return title[:256]


def _sanitize_description(desc: str) -> str:
    """Escape HTML entities in description before markdown conversion.

    Security: Prevents second-order HTML injection from Vikunja data.
    Users can inject arbitrary HTML via Vikunja web UI. We must escape
    it before converting markdown to HTML.

    Strategy: Accept markdown from user, escape any HTML in it, then
    convert markdown to HTML safely.

    Args:
        desc: Raw description string (may contain HTML and markdown)

    Returns:
        HTML-escaped description ready for markdown conversion

    Examples:
        >>> _sanitize_description("<script>alert(1)</script>")
        "&lt;script&gt;alert(1)&lt;/script&gt;"
        >>> _sanitize_description("**Bold** <b>HTML</b>")
        "**Bold** &lt;b&gt;HTML&lt;/b&gt;"
    """
    if not desc:
        return desc
    # Escape HTML entities (must be done BEFORE markdown conversion)
    import html
    return html.escape(desc)


def _md_to_slack_mrkdwn(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn format.

    Key differences:
    - Bold: **text** → *text*
    - Headers: ## Header → *Header* (Slack has no headers)
    - Links: [text](url) → <url|text>
    """
    if not text:
        return text
    # Convert markdown headers to bold (Slack has no header format)
    # Must be done before **bold** conversion to avoid double-processing
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # Convert **bold** to *bold* (Slack uses single asterisks)
    text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', text)
    # Convert [text](url) to <url|text>
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
    return text


def _load_config() -> dict:
    """Load project config from YAML file."""
    if not CONFIG_FILE.exists():
        return {"projects": {}, "instances": {}, "current_instance": None}
    try:
        with open(CONFIG_FILE, "r") as f:
            config = yaml.safe_load(f) or {}
            if "projects" not in config:
                config["projects"] = {}
            if "instances" not in config:
                config["instances"] = {}
            return config
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed config file: {e}")


def _save_config(config: dict) -> None:
    """Save project config to YAML file (atomic write)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file, then rename
    fd, temp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, CONFIG_FILE)
    except Exception:
        os.unlink(temp_path)
        raise


def _deep_merge(base: dict, updates: dict) -> dict:
    """Deep merge updates into base dict."""
    result = base.copy()
    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _get_instances() -> dict:
    """Get all configured Vikunja instances.

    Priority order:
    1. Config file instances (~/.vikunja-mcp/config.yaml)
    2. VIKUNJA_INSTANCES env var (JSON array)
    3. VIKUNJA_URL/VIKUNJA_TOKEN env vars as 'default'
    """
    config = _load_config()
    instances = dict(config.get("instances", {}))  # Copy to avoid mutating config

    # Parse VIKUNJA_INSTANCES env var (JSON array of {name, url, token})
    instances_json = os.environ.get("VIKUNJA_INSTANCES", "")
    if instances_json:
        try:
            import json
            instances_list = json.loads(instances_json)
            for inst in instances_list:
                name = inst.get("name", "").strip()
                url = inst.get("url", "").strip()
                token = inst.get("token", "").strip()
                if name and url and token:
                    if name not in instances:
                        instances[name] = {
                            "url": url.rstrip('/'),
                            "token": token
                        }
                    else:
                        # Config has this instance (e.g. external_calendars) — fill in
                        # url/token from env var so both sources are available.
                        if not instances[name].get("url"):
                            instances[name]["url"] = url.rstrip('/')
                        if not instances[name].get("token"):
                            instances[name]["token"] = token
        except (json.JSONDecodeError, TypeError):
            pass  # Invalid JSON, skip

    # Always include env var instance as 'default' if set (unless explicitly configured)
    env_url = os.environ.get("VIKUNJA_URL")
    env_token = os.environ.get("VIKUNJA_BOT_TOKEN") or os.environ.get("VIKUNJA_TOKEN")
    if env_url and env_token and "default" not in instances:
        instances["default"] = {
            "url": env_url.rstrip('/'),
            "token": env_token
        }

    return instances


def _get_current_instance() -> Optional[str]:
    """Get the name of the currently active instance.

    Priority:
    0. _forced_instance contextvar (explicit instance= arg on a write wrapper)
    1. mcp_context.instance (set by set_active_context tool)
    2. current_instance (set by switch_instance or config file)
    3. First configured instance as fallback
    """
    # Request-scoped explicit override wins over the sticky mcp_context default so a
    # write tool's instance= argument routes to the requested instance.
    forced = _forced_instance.get()
    if forced:
        return forced

    config = _load_config()

    # Check mcp_context FIRST - this is what set_active_context uses (solutions-c8sry)
    mcp_instance = config.get("mcp_context", {}).get("instance")
    if mcp_instance:
        return mcp_instance

    # Fall back to current_instance
    current = config.get("current_instance")

    # If no current instance set, check if we have instances configured
    if not current:
        instances = _get_instances()
        if instances:
            # Default to first instance or "default" if it exists
            if "default" in instances:
                return "default"
            return next(iter(instances.keys()))

    return current


def _set_current_instance(name: str) -> None:
    """Set the currently active instance."""
    instances = _get_instances()
    if name not in instances:
        available = ", ".join(instances.keys()) if instances else "none configured"
        raise ValueError(f"Instance '{name}' not found. Available: {available}")

    config = _load_config()
    config["current_instance"] = name
    _save_config(config)


def _get_instance_config(name: Optional[str] = None) -> tuple[str, str]:
    """Get URL and token for an instance.

    Args:
        name: Instance name, or None for current instance

    Returns:
        Tuple of (url, token)
    """
    # Per-request per-user override (fa-bglr.7): a calendar request acting AS a user
    # pins every instance lookup to that user's own Vikunja creds. Single-instance by
    # nature, so the requested `name` is intentionally ignored while the override holds.
    _ov_url = _current_vikunja_url.get()
    _ov_token = _current_vikunja_token.get()
    if _ov_url and _ov_token:
        return _ov_url.rstrip('/').strip(), _ov_token.strip()

    if name is None:
        name = _get_current_instance()

    if name is None:
        # Fall back to env vars
        url = os.environ.get("VIKUNJA_URL")
        token = os.environ.get("VIKUNJA_BOT_TOKEN") or os.environ.get("VIKUNJA_TOKEN")  # Optional - user tokens replace this
        if url:
            # URL is required, token is optional (user tokens stored per-user)
            # Strip whitespace from both (solutions-zja1)
            return url.rstrip('/').strip(), (token or "").strip()
        raise ValueError("No instance configured. Set VIKUNJA_URL or configure instances.")

    instances = _get_instances()
    if name not in instances:
        raise ValueError(f"Instance '{name}' not found")

    instance = instances[name]
    url = instance.get("url")
    token = instance.get("token")

    # Support env var references in token (e.g., "${VIKUNJA_CLOUD_TOKEN}")
    if token and token.startswith("${") and token.endswith("}"):
        env_var = token[2:-1]
        token = os.environ.get(env_var)
        if not token:
            raise ValueError(f"Environment variable {env_var} not set for instance '{name}'")

    if not url or not token:
        raise ValueError(f"Instance '{name}' missing url or token")

    return url.rstrip('/'), token


def _get_instance_timezone(name: Optional[str] = None) -> Optional[str]:
    """Get timezone for an instance (e.g., 'America/Los_Angeles').

    Returns None if no timezone configured for the instance.
    """
    if name is None:
        name = _get_current_instance()

    if name is None:
        return None

    instances = _get_instances()
    if name not in instances:
        return None

    return instances[name].get("timezone")


def _get_instance_token_expires(name: Optional[str] = None) -> Optional[str]:
    """Get token expiration date for an instance (e.g., '2026-11-07').

    Returns None if no expiration date configured.
    """
    if name is None:
        name = _get_current_instance()

    if name is None:
        return None

    instances = _get_instances()
    if name not in instances:
        return None

    return instances[name].get("token_expires")


def _get_effective_instance_config() -> tuple[str, str, str]:
    """Get instance config, preferring user context if available.

    This is the main function that tools should call to get instance config.
    It checks for user context (Matrix/Slack) and falls back to YAML config
    for MCP/CLI usage.

    Returns:
        Tuple of (instance_name, url, token)
    """
    user_id = _current_user_id.get()
    if user_id:
        # User context available - use PostgreSQL
        return _get_user_instance_config(user_id)
    else:
        # No user context - use YAML config (MCP/CLI mode)
        instance = _get_current_instance() or "default"
        url, token = _get_instance_config(instance)
        return instance, url, token


def _connect_instance(name: str, url: str, token: str, token_expires: str = "", timezone: str = "") -> dict:
    """Connect to a Vikunja instance (add to local config).

    Auto-switches to this instance if no current instance is set.

    Args:
        name: Instance name (e.g., 'personal', 'business')
        url: Base URL of the Vikunja instance
        token: API token
        token_expires: Optional expiration date (YYYY-MM-DD) for tracking
        timezone: Optional timezone for date conversion (e.g., 'America/Los_Angeles')
    """
    config = _load_config()
    if "instances" not in config:
        config["instances"] = {}

    # Check if we should auto-switch (no current instance)
    auto_switched = False
    if not config.get("current_instance"):
        config["current_instance"] = name
        auto_switched = True

    instance_config = {
        "url": url.rstrip('/'),
        "token": token
    }
    if token_expires:
        instance_config["token_expires"] = token_expires
    if timezone:
        instance_config["timezone"] = timezone

    config["instances"][name] = instance_config
    _save_config(config)

    result = {"name": name, "url": url, "connected": True}
    if auto_switched:
        result["switched_to"] = name
        result["note"] = "Auto-switched (first connection)"
    if not token_expires:
        result["hint"] = "If you'd like me to track token expiration, let me know the expiration date (YYYY-MM-DD)"
    else:
        result["token_expires"] = token_expires
    return result


def _disconnect_instance(name: str) -> dict:
    """Disconnect from a Vikunja instance (remove from local config only - no data deleted)."""
    config = _load_config()
    instances = config.get("instances", {})

    if name not in instances:
        raise ValueError(f"Instance '{name}' not found")

    del config["instances"][name]

    # If disconnecting current instance, clear current
    if config.get("current_instance") == name:
        config["current_instance"] = None

    _save_config(config)
    return {"name": name, "disconnected": True}


def _rename_instance(old_name: str, new_name: str) -> dict:
    """Rename a Vikunja instance, updating all references."""
    config = _load_config()
    instances = config.get("instances", {})

    if old_name not in instances:
        raise ValueError(f"Instance '{old_name}' not found")

    if new_name in instances:
        raise ValueError(f"Instance '{new_name}' already exists")

    # Copy instance config to new name
    config["instances"][new_name] = config["instances"][old_name]
    del config["instances"][old_name]

    # Update current_instance if needed
    if config.get("current_instance") == old_name:
        config["current_instance"] = new_name

    # Update xq section if needed
    if "xq" in config and old_name in config["xq"]:
        config["xq"][new_name] = config["xq"][old_name]
        del config["xq"][old_name]

    # Update projects section - any project with instance: old_name
    if "projects" in config:
        for project_id, project_config in config["projects"].items():
            if project_config.get("instance") == old_name:
                project_config["instance"] = new_name

    _save_config(config)
    return {"old_name": old_name, "new_name": new_name, "renamed": True}


def get_config():
    """Get Vikunja configuration (URL, token) for current instance."""
    return _get_instance_config()


def _connect_instance_impl(name: str, url: str, token: str) -> dict:
    """Connect a new Vikunja instance by validating and storing credentials.

    Args:
        name: Instance name (e.g., "personal", "work")
        url: Vikunja instance URL (e.g., "https://vikunja.example.com")
        token: API token from Vikunja Settings > API Tokens

    Returns:
        {success: True, name, url} or {error: str}
    """
    # Normalize URL
    url = url.rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"

    # Validate token by making a test API call (use /projects which definitely works)
    try:
        response = requests.request(
            "GET",
            f"{url}/api/v1/projects",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        if response.status_code == 401:
            return {"error": "Invalid token - check your API token in Vikunja Settings"}
        if response.status_code != 200:
            return {"error": f"API error: HTTP {response.status_code}"}

        # Token works - we don't get username from /projects, so just say "connected"
        username = "connected"
    except requests.exceptions.ConnectionError:
        return {"error": f"Cannot connect to {url} - check the URL"}
    except Exception as e:
        return {"error": f"Connection failed: {e}"}

    # Store in config
    config = _load_config()
    if "instances" not in config:
        config["instances"] = {}

    config["instances"][name] = {
        "url": url,
        "token": token
    }

    # If this is the first instance, set it as current
    if len(config["instances"]) == 1 or not config.get("current_instance"):
        config["current_instance"] = name

    _save_config(config)

    return {
        "success": True,
        "name": name,
        "url": url,
        "username": username
    }


def _disconnect_instance_impl(name: str) -> dict:
    """Disconnect a Vikunja instance by removing it from config.

    Args:
        name: Instance name to remove

    Returns:
        {success: True, name} or {error: str}
    """
    config = _load_config()
    instances = config.get("instances", {})

    if name not in instances:
        available = list(instances.keys()) if instances else []
        if available:
            return {"error": f"Instance '{name}' not found. Available: {', '.join(available)}"}
        return {"error": f"Instance '{name}' not found. No instances configured."}

    # Remove the instance
    del config["instances"][name]

    # If this was the current instance, switch to another or clear
    if config.get("current_instance") == name:
        remaining = list(config["instances"].keys())
        config["current_instance"] = remaining[0] if remaining else None

    _save_config(config)

    return {"success": True, "name": name}


def _find_projects_by_name(query: str) -> list:
    """Find projects matching query across all instances.

    Returns list of {instance, project_id, name, task_count} sorted by instance then name.
    Uses fuzzy matching (case-insensitive, substring).
    """
    instances = _get_instances()
    matches = []
    query_lower = query.lower()
    errors = []

    for instance_name in instances.keys():
        try:
            # Use _get_instance_config to properly resolve env var tokens
            url, token = _get_instance_config(instance_name)
        except ValueError as e:
            errors.append(f"{instance_name}: {e}")
            continue

        try:
            # Fetch projects from this instance
            response = requests.request(
                "GET",
                f"{url}/api/v1/projects",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                timeout=10
            )
            if response.status_code != 200:
                errors.append(f"{instance_name}: HTTP {response.status_code}")
                continue

            projects = response.json()

            # Recursively search through nested projects
            def search_projects(project_list, parent_path=""):
                for project in project_list:
                    title = project.get("title", "")
                    full_path = f"{parent_path} > {title}" if parent_path else title
                    # Fuzzy match: case-insensitive substring
                    if query_lower in title.lower():
                        matches.append({
                            "instance": instance_name,
                            "project_id": project.get("id"),
                            "name": title,
                            "path": full_path,
                        })
                    # Recurse into child projects
                    children = project.get("child_projects") or []
                    if children:
                        search_projects(children, full_path)

            search_projects(projects)
        except Exception as e:
            errors.append(f"{instance_name}: {type(e).__name__}: {e}")
            continue

    # Log errors for debugging (visible in Render logs)
    if errors:
        import logging
        logging.warning(f"_find_projects_by_name('{query}'): {errors}")

    # Sort by instance, then name
    matches.sort(key=lambda m: (m["instance"], m["name"].lower()))

    # Include errors in result for debugging
    if not matches and errors:
        return [{"_errors": errors}]
    return matches


def _request(method: str, endpoint: str, allow_instance_fallback: bool = False, instance: Optional[str] = None, **kwargs) -> dict:
    """Make authenticated request to Vikunja API.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE)
        endpoint: API endpoint (e.g., /api/v1/tasks)
        allow_instance_fallback: If True, fall back to VIKUNJA_TOKEN env var.
            Default is False for security - user requests MUST have a user token set.
            Only set to True for CLI/MCP usage or system operations.
        instance: Optional instance name override. When set, uses this instance's
            URL and token instead of the current/default instance. Useful for
            cross-instance operations in MCP mode.
        **kwargs: Additional arguments passed to requests.request()

    Token resolution:
    1. Context variable _current_vikunja_token (set by auth check for user requests)
    2. Instance token from VIKUNJA_TOKEN env var (ONLY if allow_instance_fallback=True)

    Raises:
        ValueError: If no token is available
    """
    # Get URL and default token from instance config
    # Three modes:
    # 1. User context (Matrix/Slack): Get from PostgreSQL
    # 2. Bot mode (@eis): Get from env vars (VIKUNJA_URL + VIKUNJA_BOT_TOKEN)
    # 3. MCP/CLI mode: Get from YAML config (multi-instance)
    user_id = _current_user_id.get()
    bot_mode = _bot_mode.get()

    if _per_user_override():
        # fa-bglr.7: a per-user calendar request has pinned this user's url+token for
        # the whole request. Honor it directly (via the same override branch in
        # _get_instance_config) so we don't re-resolve the user's instance from the DB
        # on EVERY _request, and so reads/writes hit the USER's own Vikunja. The token
        # itself is picked up from the contextvar below.
        base_url, instance_token = _get_instance_config(instance)
        logger.debug(f"[_request] Per-user override: url={base_url}")
    elif user_id:
        # User context available - get URL from PostgreSQL
        instance_name, base_url, instance_token = _get_user_instance_config(user_id)
        logger.debug(f"[_request] User context: user={user_id}, instance={instance_name}, url={base_url}")
    elif bot_mode:
        # Bot mode (@eis) - use env vars, NOT YAML config (solutions-zja1)
        base_url = os.environ.get("VIKUNJA_URL", "")
        instance_token = os.environ.get("VIKUNJA_BOT_TOKEN", "")
        if base_url:
            base_url = base_url.rstrip('/').strip()
        if instance_token:
            instance_token = instance_token.strip()
        logger.debug(f"[_request] Bot mode: using env vars, url={base_url}")
    else:
        # No user context and not bot_mode.
        # fa-3n7a: a context-less request must NOT silently fall through to a YAML
        # *default* instance that points at a different server. In prod the YAML/env
        # default was app.vikunja.cloud (the PUBLIC cloud), so any context-less
        # _request hit the wrong server and 401'd a valid Factumerit token — the deeper
        # version of the fa-f3ir /do-confirm misroute. Whenever this deployment sets
        # VIKUNJA_URL (single-server bot/CLI), honor it as the safe default. Only an
        # EXPLICIT `instance=` override (a genuine cross-instance MCP op) still resolves
        # via the multi-instance YAML config. Multi-instance users leave VIKUNJA_URL
        # unset, so their default-instance behavior is unchanged.
        env_url = os.environ.get("VIKUNJA_URL", "").rstrip('/').strip()
        if instance is None and env_url:
            base_url = env_url
            instance_token = (os.environ.get("VIKUNJA_BOT_TOKEN")
                              or os.environ.get("VIKUNJA_TOKEN") or "").strip()
            logger.debug(f"[_request] No user context; env VIKUNJA_URL default: url={base_url}")
        else:
            base_url, instance_token = _get_instance_config(instance)
            logger.debug(f"[_request] No user context, using YAML config: url={base_url}, instance={instance or 'default'}")

    # Check context var first (per-user token set by auth check)
    token = _current_vikunja_token.get()
    if token:
        # NEVER log token material (not even a prefix): per-user calendar requests now
        # funnel real user bearers through here (fa-bglr.7 / auggie HIGH). Length only.
        logger.debug(f"[_request] Using user token from context var (length={len(token)})")
    else:
        logger.debug(f"[_request] No user token in context var")

    # SECURITY: Only fall back to instance token if explicitly allowed
    # This prevents user requests from accidentally using the admin token
    if not token:
        # Check both parameter AND context var for fallback permission
        fallback_allowed = allow_instance_fallback or _allow_instance_fallback.get()
        if fallback_allowed:
            token = instance_token
            # Length only — never log token material (auggie HIGH).
            logger.debug(f"Using instance fallback token for {method} {endpoint} "
                         f"(length={len(instance_token) if instance_token else 0})")
        else:
            # Log security event - this should not happen if auth check is working
            logger.warning(
                f"SECURITY: No user token set for {method} {endpoint}. "
                "This may indicate a missing auth check. Rejecting request."
            )
            raise ValueError(
                "No Vikunja token available. Please connect with !vik first."
            )

    if not token:
        raise ValueError("No Vikunja token available")

    # fa-f3ir diagnostic: the confirm /do path 401s on DELETE despite a valid token.
    # Log (no token material) which branch chose the token and its shape for the rare
    # destructive DELETE, so we can see if _request is sending a different token than
    # the confirm handler set in the context var.
    if method == "DELETE":
        _t = token or ""
        _k = ("empty" if not _t else "api" if _t.startswith("tk_")
              else "jwt" if _t.startswith("ey") else "other")
        logger.info(
            f"[_request] DELETE {endpoint} token_len={len(_t)} token_kind={_k} "
            f"from_ctx={bool(_current_vikunja_token.get())} bot_mode={bot_mode} "
            f"user_ctx={bool(user_id)} base_url={base_url}"
        )

    full_url = f"{base_url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # Log project-specific requests at INFO level for debugging
    if "/projects/" in endpoint and "/tasks" in endpoint:
        logger.info(f"[_request] {method} {full_url} (params={kwargs.get('params', {})})")
    else:
        logger.debug(f"[_request] {method} {full_url} (params={kwargs.get('params', {})})")

    # Performance tracking
    start_time = time.time()
    # A stalled Vikunja call with no timeout hangs the whole MCP server
    # until the upstream proxy kills the socket (~4 min), indistinguishable from a
    # crash. Fail fast with a default timeout callers can still override via kwargs.
    kwargs.setdefault("timeout", 30)
    response = requests.request(method, full_url, headers=headers, **kwargs)
    elapsed_ms = (time.time() - start_time) * 1000

    # Log project-specific responses at INFO level
    if "/projects/" in endpoint and "/tasks" in endpoint:
        logger.info(f"[_request] Response: {response.status_code} ({elapsed_ms:.0f}ms) - Body length: {len(response.text)}")
    else:
        logger.debug(f"[_request] Response: {response.status_code} ({elapsed_ms:.0f}ms)")

    if PERF_LOGGING:
        logger.info(f"[PERF] {method} {endpoint} → {response.status_code} ({elapsed_ms:.0f}ms)")

    if response.status_code == 401:
        logger.error(f"[_request] 401 Unauthorized: {response.text[:200]}")
        raise ValueError(f"Authentication failed: {response.text}")
    elif response.status_code == 404:
        logger.error(f"[_request] 404 Not Found: {response.text[:200]}")
        raise ValueError(f"Resource not found: {response.text}")
    elif response.status_code == 403:
        # Vikunja returns an identical 403 whether the token genuinely
        # lacks permission OR the resource simply lives on a DIFFERENT instance than
        # the one this request routed to (multi-instance mismatch). Surface the
        # routing possibility so it isn't misdiagnosed as a read-only-token problem.
        logger.error(f"[_request] 403 Forbidden: {response.text[:200]}")
        raise ValueError(
            f"Permission denied (403): {response.text}. If this resource lives on a "
            f"different instance, pass the correct instance= (or ctx_set the active "
            f"instance) — a cross-instance request returns this same 403."
        )
    elif response.status_code >= 400:
        logger.error(f"[_request] {response.status_code} Error: {response.text[:200]}")
        raise ValueError(f"API error ({response.status_code}): {response.text}")

    if method != "DELETE":
        data = response.json()
        if isinstance(data, list):
            logger.debug(f"[_request] Returned {len(data)} items")
        elif isinstance(data, dict):
            logger.debug(f"[_request] Returned dict with keys: {list(data.keys())[:5]}")
        return data
    return {}


def _fetch_all_pages(
    method: str,
    endpoint: str,
    per_page: int = 50,
    max_pages: int = 100,
    params: dict = None,
    raise_on_error: bool = False,
    **kwargs
) -> list:
    """Fetch all pages from a paginated Vikunja API endpoint.

    Vikunja APIs return max 50 items per page (server-side limit).
    This function iterates through all pages until:
    - A page returns fewer items than per_page (last page)
    - An empty page is returned
    - max_pages limit is reached (safety against infinite loops)

    Args:
        method: HTTP method (usually "GET")
        endpoint: API endpoint path
        per_page: Items per page (Vikunja caps at 50)
        max_pages: Safety limit to prevent infinite loops
        params: Query parameters (page will be added/overwritten)
        raise_on_error: Propagate mid-pagination failures instead of silently
            returning the partial list. Use for completeness-critical callers
            (e.g. backups) where partial results are worse than an error.
        **kwargs: Additional arguments passed to _request

    Returns:
        List of all items from all pages combined
    """
    all_items = []
    params = dict(params) if params else {}
    params["per_page"] = per_page

    for page in range(1, max_pages + 1):
        params["page"] = page
        try:
            data = _request(method, endpoint, params=params, **kwargs)
        except ValueError as e:
            # Re-raise auth and critical errors
            if raise_on_error or "Authentication" in str(e) or "401" in str(e):
                raise
            # Stop on other errors (e.g., page doesn't exist)
            break
        except Exception:
            if raise_on_error:
                raise
            # Stop on unexpected errors
            break

        if not isinstance(data, list):
            if raise_on_error:
                raise ValueError(f"unexpected non-list response from {endpoint} page {page}")
            # Unexpected response format
            break

        all_items.extend(data)

        # Stop if we got fewer items than requested (last page)
        if len(data) < per_page:
            break

    return all_items


def _format_task(task: dict) -> dict:
    """Format task for MCP response."""
    reminders = task.get("reminders") or []
    return {
        "id": task["id"],
        "title": task["title"],
        "description": task.get("description", ""),
        "done": task.get("done", False),
        "priority": task.get("priority", 0),
        "position": task.get("position"),  # view-specific position (may be None)
        "start_date": task.get("start_date"),
        "end_date": task.get("end_date"),
        "due_date": task.get("due_date"),
        "repeat_after": task.get("repeat_after", 0),  # seconds between repeats (0 = no repeat)
        "repeat_mode": task.get("repeat_mode", 0),  # 0 = from due date, 1 = monthly, 2 = from completion
        "reminders": [r.get("reminder") for r in reminders],
        "project_id": task.get("project_id") or task.get("list_id", 0),
        "bucket_id": task.get("bucket_id", 0),
        "labels": [{"id": l["id"], "title": l["title"]} for l in (task.get("labels") or [])],
        "assignees": [{"id": a["id"], "username": a.get("username", "")} for a in (task.get("assignees") or [])],
    }


def _format_project(project: dict) -> dict:
    """Format project for MCP response."""
    return {
        "id": project["id"],
        "title": project["title"],
        "description": project.get("description", ""),
        "parent_project_id": project.get("parent_project_id", 0),
        "hex_color": project.get("hex_color", ""),
        "is_favorite": project.get("is_favorite", False),
        "is_archived": project.get("is_archived", False),
        "position": project.get("position", 0),
    }


def _format_label(label: dict) -> dict:
    """Format label for MCP response."""
    return {
        "id": label["id"],
        "title": label["title"],
        "hex_color": label.get("hex_color", ""),
    }


def _label_metadata(label: dict) -> dict:
    """Parse a Vikunja label's ``description`` field as JSON metadata.

    The "fa-3lri pattern" (see docs/specifications/today/02-label-metadata.md): a label's
    description holds a JSON object of attributes, turning a flat string label
    into a programmable, cross-cutting metadata slot — no Vikunja schema change.

    Returns ``{}`` on anything that isn't a JSON object so a plain string label
    is simply a tag with no metadata. Never raises: a malformed, empty, missing,
    or non-object (list/number/string) description degrades to ``{}``. This keeps
    every consumer (today-actions, !goal, !cal plan/log) able to read metadata
    defensively without guarding each call.
    """
    if not isinstance(label, dict):
        return {}  # None, or a bare-string label: a tag with no metadata
    desc = label.get("description") or ""
    try:
        val = json.loads(desc)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _format_bucket(bucket: dict) -> dict:
    """Format bucket for MCP response."""
    return {
        "id": bucket["id"],
        "title": bucket["title"],
        "project_id": bucket.get("project_id") or bucket.get("list_id", 0),
        "position": bucket.get("position", 0),
        "limit": bucket.get("limit", 0),
    }


def _format_view(view: dict) -> dict:
    """Format view for MCP response."""
    result = {
        "id": view["id"],
        "title": view["title"],
        "project_id": view.get("project_id", 0),
        "view_kind": view.get("view_kind", ""),
    }
    # Include filter if present - handle both string and object formats
    # (Vikunja API changed from object to string format)
    if "filter" in view and view["filter"]:
        filter_val = view["filter"]
        if isinstance(filter_val, str):
            # New format: filter is the query string directly
            filter_query = filter_val
        elif isinstance(filter_val, dict):
            # Old format: filter is an object with "filter" key
            filter_query = filter_val.get("filter", "")
        else:
            filter_query = ""
        if filter_query:
            result["filter"] = filter_query
    return result


def _format_relation(task_id: int, relation_kind: str, other_task: dict) -> dict:
    """Format task relation for MCP response."""
    return {
        "task_id": task_id,
        "other_task_id": other_task["id"],
        "other_task_title": other_task.get("title", ""),
        "relation_kind": relation_kind,
    }


def _list_projects_impl(instance: str = None) -> list[dict]:
    # Fetch all pages for projects
    response = _fetch_all_pages("GET", "/api/v1/projects", per_page=50, max_pages=20, instance=instance)
    return [_format_project(p) for p in response]


def _get_project_impl(project_id: int, instance: str = None) -> dict:
    response = _request("GET", f"/api/v1/projects/{project_id}", instance=instance)
    return _format_project(response)


def _create_project_impl(title: str, description: str = "", hex_color: str = "", parent_project_id: int = 0) -> dict:
    """Create a project - routes to queue system for bot mode, direct creation for MCP mode.

    Bead: solutions-eofy

    Bot mode (EARS @mentions): Queue project for user to create with their session token.
    MCP mode (Claude Desktop): Create project directly with user's token (works fine).
    """
    # Check if we're in bot mode (Vikunja EARS) vs MCP mode (Claude Desktop)
    bot_mode = _bot_mode.get()

    if bot_mode:
        # Bot mode: Use queue system (solutions-eofy)
        return _create_project_impl_queue(title, description, hex_color, parent_project_id)
    else:
        # MCP mode: Direct creation (existing behavior)
        return _create_project_impl_direct(title, description, hex_color, parent_project_id)


def _create_project_impl_direct(title: str, description: str = "", hex_color: str = "", parent_project_id: int = 0) -> dict:
    """Direct project creation for MCP mode - user's token, works fine."""
    # Security: Sanitize title (strip HTML)
    data = {"title": _sanitize_title(title)}
    if description:
        data["description"] = description
    if hex_color:
        data["hex_color"] = hex_color
    if parent_project_id:
        data["parent_project_id"] = parent_project_id

    response = _request("PUT", "/api/v1/projects", json=data)
    project = _format_project(response)
    new_project_id = project.get("id")

    # fa-s965.2 create-hook: instantly grant the assistant (dispatcher subscribe + actor
    # read/write) access to a project the assistant just created for a Factumerit user, so
    # @mentions in it work immediately instead of waiting for the periodic sweep (Phase 2a).
    # Keyed on `_current_user_id` — the SAME user context `_request` used to create the
    # project (dispatch/handler paths); `_requesting_user` is bot-mode-only and this direct
    # path is non-bot-mode, so it would be None here. normalize_vikunja_user_id handles
    # 2/3-element ids and never double-prefixes (auggie). BEST-EFFORT: never fail creation
    # on a grant hiccup. Plain Claude Desktop has no `_current_user_id` → skipped;
    # UI-created projects are covered by the sweep.
    _grant_hook_uid = _current_user_id.get()
    if new_project_id and _grant_hook_uid:
        try:
            from .bot_provisioning import grant_project, normalize_vikunja_user_id
            _gr = grant_project(normalize_vikunja_user_id(_grant_hook_uid), new_project_id)
            logger.info(f"[create_project] fa-s965.2 grant p{new_project_id}: {_gr.get('status')}")
        except Exception as e:
            logger.warning(f"[create_project] fa-s965.2 grant hook failed for p{new_project_id}: {e}")

    shared_with = []

    # Auto-transfer: If bot created this project, clone it to owner's account (solutions-2x6i)
    # DISABLED: Project cloning disabled due to JWT token expiry issues (solutions-eofy)
    # Owner JWT tokens expire after 24 hours, but we only store them once during signup.
    # This causes cloning to fail for users who haven't logged in recently.
    #
    # Alternative approach: Bot creates project and shares it with owner (see fallback sharing below).
    # Owner can access bot-owned projects just fine - they just don't "own" them.
    #
    # TODO: Implement one of these solutions:
    # 1. Store owner credentials (encrypted) and get fresh JWT on demand
    # 2. Implement JWT refresh token flow
    # 3. Accept that projects are bot-owned and shared with users
    requesting_user = _requesting_user.get()
    if False and new_project_id and requesting_user:  # Disabled for now
        try:
            from .project_cloner import clone_project_to_user
            from .bot_provisioning import get_bot_owner_token, get_user_bot_credentials
            from .bot_jwt_manager import get_bot_jwt
            from .token_broker import get_user_token, AuthRequired

            # Get bot's JWT token (to read bot's project)
            # Bot API tokens are broken (Vikunja issue #105), so we use JWT auth
            user_id_for_lookup = f"vikunja:{requesting_user}"
            bot_token = None

            bot_creds = get_user_bot_credentials(user_id_for_lookup)
            if bot_creds:
                bot_username, bot_password = bot_creds
                bot_token = get_bot_jwt(bot_username, bot_password, os.environ.get("VIKUNJA_URL", "https://vikunja.factumerit.app"))
                logger.info(f"[create_project] Got bot JWT token for {bot_username}")

            # Get user's JWT token (to create project in user's account)
            # First check personal_bots table (stored during signup)
            # Then fall back to token_broker (OIDC-authenticated users)
            user_token = None

            user_token = get_bot_owner_token(user_id_for_lookup)
            if user_token:
                logger.info(f"[create_project] Using owner token from personal_bots for {requesting_user}")
            else:
                # Fall back to OIDC token
                try:
                    user_token = get_user_token(
                        user_id=requesting_user,
                        purpose="clone_bot_project",
                        caller="server._create_project_impl"
                    )
                    logger.info(f"[create_project] Using OIDC token from token_broker for {requesting_user}")
                except AuthRequired as e:
                    logger.info(f"[create_project] User {requesting_user} has no token (not in personal_bots or token_broker), skipping clone: {e}")
                    # Fall through to return bot's project (still works, just not in user's account)

            if bot_token and user_token:
                logger.info(f"[create_project] Cloning bot project {new_project_id} to user {requesting_user}")

                # Clone project from bot's account to user's account
                result = clone_project_to_user(
                    bot_project_id=new_project_id,
                    target_user_token=user_token,
                    bot_token=bot_token,  # Bot's JWT token (not broken API token)
                    parent_project_id=parent_project_id,
                    delete_original=True  # Delete bot's copy after cloning
                )

                if result["success"]:
                    # Return the user's project instead of bot's project
                    user_project_id = result["user_project_id"]
                    logger.info(f"[create_project] Successfully cloned: bot#{new_project_id} → user#{user_project_id}")

                    # Fetch and return the user's project
                    user_project = _request("GET", f"/api/v1/projects/{user_project_id}")
                    return _format_project(user_project)
                else:
                    logger.error(f"[create_project] Clone failed: {result.get('error')}")
                    # Fall through to return bot's project
            else:
                logger.info(f"[create_project] Skipping clone (bot_token={bool(bot_token)}, user_token={bool(user_token)})")
        except AuthRequired as e:
            # User hasn't authenticated - this is expected for users who haven't done OIDC yet
            logger.info(f"[create_project] User {requesting_user} not authenticated, skipping clone: {e}")
        except Exception as e:
            logger.error(f"[create_project] Failed to clone project to user: {e}", exc_info=True)

    # Auto-share: If this is a subproject, inherit users from parent
    if parent_project_id and new_project_id:
        try:
            parent_users = _request("GET", f"/api/v1/projects/{parent_project_id}/users")
            logger.info(f"[create_project] Parent project {parent_project_id} has {len(parent_users)} users: {[u.get('username') for u in parent_users]}")
            for user in parent_users:
                # NOTE: user.get("id") is the RELATION ID, not user ID!
                # The actual user_id is excluded from JSON response (json:"-")
                # We must look up the real user ID by username
                username = user.get("username", "")
                right = user.get("right", 2)  # Preserve their permission level
                if username:
                    try:
                        # Look up real user ID by username
                        user_search = _request("GET", f"/api/v1/users?s={username}")
                        matching = [u for u in user_search if u.get("username", "").lower() == username.lower()]
                        if matching:
                            real_user_id = matching[0]["id"]
                            logger.info(f"[create_project] Inheriting user {username} (real_id={real_user_id}) from parent")
                            _request("PUT", f"/api/v1/projects/{new_project_id}/users", json={
                                "user_id": str(real_user_id),  # Vikunja expects string
                                "right": right
                            })
                            shared_with.append(username)
                        else:
                            logger.warning(f"[create_project] Could not find user {username} via search")
                    except Exception as e:
                        logger.warning(f"[create_project] Failed to inherit user {username}: {e}")
            if shared_with:
                logger.info(f"[create_project] Inherited access from parent: {shared_with}")
        except Exception as e:
            logger.warning(f"[create_project] Failed to inherit users from parent: {e}")

    # Fallback sharing: Share bot's project with requesting user
    # This ensures users can access bot-created projects with admin rights
    # Uses bot's JWT token to share (bot owns the project)
    # Uses requesting_user_id directly (no user search needed - passed from poller)
    requesting_user_id = _requesting_user_id.get()
    if new_project_id and requesting_user_id:
        # Skip if already shared via cloning or parent inheritance
        if not requesting_user or requesting_user.lower() not in [u.lower() for u in shared_with]:
            try:
                from .bot_provisioning import get_user_bot_credentials
                from .bot_jwt_manager import get_bot_jwt
                import httpx

                # Get bot's JWT token (to share the project it owns)
                user_id_for_lookup = f"vikunja:{requesting_user}" if requesting_user else None
                bot_token = None

                if user_id_for_lookup:
                    bot_creds = get_user_bot_credentials(user_id_for_lookup)
                    if bot_creds:
                        bot_username, bot_password = bot_creds
                        bot_token = get_bot_jwt(bot_username, bot_password, os.environ.get("VIKUNJA_URL", "https://vikunja.factumerit.app"))
                        logger.info(f"[create_project] Got bot JWT token for sharing: {bot_username}")

                if not bot_token:
                    logger.warning(f"[create_project] No personal bot found for {requesting_user} - user may be legacy account created before bot provisioning was added. Projects will be created in shared bot account.")
                else:
                    vikunja_url = os.environ.get("VIKUNJA_URL", "https://vikunja.factumerit.app")

                    # Share project using bot's JWT token (bot owns it)
                    # Use requesting_user (username) - Vikunja API expects "username" field, not "user_id"
                    # See: solutions-2x6i, 111-BOT_PROJECT_SHARING_BUG.md
                    logger.info(f"[create_project] Sharing project {new_project_id} with user {requesting_user}")
                    share_resp = httpx.put(
                        f"{vikunja_url}/api/v1/projects/{new_project_id}/users",
                        headers={"Authorization": f"Bearer {bot_token}"},
                        json={
                            "username": requesting_user,  # Vikunja expects "username", not "user_id"
                            "right": 2  # Admin access
                        },
                        timeout=10
                    )
                    share_resp.raise_for_status()
                    shared_with.append(requesting_user)
                    logger.info(f"[create_project] Successfully shared bot project with {requesting_user}")
            except Exception as e:
                logger.warning(f"[create_project] Failed to auto-share with {requesting_user}: {e}")

    # Auto-share dispatcher bot (read-only) + personal bot (read/write) with new projects
    # created via MCP server. This enables @mentions and personal bot writes. (fa-n5lm)
    # Implicit consent: if you create via the bot, you want the bot there.
    if new_project_id and requesting_user:
        try:
            from .bot_provisioning import get_user_bot_credentials, get_user_bot_vikunja_id
            from .bot_jwt_manager import get_bot_jwt
            import httpx

            vikunja_url = os.environ.get("VIKUNJA_URL", "https://vikunja.factumerit.app")
            user_id_str = f"vikunja:{requesting_user}"

            # Get personal bot's JWT token (bot owns the project, can share it)
            personal_creds = get_user_bot_credentials(user_id_str)
            if personal_creds:
                pb_username, pb_password = personal_creds
                pb_token = get_bot_jwt(pb_username, pb_password, vikunja_url)

                if pb_token:
                    # Share dispatcher bot (read-only) for @mention detection
                    dispatch_creds = get_user_bot_credentials("system:dispatcher")
                    if dispatch_creds:
                        dispatch_bot = BotVikunjaClient(user_id="system:dispatcher")
                        dispatch_vikunja_id = dispatch_bot.get_bot_user_id()
                        try:
                            resp = httpx.put(
                                f"{vikunja_url}/api/v1/projects/{new_project_id}/users",
                                headers={"Authorization": f"Bearer {pb_token}"},
                                json={"user_id": str(dispatch_vikunja_id), "right": 0},
                                timeout=10
                            )
                            if resp.status_code != 409:
                                resp.raise_for_status()
                            logger.info(f"[create_project] Shared dispatcher (read-only) with project {new_project_id}")
                        except Exception as e:
                            logger.debug(f"[create_project] Dispatcher share: {e}")

                    # Share personal bot (read/write) for writing responses
                    personal_vikunja_id = get_user_bot_vikunja_id(user_id_str)
                    if personal_vikunja_id:
                        try:
                            resp = httpx.put(
                                f"{vikunja_url}/api/v1/projects/{new_project_id}/users",
                                headers={"Authorization": f"Bearer {pb_token}"},
                                json={"user_id": str(personal_vikunja_id), "right": 1},
                                timeout=10
                            )
                            if resp.status_code != 409:
                                resp.raise_for_status()
                            logger.info(f"[create_project] Shared personal bot (read/write) with project {new_project_id}")
                        except Exception as e:
                            logger.debug(f"[create_project] Personal bot share: {e}")
        except Exception as e:
            logger.warning(f"[create_project] Failed to auto-share bots: {e}")

    if shared_with:
        project["shared_with"] = shared_with

    _invalidate_project_instance_cache()  # fa-tghu: project set changed
    _invalidate_project_colors_cache()    # fa-doh7: project set changed
    return project


def _create_project_impl_queue(title: str, description: str = "", hex_color: str = "", parent_project_id: int = 0) -> dict:
    """Queue project for user-side creation (bot mode only).

    Bead: solutions-eofy

    Instead of bot creating the project (which causes permission issues),
    we queue the project spec for the user's frontend to create using their
    active session token. This ensures:
    - User owns the project from the start
    - No token expiry issues (uses active session)
    - Bot gets access (user shares back)

    Supports batching: Multiple create_project calls in one LLM turn are
    batched into a single queue entry with projects as JSON array.
    """
    # Security: Sanitize title (strip HTML)
    sanitized_title = _sanitize_title(title)

    # Get user context
    requesting_user = _requesting_user.get()  # e.g., "ivan"
    requesting_user_id = _requesting_user_id.get()  # Numeric ID from bot mode

    if not requesting_user:
        logger.warning("[create_project_queue] No requesting_user, falling back to direct creation")
        return _create_project_impl_direct(sanitized_title, description, hex_color, parent_project_id)

    # Construct user_id for bot_provisioning lookup
    # Bot mode uses "vikunja:username" format
    user_id = f"vikunja:{requesting_user}"

    # Get bot username for sharing back
    try:
        from .bot_provisioning import get_user_bot_credentials
    except ImportError:
        # Bot provisioning is server-side and unpublished. Fall back to the same direct
        # path this function already takes when there is no requesting user (fa-sxac).
        logger.warning("[create_project_queue] bot_provisioning unavailable — creating directly")
        return _create_project_impl_direct(sanitized_title, description, hex_color, parent_project_id)
    bot_username = None
    bot_creds = get_user_bot_credentials(user_id)
    if bot_creds:
        bot_username, _ = bot_creds

    if not bot_username:
        logger.warning(f"[create_project_queue] No bot found for {requesting_user} (user_id={user_id}), falling back to direct creation")
        return _create_project_impl_direct(sanitized_title, description, hex_color, parent_project_id)

    # Check if we're in batch mode (LLM creating multiple projects)
    pending = _pending_projects.get()
    if pending is None:
        # Initialize batch mode for this LLM turn
        pending = []
        _pending_projects.set(pending)

    # Assign temporary negative ID for parent references
    temp_id = _next_temp_id.get()
    _next_temp_id.set(temp_id - 1)

    # Add to batch
    project_spec = {
        "temp_id": temp_id,
        "title": sanitized_title,
        "description": description,
        "hex_color": hex_color,
        "parent_project_id": parent_project_id
    }
    pending.append(project_spec)

    logger.info(f"[create_project_queue] Queued project '{sanitized_title}' (temp_id={temp_id}) for {requesting_user}")

    # Return temp project (will be flushed at end of LLM turn)
    return {
        "id": temp_id,  # Negative temp ID
        "title": sanitized_title,
        "description": description,
        "hex_color": hex_color,
        "parent_project_id": parent_project_id,
        "status": "queued_for_creation"
    }


def _delete_project_impl(project_id: int) -> dict:
    _request("DELETE", f"/api/v1/projects/{project_id}")
    _invalidate_project_instance_cache()  # fa-tghu: project set changed
    _invalidate_project_colors_cache()    # fa-doh7: project set changed
    return {"deleted": True, "project_id": project_id}


def _get_project_users_impl(project_id: int) -> dict:
    """Get users with access to a project.

    Note: The Vikunja API returns a relation_id (not user_id) in the 'id' field.
    The actual user_id is excluded from the JSON response. Use username for
    lookups via /api/v1/users?s={username} if you need the real user ID.
    """
    users = _request("GET", f"/api/v1/projects/{project_id}/users")
    return {
        "project_id": project_id,
        "users": [
            {
                "relation_id": u.get("id"),  # NOTE: This is NOT user_id, it's the relation ID
                "username": u.get("username"),
                "name": u.get("name"),
                "right": u.get("right"),  # 0=read, 1=read+write, 2=admin
            }
            for u in users
        ]
    }


def _share_project_impl(project_id: int, username: str = "", user_id: int = 0, right: int = 2) -> dict:
    """Share a project with a user by username.

    Args:
        project_id: Project to share
        username: Username to share with (required - Vikunja API uses username)
        user_id: Deprecated - Vikunja API doesn't accept user_id, only username
        right: Permission level (0=read, 1=read+write, 2=admin). Default is admin.

    Returns:
        Success/failure status
    """
    # Vikunja API uses username, not user_id
    if not username:
        return {"error": "Username is required (Vikunja API uses username, not user_id)"}

    # Get requesting user's token for the share call
    # Bot tokens can't share projects - need user's own token
    requesting_user = _requesting_user.get()
    user_token = None
    if requesting_user:
        try:
            from .token_broker import get_user_token
            # User ID format is "vikunja:username" - need to construct it
            token_user_id = f"vikunja:{requesting_user}"
            user_token = get_user_token(
                user_id=token_user_id,
                purpose="share_project",
                caller="server._share_project_impl"
            )
            logger.info(f"[share_project] Got user token for {requesting_user}")
        except Exception as e:
            logger.warning(f"[share_project] Could not get user token: {e}")

    # Add user to project - Vikunja API uses username, not user_id
    try:
        if user_token:
            # Use user's token for sharing (bot token can't share)
            import httpx
            base_url = os.environ.get("VIKUNJA_URL", "https://vikunja.factumerit.app").rstrip("/")
            resp = httpx.put(
                f"{base_url}/api/v1/projects/{project_id}/users",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"username": username, "right": right},
                timeout=30.0,
            )
            if resp.status_code >= 400:
                return {"error": f"Share failed: {resp.status_code} - {resp.text}"}
            logger.info(f"[share_project] Shared project {project_id} with {username} using user token")
        else:
            # Use bot token - should work now with username
            _request("PUT", f"/api/v1/projects/{project_id}/users", json={
                "username": username,
                "right": right
            })

        right_names = {0: "read", 1: "read+write", 2: "admin"}
        return {
            "success": True,
            "project_id": project_id,
            "user_id": user_id,
            "username": username or requesting_user,
            "right": right_names.get(right, str(right))
        }
    except Exception as e:
        return {"error": f"Failed to share project: {e}"}


def _update_project_impl(project_id: int, title: str = "", description: str = "", hex_color: str = "", parent_project_id: int = -1, position: float = -1) -> dict:
    """Update a project's properties. Use parent_project_id=0 to move to root."""
    # GET current project state (Vikunja API replaces, so we merge)
    current = _request("GET", f"/api/v1/projects/{project_id}")

    # Only update fields that were explicitly provided
    # Security: Sanitize title (strip HTML)
    if title:
        current["title"] = _sanitize_title(title)
    if description:
        current["description"] = description
    if hex_color:
        current["hex_color"] = hex_color
    if parent_project_id >= 0:  # -1 means don't change, 0 means root, >0 means reparent
        current["parent_project_id"] = parent_project_id
    if position >= 0:  # -1 means don't change
        current["position"] = position

    response = _request("POST", f"/api/v1/projects/{project_id}", json=current)
    _invalidate_project_colors_cache()  # fa-doh7: hex_color may have changed
    return _format_project(response)


def _export_all_projects_impl(include_comments: bool = False) -> dict:
    """Export all projects and their tasks for backup."""
    # Global labels with colors
    labels_raw = _fetch_all_pages("GET", "/api/v1/labels", per_page=50, max_pages=10)
    global_labels = [
        {"id": l["id"], "title": l["title"], "hex_color": l.get("hex_color", "")}
        for l in labels_raw
    ]

    # raise_on_error: a backup must fail loudly rather than quietly omit
    # whatever came after a mid-pagination failure (fa-k5hw).
    projects = _fetch_all_pages("GET", "/api/v1/projects", per_page=50, max_pages=100,
                                raise_on_error=True)
    export = {
        "exported_at": datetime.now().isoformat(),
        "project_count": len(projects),
        "task_count": 0,
        "labels": global_labels,
        "projects": []
    }
    if len(projects) >= 50 * 100:
        # every page came back full — the max_pages cap may have truncated the list
        export["warning"] = (
            f"project list hit the {50 * 100}-item pagination cap; export may be "
            "incomplete — raise max_pages in _export_all_projects_impl"
        )

    task_count = 0
    for project in projects:
        project_data = _format_project(project)

        # Views + buckets for kanban views
        try:
            views_raw = _request("GET", f"/api/v1/projects/{project['id']}/views")
            project_views = []
            for v in views_raw:
                view_data = _format_view(v)
                if v.get("view_kind") == "kanban":
                    try:
                        # Use the tasks-in-view endpoint — it returns bucket objects with tasks
                        # nested. The dedicated /buckets endpoint returns 401 on some instances.
                        buckets_with_tasks = _request("GET", f"/api/v1/projects/{project['id']}/views/{v['id']}/tasks")
                        view_data["buckets"] = []
                        for b in (buckets_with_tasks or []):
                            bucket_entry = {k: val for k, val in _format_bucket(b).items() if k != "tasks"}
                            # Store task IDs so import can restore bucket assignments per-view
                            bucket_entry["task_ids"] = [t["id"] for t in b.get("tasks", [])]
                            view_data["buckets"].append(bucket_entry)
                    except Exception:
                        view_data["buckets"] = []
                project_views.append(view_data)
            project_data["views"] = project_views
        except Exception:
            project_data["views"] = []

        # Tasks (all pages, including completed)
        try:
            tasks_raw = _fetch_all_pages("GET", f"/api/v1/projects/{project['id']}/tasks", per_page=50, max_pages=100)
            tasks = []
            for t in tasks_raw:
                task_data = _format_task(t)

                # Relations — already present in task response, no extra API call
                related_tasks = t.get("related_tasks") or {}
                relations = []
                for relation_kind, related in related_tasks.items():
                    for other in (related or []):
                        relations.append({
                            "other_task_id": other["id"],
                            "other_task_title": other.get("title", ""),
                            "relation_kind": relation_kind,
                        })
                if relations:
                    task_data["relations"] = relations

                # Comments — opt-in (one API call per task)
                if include_comments:
                    try:
                        task_data["comments"] = _get_comments_impl(t["id"])
                    except Exception:
                        task_data["comments"] = []

                tasks.append(task_data)
            project_data["tasks"] = tasks
            task_count += len(tasks)
        except Exception:
            project_data["tasks"] = []
            project_data["task_error"] = "Failed to fetch tasks"

        export["projects"].append(project_data)

    export["task_count"] = task_count
    return export


def _topological_sort_projects(projects: list[dict]) -> list[dict]:
    """Sort projects so parents always appear before their children.

    Projects whose parent is not in the export are treated as roots.
    """
    project_ids = {p["id"] for p in projects}
    by_id = {p["id"]: p for p in projects}
    result = []
    visited = set()

    def visit(project_id: int):
        if project_id in visited:
            return
        visited.add(project_id)
        parent_id = by_id[project_id].get("parent_project_id", 0)
        if parent_id and parent_id in project_ids:
            visit(parent_id)
        result.append(by_id[project_id])

    for p in projects:
        visit(p["id"])

    return result


def _import_all_projects_impl(export_data: dict, dry_run: bool = False) -> dict:
    """Import a full project export into the current Vikunja instance.

    Handles label deduplication, project hierarchy, view/bucket creation,
    task creation with ID remapping, relations, and comments.

    Args:
        export_data: Output from _export_all_projects_impl
        dry_run: If True, count what would be created without making API calls
    """
    summary = {
        "dry_run": dry_run,
        "labels_created": 0,
        "labels_skipped": 0,
        "projects_created": 0,
        "views_created": 0,
        "buckets_created": 0,
        "tasks_created": 0,
        "relations_created": 0,
        "comments_created": 0,
        "errors": [],
    }

    label_id_map: dict[int, int] = {}        # old_id → new_id
    project_id_map: dict[int, int] = {}      # old_id → new_id
    bucket_id_map: dict[int, int] = {}       # old_id → new_id
    task_id_map: dict[int, int] = {}         # old_id → new_id
    view_task_bucket: dict[int, int] = {}    # old_task_id → new_bucket_id (default view)
    # (new_project_id, new_view_id, new_bucket_id, old_task_id) for custom view bucket assignments
    view_bucket_task_assignments: list = []

    # ── Step 1: Labels ────────────────────────────────────────────────────────
    existing_labels = _fetch_all_pages("GET", "/api/v1/labels", per_page=50, max_pages=10)
    existing_by_title = {l["title"]: l["id"] for l in existing_labels}

    for label in export_data.get("labels", []):
        title = label["title"]
        if title in existing_by_title:
            label_id_map[label["id"]] = existing_by_title[title]
            summary["labels_skipped"] += 1
        else:
            summary["labels_created"] += 1
            if not dry_run:
                new_label = _request("PUT", "/api/v1/labels",
                                     json={"title": title, "hex_color": label.get("hex_color", "")})
                label_id_map[label["id"]] = new_label["id"]

    if dry_run:
        # Count projects/tasks/relations without API calls
        projects = export_data.get("projects", [])
        summary["projects_created"] = len(projects)
        tasks = [t for p in projects for t in p.get("tasks", [])]
        summary["tasks_created"] = len(tasks)
        summary["relations_created"] = sum(len(t.get("relations", [])) for t in tasks)
        summary["views_created"] = sum(len(p.get("views", [])) for p in projects)
        summary["buckets_created"] = sum(
            len(v.get("buckets", [])) for p in projects for v in p.get("views", [])
        )
        summary["comments_created"] = sum(len(t.get("comments", [])) for t in tasks)
        return summary

    # ── Step 2: Projects (topologically sorted) ───────────────────────────────
    projects = _topological_sort_projects(export_data.get("projects", []))

    for project in projects:
        old_parent = project.get("parent_project_id", 0)
        new_parent = project_id_map.get(old_parent, 0) if old_parent else 0

        try:
            new_project = _request("PUT", "/api/v1/projects", json={
                "title": project["title"],
                "description": project.get("description", ""),
                "hex_color": project.get("hex_color", ""),
                "parent_project_id": new_parent,
            })
            new_project_id = _format_project(new_project)["id"]
            project_id_map[project["id"]] = new_project_id
            summary["projects_created"] += 1
        except Exception as e:
            summary["errors"].append(f"Project '{project['title']}': {e}")
            continue

        # ── Step 3: Views + buckets ───────────────────────────────────────────
        for view in project.get("views", []):
            try:
                view_payload: dict = {
                    "title": view["title"],
                    "view_kind": view.get("view_kind", "list"),
                }
                if view.get("filter"):
                    # Vikunja expects filter as a plain string (e.g., "done = false").
                    # If the exported filter is a dict (old format), extract the string.
                    filt = view["filter"]
                    if isinstance(filt, dict):
                        filt = filt.get("filter", "")
                    view_payload["filter"] = filt
                new_view = _request("PUT", f"/api/v1/projects/{new_project_id}/views", json=view_payload)
                new_view_id = new_view["id"]
                summary["views_created"] += 1

                if view.get("view_kind") == "kanban":
                    # Remove auto-created default buckets
                    try:
                        defaults = _request("GET", f"/api/v1/projects/{new_project_id}/views/{new_view_id}/buckets")
                        for b in (defaults or []):
                            _request("DELETE", f"/api/v1/projects/{new_project_id}/views/{new_view_id}/buckets/{b['id']}")
                    except Exception:
                        pass

                    new_bucket_ids = []
                    for bucket in view.get("buckets", []):
                        try:
                            new_bucket = _request("PUT", f"/api/v1/projects/{new_project_id}/views/{new_view_id}/buckets", json={
                                "title": bucket["title"],
                                "position": bucket.get("position", 0),
                                "limit": bucket.get("limit", 0),
                            })
                            new_bid = new_bucket["id"]
                            bucket_id_map[bucket["id"]] = new_bid
                            new_bucket_ids.append(new_bid)
                            summary["buckets_created"] += 1
                            # Record view-specific bucket assignments for a post-task-creation pass
                            for old_tid in bucket.get("task_ids", []):
                                view_task_bucket[old_tid] = new_bid  # for default view fallback
                                view_bucket_task_assignments.append(
                                    (new_project_id, new_view_id, new_bid, old_tid)
                                )
                        except Exception as e:
                            summary["errors"].append(f"Bucket '{bucket['title']}': {e}")

                    # Switch to manual mode and set default/done buckets so tasks
                    # render as cards rather than column headers (v2.0+ requirement)
                    if new_bucket_ids:
                        try:
                            _request("POST", f"/api/v1/projects/{new_project_id}/views/{new_view_id}", json={
                                "title": view["title"],
                                "view_kind": "kanban",
                                "bucket_configuration_mode": "manual",
                                "default_bucket_id": new_bucket_ids[0],
                                "done_bucket_id": new_bucket_ids[-1],
                            })
                        except Exception:
                            pass
            except Exception as e:
                summary["errors"].append(f"View '{view.get('title')}' in '{project['title']}': {e}")

        # ── Step 4: Tasks ─────────────────────────────────────────────────────
        for task in project.get("tasks", []):
            try:
                # Prefer view-specific bucket assignment (from task_ids in bucket export)
                # over the task's own bucket_id (which may be 0 for custom kanban views)
                new_bucket_id = (view_task_bucket.get(task["id"])
                                 or bucket_id_map.get(task.get("bucket_id", 0), 0))
                task_data = {
                    "title": task["title"],
                    "description": task.get("description", ""),
                    "done": task.get("done", False),
                    "priority": task.get("priority", 0),
                    "repeat_after": task.get("repeat_after", 0),
                    "repeat_mode": task.get("repeat_mode", 0),
                    "bucket_id": new_bucket_id,
                }
                for date_field in ("due_date", "start_date", "end_date"):
                    val = task.get(date_field)
                    if val and val != "0001-01-01T00:00:00Z":
                        task_data[date_field] = val

                new_task = _request("PUT", f"/api/v1/projects/{new_project_id}/tasks", json=task_data)
                new_task_id = new_task["id"]
                task_id_map[task["id"]] = new_task_id
                summary["tasks_created"] += 1

                # Apply labels
                for lbl in task.get("labels", []):
                    new_label_id = label_id_map.get(lbl["id"])
                    if new_label_id:
                        try:
                            _request("PUT", f"/api/v1/tasks/{new_task_id}/labels",
                                     json={"label_id": new_label_id})
                        except Exception:
                            pass

                # Set reminders
                reminders = [r for r in task.get("reminders", [])
                             if r and r != "0001-01-01T00:00:00Z"]
                if reminders:
                    try:
                        _set_reminders_impl(new_task_id, reminders)
                    except Exception:
                        pass

                # Comments
                for comment in task.get("comments", []):
                    text = comment.get("text", "")
                    if text:
                        try:
                            _request("PUT", f"/api/v1/tasks/{new_task_id}/comments",
                                     json={"comment": text})
                            summary["comments_created"] += 1
                        except Exception:
                            pass

            except Exception as e:
                summary["errors"].append(f"Task '{task.get('title')}': {e}")

    # ── Step 5: Relations (after all tasks exist) ─────────────────────────────
    # Vikunja auto-creates the inverse relation (e.g. creating "A blocks B"
    # also creates "B blocked-by A"). The export captures both directions, so
    # we deduplicate by canonical kind before creating.
    _canonical_rel = {
        "blocking": "blocking", "blocked": "blocking",
        "subtask": "subtask", "parenttask": "subtask",
        "precedes": "precedes", "follows": "precedes",
        "duplicateof": "duplicateof", "duplicates": "duplicateof",
        "copiedfrom": "copiedfrom", "copiedto": "copiedfrom",
        "related": "related",
    }
    seen_relations: set = set()
    for project in export_data.get("projects", []):
        for task in project.get("tasks", []):
            new_task_id = task_id_map.get(task["id"])
            if not new_task_id:
                continue
            for rel in task.get("relations", []):
                other_new_id = task_id_map.get(rel["other_task_id"])
                if not other_new_id:
                    continue
                kind = rel["relation_kind"]
                canonical = _canonical_rel.get(kind, kind)
                pair_key = (frozenset({new_task_id, other_new_id}), canonical)
                if pair_key in seen_relations:
                    continue
                seen_relations.add(pair_key)
                try:
                    _request("PUT", f"/api/v1/tasks/{new_task_id}/relations", json={
                        "task_id": new_task_id,
                        "other_task_id": other_new_id,
                        "relation_kind": kind,
                    })
                    summary["relations_created"] += 1
                except Exception as e:
                    summary["errors"].append(
                        f"Relation '{kind}' for task {task['id']}: {e}")

    # ── Step 6: View-specific bucket assignments ───────────────────────────────
    # bucket_id on tasks only sets the default kanban view's bucket. For custom
    # kanban views, use POST /projects/{pid}/views/{vid}/buckets/{bid}/tasks.
    for new_pid, new_vid, new_bid, old_tid in view_bucket_task_assignments:
        new_tid = task_id_map.get(old_tid)
        if not new_tid:
            continue
        try:
            _request("POST", f"/api/v1/projects/{new_pid}/views/{new_vid}/buckets/{new_bid}/tasks",
                     json={"task_id": new_tid})
        except Exception:
            pass

    return summary


@mcp.tool()
@mcp_tool_with_fallback
def project_import(
    export_data: dict = Field(description="Export data from export_all_projects"),
    dry_run: bool = Field(default=False, description="Preview counts without creating anything")
) -> dict:
    """
    Import a full project export into the current Vikunja instance.

    Use export_all_projects on the source instance, then switch instances
    and call this to recreate everything on the target.

    Handles: label deduplication, project hierarchy, kanban views/buckets,
    tasks with remapped IDs, reminders, relations, and comments.

    Returns: {dry_run, labels_created, labels_skipped, projects_created,
              views_created, buckets_created, tasks_created,
              relations_created, comments_created, errors}
    """
    return _import_all_projects_impl(export_data, dry_run=dry_run)


@mcp.tool()
@mcp_tool_with_fallback
def project_list(
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance.")
) -> list[dict]:
    """
    List all Vikunja projects.

    Returns projects with IDs, titles, descriptions, and parent relationships.
    Use project IDs when creating tasks or listing tasks.
    """
    return _list_projects_impl(instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def project_get(
    project_id: int = Field(description="ID of the project to retrieve"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the project's instance or you'll get 403.")
) -> dict:
    """
    Get details of a specific project.

    Returns project ID, title, description, color, and parent project ID.
    """
    return _get_project_impl(project_id, instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def project_create(
    title: str = Field(description="Title of the new project"),
    description: str = Field(default="", description="Optional project description"),
    hex_color: str = Field(default="", description="Color in hex format (e.g., '#3498db')"),
    parent_project_id: int = Field(default=0, description="Parent project ID for nesting (0 = top-level)")
) -> dict:
    """
    Queue a new Vikunja project for creation.

    IMPORTANT: When called from Vikunja bot (@eis), projects are NOT created instantly.
    Instead, they are queued for the user to create. The user will receive a link to
    complete the creation process using their active Vikunja session.

    Returns a queued project spec with status "queued_for_creation".
    The user must click the provided link to finalize creation.

    Use parent_project_id to create nested/child projects.
    Multiple projects created in one turn are batched together.
    """
    return _create_project_impl(title, description, hex_color, parent_project_id)


@mcp.tool()
@mcp_tool_with_fallback
def project_delete(
    project_id: int = Field(description="ID of the project to delete")
) -> dict:
    """
    Delete a project and all its tasks.

    WARNING: This permanently deletes the project and all contained tasks.
    Returns confirmation of deletion.
    """
    return _delete_project_impl(project_id)


@mcp.tool()
@mcp_tool_with_fallback
def project_update(
    project_id: int = Field(description="ID of the project to update"),
    title: str = Field(default="", description="New title (empty = keep current)"),
    description: str = Field(default="", description="New description (empty = keep current)"),
    hex_color: str = Field(default="", description="New color in hex format (empty = keep current)"),
    parent_project_id: int = Field(default=-1, description="New parent project ID (-1 = keep current, 0 = move to root, >0 = reparent under that project)"),
    position: float = Field(default=-1, description="Position for ordering (-1 = keep current, lower = earlier in list)")
) -> dict:
    """
    Update a project's properties including its parent (reparenting) and position.

    Use parent_project_id to move projects in the hierarchy:
    - -1: Don't change parent (default)
    - 0: Move to root level (top-level project)
    - >0: Move under the specified parent project

    Use position to reorder projects within their parent:
    - -1: Don't change position (default)
    - Lower values appear first in the list
    - Use list_projects to see current positions

    WARNING: Reparenting has known bugs in Vikunja. Back up first with export_all_projects.
    """
    return _update_project_impl(project_id, title, description, hex_color, parent_project_id, position)


@mcp.tool()
@mcp_tool_with_fallback
def project_export(
    include_comments: bool = Field(default=False, description="Include task comments (one API call per task — slow on large instances)")
) -> dict:
    """
    Export all projects and tasks for backup.

    Returns a complete snapshot of all projects with their tasks.
    Use before major restructuring operations.

    Returns: {exported_at, project_count, task_count, labels: [...], projects: [{id, title, ..., views: [...], tasks: [{..., relations: [...], comments?: [...]}]}]}
    """
    return _export_all_projects_impl(include_comments=include_comments)


def _list_tasks_impl(project_id: int, include_completed: bool = False, label_filter: str = "", instance: str = None) -> list[dict]:
    # Fetch all pages for the project
    response = _fetch_all_pages("GET", f"/api/v1/projects/{project_id}/tasks", per_page=50, max_pages=100, instance=instance)
    tasks = [_format_task(t) for t in response]
    if not include_completed:
        tasks = [t for t in tasks if not t["done"]]
    if label_filter:
        # Filter by label name (case-insensitive partial match)
        label_lower = label_filter.lower()
        tasks = [t for t in tasks if any(label_lower in l["title"].lower() for l in t["labels"])]
    return tasks


def _get_task_impl(task_id: int, instance: str = None) -> dict:
    response = _request("GET", f"/api/v1/tasks/{task_id}", instance=instance)
    return _format_task(response)


def _convert_local_to_utc(date_str: str) -> str:
    """Convert a datetime string from local timezone to UTC.

    If the datetime has no timezone (e.g., '2025-01-06T18:00:00'), treats it
    as the user's local timezone (from instance config) and converts to UTC.

    If already has timezone (ends with Z or has offset), returns as-is.
    Date-only strings (no time) are returned as-is.

    Returns: UTC datetime string (with Z suffix if converted)
    """
    if not date_str:
        return date_str

    # Date-only strings don't need timezone conversion
    if "T" not in date_str:
        return date_str

    # Already has timezone info - return as-is
    if date_str.endswith("Z") or "+" in date_str or date_str.count("-") > 2:
        # Count dashes: 2025-01-06T18:00:00 has 2, 2025-01-06T18:00:00-08:00 has 3
        if "+" in date_str[10:] or date_str[10:].count("-") > 0:
            return date_str
        if date_str.endswith("Z"):
            return date_str

    # No timezone - assume local timezone from config
    local_tz_name = _get_instance_timezone()
    if not local_tz_name:
        # No timezone configured - assume UTC (backward compatible)
        return date_str if date_str.endswith("Z") else date_str + "Z"

    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo(local_tz_name)

        # Parse naive datetime
        naive_dt = datetime.fromisoformat(date_str)

        # Attach user's timezone, then convert to UTC
        local_dt = naive_dt.replace(tzinfo=local_tz)
        utc_dt = local_dt.astimezone(timezone.utc)

        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        # Fallback: return as-is with Z suffix
        return date_str if date_str.endswith("Z") else date_str + "Z"


def _validate_and_fix_date(date_str: str, field_name: str = "due_date") -> tuple[str, str | None]:
    """Validate date, convert timezone if needed, and auto-correct if in past near year boundary.

    Returns: (corrected_date, warning_message) or (original_date, None)

    Processing steps:
    1. Convert from local timezone to UTC (if datetime has no timezone suffix)
    2. Auto-correct Jan/Feb dates to next year if created in Dec (year boundary fix)
    3. Warn if date is more than 1 day in past
    """
    if not date_str:
        return date_str, None

    # Step 1: Convert local timezone to UTC
    date_str = _convert_local_to_utc(date_str)

    try:
        # Parse the date (handle various ISO formats)
        if "T" in date_str:
            # Full datetime: 2025-01-06T10:00:00Z
            parsed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        else:
            # Date only: 2025-01-06
            parsed = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)

        # Check if date is in the past
        if parsed < now:
            days_in_past = (now - parsed).days

            # Near year boundary (Jan/Feb date created in Dec): auto-correct
            if parsed.month <= 2 and now.month >= 11 and days_in_past < 365:
                # Likely meant next year
                corrected = parsed.replace(year=parsed.year + 1)
                corrected_str = corrected.strftime("%Y-%m-%dT%H:%M:%SZ") if "T" in date_str else corrected.strftime("%Y-%m-%d")
                warning = f"⚠️ Auto-corrected {field_name} from {date_str[:10]} to {corrected_str[:10]} (assumed next year)"
                return corrected_str, warning

            # Date is in past but not near year boundary - warn only
            if days_in_past > 1:  # More than 1 day in past
                warning = f"⚠️ Warning: {field_name} {date_str[:10]} is {days_in_past} days in the past"
                return date_str, warning

        return date_str, None

    except (ValueError, TypeError):
        # Can't parse date, pass through
        return date_str, None


def _create_task_impl(project_id: int, title: str, description: str = "", start_date: str = "", end_date: str = "", due_date: str = "", priority: int = 0, repeat_after: int = 0, repeat_mode: int = 0, instance: Optional[str] = None) -> dict:
    # Security: Sanitize title (strip HTML) and description (escape HTML before markdown)
    data = {"title": _sanitize_title(title)}
    warnings = []

    if description:
        # If already HTML, pass through; otherwise sanitize and convert markdown
        if _is_html(description):
            data["description"] = description
        else:
            data["description"] = md_to_html(_sanitize_description(description))
    if start_date:
        start_date, warn = _validate_and_fix_date(start_date, "start_date")
        data["start_date"] = start_date
        if warn:
            warnings.append(warn)
    if end_date:
        end_date, warn = _validate_and_fix_date(end_date, "end_date")
        data["end_date"] = end_date
        if warn:
            warnings.append(warn)
    if due_date:
        due_date, warn = _validate_and_fix_date(due_date, "due_date")
        data["due_date"] = due_date
        if warn:
            warnings.append(warn)
    if priority:
        data["priority"] = priority
    if repeat_after > 0:
        data["repeat_after"] = repeat_after
        data["repeat_mode"] = repeat_mode

    response = _request("PUT", f"/api/v1/projects/{project_id}/tasks", instance=instance, json=data)
    # Invalidate caches since task list changed
    _invalidate_ics_cache(instance or _get_current_instance())
    _invalidate_task_list_cache()

    result = _format_task(response)
    if warnings:
        result["_warnings"] = warnings
    return result


def _update_task_impl(task_id: int, title: str = "", description: str = "", start_date: str = "", end_date: str = "", due_date: str = "", priority: int = -1, repeat_after: int = -1, repeat_mode: int = -1, instance: Optional[str] = None) -> dict:
    # Vikunja API replaces the task, so we must GET first and merge changes
    current = _request("GET", f"/api/v1/tasks/{task_id}", instance=instance)
    warnings = []

    # Only update fields that were explicitly provided
    # Security: Sanitize title (strip HTML) and description (escape HTML before markdown)
    if title:
        current["title"] = _sanitize_title(title)
    if description:
        # If already HTML, pass through; otherwise sanitize and convert markdown
        if _is_html(description):
            current["description"] = description
        else:
            current["description"] = md_to_html(_sanitize_description(description))
    if start_date:
        start_date, warn = _validate_and_fix_date(start_date, "start_date")
        current["start_date"] = start_date
        if warn:
            warnings.append(warn)
    if end_date:
        end_date, warn = _validate_and_fix_date(end_date, "end_date")
        current["end_date"] = end_date
        if warn:
            warnings.append(warn)
    if due_date:
        due_date, warn = _validate_and_fix_date(due_date, "due_date")
        current["due_date"] = due_date
        if warn:
            warnings.append(warn)
    if priority >= 0:
        current["priority"] = priority
    if repeat_after >= 0:
        current["repeat_after"] = repeat_after
    if repeat_mode >= 0:
        current["repeat_mode"] = repeat_mode

    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current, instance=instance)
    # Invalidate caches since task may affect calendar
    _invalidate_ics_cache(instance or _get_current_instance())
    _invalidate_task_list_cache()

    result = _format_task(response)
    if warnings:
        result["_warnings"] = warnings
    return result


def _complete_task_impl(task_id: int) -> dict:
    # GET first to preserve other fields
    current = _request("GET", f"/api/v1/tasks/{task_id}")
    current["done"] = True
    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)
    # Invalidate caches since task status changed
    _invalidate_ics_cache(_get_current_instance())
    _invalidate_task_list_cache()
    return _format_task(response)


def _delete_task_impl(task_id: int) -> dict:
    _request("DELETE", f"/api/v1/tasks/{task_id}")
    # Invalidate caches since task removed
    _invalidate_ics_cache(_get_current_instance())
    _invalidate_task_list_cache()
    return {"deleted": True, "task_id": task_id}


def _batch_delete_tasks_impl(task_ids: list[int]) -> dict:
    """Delete multiple tasks at once.

    Args:
        task_ids: List of task IDs to delete

    Returns:
        Summary of deleted and failed tasks
    """
    deleted = []
    failed = []
    for task_id in task_ids:
        try:
            _request("DELETE", f"/api/v1/tasks/{task_id}")
            deleted.append(task_id)
        except Exception as e:
            failed.append({"task_id": task_id, "error": str(e)})

    # Invalidate caches once at the end
    if deleted:
        _invalidate_ics_cache(_get_current_instance())
        _invalidate_task_list_cache()

    return {
        "deleted_count": len(deleted),
        "deleted_ids": deleted,
        "failed_count": len(failed),
        "failed": failed if failed else None
    }


def _set_task_position_impl(
    task_id: int,
    project_id: int,
    view_id: int,
    bucket_id: int,
    apply_sort: bool = False
) -> dict:
    """
    Move a task to a kanban bucket.

    If apply_sort=True, calculates the correct position based on the bucket's
    sort strategy from project config (instead of just appending).
    """
    # Add task to bucket
    bucket_data = {
        "max_permission": None,
        "task_id": task_id,
        "bucket_id": bucket_id,
        "project_view_id": view_id,
        "project_id": project_id
    }
    _request("POST", f"/api/v1/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks", json=bucket_data)


    # CRITICAL: Always make the second API call to commit the bucket assignment (Call 2)
    # This matches the Python wrapper behavior and what the UI does
    position_data = {
        "max_permission": None,
        "project_view_id": view_id,
        "task_id": task_id
    }
    _request("POST", f"/api/v1/tasks/{task_id}/position", json=position_data)
    result = {"task_id": task_id, "bucket_id": bucket_id, "view_id": view_id, "position_set": True}

    if not apply_sort:
        return result

    # Get project config for sort strategy
    config_result = _get_project_config_impl(project_id)
    project_config = config_result.get("config")
    if not project_config:
        return result

    sort_strategy = project_config.get("sort_strategy", {})
    default_strategy = sort_strategy.get("default", "manual")
    bucket_strategies = sort_strategy.get("buckets", {})

    # Get bucket name from bucket_id
    buckets = _list_buckets_impl(project_id, view_id)
    bucket_name = None
    for b in buckets:
        if b["id"] == bucket_id:
            bucket_name = b["title"]
            break

    if not bucket_name:
        return result

    # Get sort strategy for this bucket
    strategy = bucket_strategies.get(bucket_name, default_strategy)
    if strategy == "manual":
        return result

    # Fetch the task to get its sort key value
    task = _get_task_impl(task_id)

    # Fetch existing tasks in bucket with positions
    existing_raw = _get_bucket_tasks_raw(project_id, view_id, bucket_id)
    # Filter out the task we just moved (it's now in the bucket)
    existing_raw = [t for t in existing_raw if t["id"] != task_id]

    # Build sorted list of (sort_key, position) for existing tasks
    existing_sorted = []
    for t in existing_raw:
        key = _get_task_sort_key(t, strategy)
        pos = t.get("position", 0)
        existing_sorted.append((key, pos))
    existing_sorted.sort(key=lambda x: x[0])

    # Get sort key for the moved task
    new_key = _get_task_sort_key(task, strategy)

    # Extract just the sort keys for bisect
    existing_keys = [x[0] for x in existing_sorted]

    # Binary search to find insertion point
    insert_idx = bisect.bisect_left(existing_keys, new_key)

    # Calculate position between neighbors
    if not existing_sorted:
        new_pos = 1000.0
    elif insert_idx == 0:
        first_pos = existing_sorted[0][1]
        new_pos = first_pos / 2 if first_pos > 0 else -1000.0
    elif insert_idx >= len(existing_sorted):
        last_pos = existing_sorted[-1][1]
        new_pos = last_pos + 1000.0
    else:
        prev_pos = existing_sorted[insert_idx - 1][1]
        next_pos = existing_sorted[insert_idx][1]
        new_pos = (prev_pos + next_pos) / 2

    # Set the position
    _set_view_position_impl(task_id, view_id, new_pos)
    result["position_set"] = True
    result["position"] = new_pos

    return result


def _add_label_to_task_impl(task_id: int, label_id: int) -> dict:
    # Calendar label mutual exclusion: calendar, calendar-busy, calendar-private
    # are the same concept (calendar visibility) at different privacy levels.
    # Adding one removes the others.
    _calendar_label_mutual_exclusion(task_id, label_id)

    try:
        _request("PUT", f"/api/v1/tasks/{task_id}/labels", json={"label_id": label_id})
    except ValueError as e:
        # A label id Vikunja no longer knows (deleted + recreated) → drop this
        # account's per-user cache so the next resolve name-scans (today/08 §5;
        # auggie #2 MEDIUM). Then re-raise: the caller still sees the failure.
        if _per_user_override() and _label_not_found_error(e):
            uid = _current_user_id.get() or ""
            if uid:
                try:
                    from . import label_cache
                    label_cache.forget(uid, _account_key(""))
                except ImportError:
                    pass  # server-side cache; absent in the extracted package (fa-sxac)
        raise
    # Invalidate caches since label may be "calendar"
    _invalidate_ics_cache(_get_current_instance())
    _invalidate_task_list_cache()
    return {"task_id": task_id, "label_id": label_id, "added": True}


def _label_not_found_error(e: Exception) -> bool:
    """Vikunja's answer to a label id that doesn't exist: 404, or 400 with the
    'label does not exist' code (4004)."""
    msg = str(e)
    return "API error (404)" in msg or ('"code":4004' in msg) or ("label does not exist" in msg.lower())


def _calendar_label_mutual_exclusion(task_id: int, new_label_id: int) -> None:
    """If adding a calendar-family label, remove conflicting calendar labels."""
    instance = _get_current_instance()
    if not instance:
        return

    config = _load_config()
    special = config.get("special_labels", {}).get(instance, {})
    calendar_ids = {v for k, v in special.items() if k.startswith("calendar")}

    if new_label_id not in calendar_ids:
        return  # Not a calendar label, nothing to do

    # Remove other calendar labels from this task
    try:
        task = _request("GET", f"/api/v1/tasks/{task_id}")
        for lbl in (task.get("labels") or []):
            if lbl["id"] in calendar_ids and lbl["id"] != new_label_id:
                try:
                    _request("DELETE", f"/api/v1/tasks/{task_id}/labels/{lbl['id']}")
                    logger.info(f"[special_labels] Removed conflicting label {lbl['id']} from task {task_id}")
                except Exception:
                    pass  # Best effort
    except Exception:
        pass  # Task might not exist yet (new task flow)


def _assign_user_impl(task_id: int, user_id: int) -> dict:
    _request("PUT", f"/api/v1/tasks/{task_id}/assignees", json={"user_id": user_id})
    return {"task_id": task_id, "user_id": user_id, "assigned": True}


def _unassign_user_impl(task_id: int, user_id: int) -> dict:
    _request("DELETE", f"/api/v1/tasks/{task_id}/assignees/{user_id}")
    return {"task_id": task_id, "user_id": user_id, "unassigned": True}


@mcp.tool()
@mcp_tool_with_fallback
def task_list(
    project_id: int = Field(description="ID of the project to list tasks from"),
    include_completed: bool = Field(default=False, description="Whether to include completed tasks"),
    label_filter: str = Field(default="", description="Filter by label name (case-insensitive partial match, e.g., 'Sourdough' or '🍞')"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the project's instance or you'll get 403.")
) -> list[dict]:
    """
    List tasks in a Vikunja project.

    Returns tasks with IDs, titles, descriptions, priorities, due dates, labels, and assignees.
    By default excludes completed tasks. Use include_completed=true to see all.
    Use label_filter to find tasks with specific labels (e.g., label_filter="Sourdough").
    """
    return _list_tasks_impl(project_id, include_completed, label_filter, instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def task_get(
    task_id: int = Field(description="ID of the task to retrieve"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the task's instance or you'll get 403.")
) -> dict:
    """
    Get details of a specific task.

    Returns full task details including labels, assignees, and bucket placement.
    """
    return _get_task_impl(task_id, instance=instance or None)


def _invalidate_project_instance_cache():
    """Clear the project->instance map cache (call on project create/delete)."""
    global _project_instance_cache
    _project_instance_cache = {}


def _instances_containing_project(project_id: int) -> list[str]:
    """Return the configured instance names whose project set includes project_id.

    Multi-instance MCP mode only. project_ids are per-instance (their id spaces
    can collide across instances), so this may return 0, 1, or (rarely) >1 names.
    Results are cached briefly (see _PROJECT_INSTANCE_CACHE_TTL_SECONDS).
    """
    cached = _project_instance_cache.get("map")
    if cached is not None:
        result, ts = cached
        if time.time() - ts < _PROJECT_INSTANCE_CACHE_TTL_SECONDS:
            return result.get(project_id, [])

    # (Re)build the full project_id -> [instances] map in one parallel sweep.
    # max_pages=100 (=5000 projects/instance) matches _fetch_all_pages' own
    # default so a large instance isn't silently truncated — a truncated page
    # would drop the owning project_id and misroute the create back to current.
    results = _fetch_all_pages_from_all_instances(
        "GET", "/api/v1/projects", per_page=50, max_pages=100
    )
    pmap: dict = {}
    for instance_name, data in results.items():
        if not isinstance(data, list):
            continue  # error dict for this instance — skip it
        for project in data:
            pid = project.get("id")
            if pid is not None:
                pmap.setdefault(pid, []).append(instance_name)
    _project_instance_cache["map"] = (pmap, time.time())
    return pmap.get(project_id, [])


def _resolve_instance_for_project(project_id: int, instance: Optional[str]) -> Optional[str]:
    """Infer the instance that owns project_id when none was explicitly given.

    fa-tghu: MCP task creation should land on the instance that actually owns the
    target project, instead of silently using the current/default instance — which
    otherwise 403s or, worse, creates the task in a same-numbered project on the
    WRONG account. Only fires in multi-instance MCP mode with no explicit instance.

    Resolution:
    - explicit instance given            -> honor it (the caller was explicit)
    - per-user / user-context / bot mode -> current (single effective instance;
                                            the ambient token already routes right)
    - fewer than 2 instances configured  -> current (nothing to infer)
    - current instance already owns it   -> current (fast path, no misroute possible)
    - exactly one OTHER instance owns it -> that instance (the fix)
    - zero owners, or ambiguous (>1)     -> current (surface the real API error as before)
    """
    if instance:
        return instance

    current = _get_current_instance()

    # Inference is a multi-instance-MCP concern only. In per-user / user-context /
    # bot mode there is a single effective instance and cross-instance project
    # fetches would be both wrong (foreign tokens) and costly.
    if _per_user_override() or _current_user_id.get() or _bot_mode.get():
        return current

    if len(_get_instances()) < 2:
        return current

    try:
        owners = _instances_containing_project(project_id)
    except Exception as e:
        # Never fail a create on an inference hiccup — fall back to current.
        logger.warning(f"[instance-aware] project {project_id} inference failed: {e}")
        return current

    if current in owners:
        return current  # current owns it — no misroute possible, keep it
    if len(owners) == 1:
        logger.info(
            f"[instance-aware] fa-tghu: project {project_id} routed to "
            f"'{owners[0]}' (current '{current}' does not own it)"
        )
        return owners[0]
    # zero owners (unknown/new project) or ambiguous collision — keep current and
    # let the API return its real error rather than guessing.
    return current


@mcp.tool()
@mcp_tool_with_fallback
def task_create(
    project_id: int = Field(description="ID of the project to create the task in"),
    title: str = Field(description="Title of the task"),
    description: str = Field(default="", description="Optional task description"),
    start_date: str = Field(default="", description="Event start time (ISO format) - sets DTSTART in calendar feed (Google Cal/Outlook)"),
    end_date: str = Field(default="", description="Event end time (ISO format) - sets DTEND in calendar feed (Google Cal/Outlook)"),
    due_date: str = Field(default="", description="Due date in ISO format - for deadlines/Upcoming view"),
    priority: int = Field(default=0, description="Priority: 0=none, 1=low, 2=medium, 3=high, 4=urgent, 5=critical"),
    repeat_after: int = Field(default=0, description="Repeat interval in seconds (0=no repeat, 86400=daily, 604800=weekly)"),
    repeat_mode: int = Field(default=0, description="0=from due date, 1=monthly, 2=from completion date"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the project's instance or you'll get 403.")
) -> dict:
    """
    Create a new task in a Vikunja project.

    Date fields (ISO format YYYY-MM-DDTHH:MM:SSZ):
    - start_date + end_date: Calendar event block — sets DTSTART/DTEND in the ICS
      feed so the event appears correctly in Google Calendar and Outlook. Use actual
      start and end times (e.g., "2pm-4pm" → start=22:00Z, end=00:00Z next day for PST).
    - due_date: Deadline — used for Vikunja's "Upcoming" view and task list sorting.
      Also used as DTSTART when no start_date is set.

    TIMEZONE: All dates must be in UTC (Z suffix). Check the user's timezone via
    get_user_metadata or config before converting — e.g., if the user says "noon"
    and their timezone is America/Los_Angeles (PST=UTC-8, PDT=UTC-7), store 20:00Z
    (PST) or 19:00Z (PDT). Vikunja's web UI does NOT convert local time to UTC,
    so times entered there are often stored as UTC literally — be aware when reading
    back task times set via the UI.

    GANTT: Only use full-day spans (T00:00:00Z / T23:59:00Z) if the user explicitly
    wants Gantt bar visibility. For calendar events, always use real times.

    Priority levels: 0=none, 1=low, 2=medium, 3=high, 4=urgent, 5=critical

    Recurring tasks: Set repeat_after to interval in seconds. Common values:
    - 86400 = daily, 604800 = weekly, 2592000 = ~monthly (30 days)
    repeat_mode (Vikunja enum — 1 is MONTHLY, not from-completion):
                 0 = next due date calculated from original due date
                 1 = repeat monthly (calendar month, ignores repeat_after)
                 2 = next due date calculated from completion (current) date
    """
    # fa-tghu: land on the instance that actually owns project_id instead of the
    # ambient current/default one (which caused wrong-account creations / 403s).
    requested = instance or None
    resolved = _resolve_instance_for_project(project_id, requested)
    result = _create_task_impl(project_id, title, description, start_date, end_date, due_date, priority, repeat_after, repeat_mode, instance=resolved)
    if resolved and requested is None and resolved != _get_current_instance():
        # Confirm the inferred landing so the agent/user can see it went elsewhere.
        result["_instance"] = resolved
        result["_routing_note"] = (
            f"Task created on instance '{resolved}' (owner of project {project_id}), "
            f"not the current instance."
        )
    return result


def _find_or_create_label(name: str, hex_color: str = "#4caf50", instance: Optional[str] = None) -> int:
    """Find a label by name (case-insensitive) or create it if not found.

    Returns the label ID. For user-created labels only.
    For system labels (calendar, calendar-busy), use _get_special_label_id().
    """
    labels = _list_labels_impl(instance=instance)
    for label in labels:
        if label.get("title", "").lower() == name.lower():
            return label["id"]

    # Label not found, create it
    new_label = _create_label_impl(name, hex_color, instance=instance)
    return new_label["id"]


def _reserved_label_conflict(title: str) -> Optional[str]:
    """Return the reserved-behavior description if ``title`` collides (case-
    insensitively) with a reserved label keyword, else None."""
    return RESERVED_LABEL_KEYWORDS.get((title or "").strip().lower())


def _label_cache():
    """The per-user label cache module, or None where it is not available.

    The cache is server-side (a DB table keyed by user and account) and is not published,
    so callers must be able to ask for it and carry on without it. Returning None rather
    than raising lets the per-user branch be skipped as a whole — guarding only the
    import would leave the four `label_cache.…` calls in that branch undefined (fa-sxac).
    """
    try:
        from . import label_cache
        return label_cache
    except ImportError:
        return None


def _get_special_label_id(label_name: str, instance: str = None) -> int:
    """Get label ID for a special (system) label, creating if needed.

    Resolution order:
    1. Config lookup (instant, no API call)
    2. Name scan via API (fallback, writes ID to config)
    3. Create label (first-time setup, writes ID to config)

    Args:
        label_name: Special label name (e.g., "calendar", "calendar-busy")
        instance: Instance name, or None for current instance

    Returns:
        Vikunja label ID
    """
    if label_name not in _SPECIAL_LABEL_NAMES:
        # Not a special label — fall back to name-based resolution
        return _find_or_create_label(label_name, _SPECIAL_LABEL_COLORS.get(label_name, "#4caf50"), instance=instance)

    # fa-bglr.7: under a per-user calendar override, the shared config's special_labels
    # are keyed by the OWNER's instance namespace. A name collision (e.g. both the owner
    # and the user have an instance called "default") would hand back the OWNER's label
    # id → a 403 when applied to the USER's task — and writing the user's id back into
    # the shared config would poison every other user. So resolve straight against the
    # user's own Vikunja by name-scan/create (these API calls already ride the override
    # token) and DON'T touch the shared config cache.
    label_cache = _label_cache()
    if _per_user_override() and label_cache is not None:
        # today/08 §5: the per-user cache lives in the DB, keyed (user, plain account
        # name) — the same key the read surfaces use — so a hit costs no API call and
        # the shared config is never touched. Absent the cache module we fall through to
        # the shared-config path below, which is the correct answer, just slower.
        uid = _current_user_id.get() or ""
        key = _account_key(instance or "")
        if uid:
            cached = label_cache.get(uid, key, label_name)
            if cached:
                return cached
        for label in _list_labels_impl(instance=instance):
            if label.get("title", "").lower() == label_name.lower():
                if uid:
                    label_cache.put(uid, key, label_name, label["id"])
                return label["id"]
        new_label = _create_label_impl(
            label_name, _SPECIAL_LABEL_COLORS.get(label_name, "#4caf50"), instance=instance)
        if uid:
            label_cache.put(uid, key, label_name, new_label["id"])
        return new_label["id"]

    instance = instance or _get_current_instance()
    if not instance:
        # No instance context — fall back to name-based
        return _find_or_create_label(label_name, _SPECIAL_LABEL_COLORS.get(label_name, "#4caf50"))

    config = _load_config()
    stored_id = config.get("special_labels", {}).get(instance, {}).get(label_name)
    if stored_id:
        return stored_id

    # Not in config — resolve by name scan, then store. Scan/create MUST target
    # `instance`, not the ambient one: labels are per-instance, so resolving a
    # non-current instance's label against the current one yields a foreign label
    # id (and a 403 when applied to a task in the real instance).
    labels = _list_labels_impl(instance=instance)
    for label in labels:
        if label.get("title", "").lower() == label_name.lower():
            _store_special_label_id(instance, label_name, label["id"])
            return label["id"]

    # Label doesn't exist yet — create it on the target instance
    new_label = _create_label_impl(label_name, _SPECIAL_LABEL_COLORS.get(label_name, "#4caf50"), instance=instance)
    _store_special_label_id(instance, label_name, new_label["id"])
    return new_label["id"]


def _store_special_label_id(instance: str, label_name: str, label_id: int) -> None:
    """Store a special label ID in config for future lookups."""
    config = _load_config()
    if "special_labels" not in config:
        config["special_labels"] = {}
    if instance not in config["special_labels"]:
        config["special_labels"][instance] = {}
    config["special_labels"][instance][label_name] = label_id
    _save_config(config)
    logger.info(f"[special_labels] Stored {label_name}={label_id} for instance {instance}")


def _add_to_calendar_impl(
    project_id: int,
    title: str,
    due_date: str,
    description: str = "",
    start_date: str = "",
    end_date: str = "",
    label_name: str = "calendar"
) -> dict:
    """Create a task and add the calendar label to it."""
    # Create the task
    task = _create_task_impl(
        project_id=project_id,
        title=title,
        description=description,
        start_date=start_date,
        end_date=end_date,
        due_date=due_date
    )

    # Resolve calendar label — special labels use config-based IDs
    if label_name in _SPECIAL_LABEL_NAMES:
        label_id = _get_special_label_id(label_name)
    else:
        label_id = _find_or_create_label(label_name)

    # Add label to task
    _add_label_to_task_impl(task["id"], label_id)

    task["calendar_label"] = label_name
    task["added_to_calendar"] = True
    return task


@mcp.tool()
@mcp_tool_with_fallback
def cal_add_event(
    project_id: int = Field(description="ID of the project to create the task in"),
    title: str = Field(description="Title of the calendar event"),
    due_date: str = Field(description="Due date/time in ISO format (YYYY-MM-DDTHH:MM:SSZ)"),
    description: str = Field(default="", description="Optional event description"),
    start_date: str = Field(default="", description="Event start time (ISO format) - sets DTSTART; defaults to due_date if omitted"),
    end_date: str = Field(default="", description="Event end time (ISO format) - sets DTEND; defaults to due_date+1hr if omitted"),
    label_name: str = Field(default="calendar", description="Label name to add (default: 'calendar')")
) -> dict:
    """
    Add an event to the calendar by creating a task with the 'calendar' label.

    This creates a task and automatically adds the specified label (default: 'calendar').
    Tasks with this label appear in the ICS calendar feed.

    ## Privacy: which label to use

    - label_name='calendar' (default): full title and description are visible in the
      calendar app to anyone who can see the calendar. Use for neutral events: social
      plans, classes, cooking schedules, public meetups.

    - label_name='calendar-busy': shows only as "Busy" in the calendar app with a
      private link. Use when the title itself is sensitive: business meetings, investor
      calls, medical appointments, job interviews, confidential discussions.

    When in doubt, ask the user whether they want the event private or visible.
    Err toward asking rather than guessing wrong in either direction.

    ## Timezone
    All dates must be UTC (Z suffix). Check the user's timezone before converting
    natural language times — e.g., "noon Pacific" = 20:00Z (PST) or 19:00Z (PDT).
    User timezone is stored in config under users.{user_id}.timezone_override.

    Use get_calendar_url to get the subscription URL for Google Calendar/Outlook.
    """
    return _add_to_calendar_impl(project_id, title, due_date, description, start_date, end_date, label_name)


def _anchor_occasion_date(date_str: str, today=None) -> str:
    """Resolve an occasion's date to the next on/after-today occurrence.

    Accepts a full date ('1990-03-03'), a bare month-day ('03-03', 'March 3'),
    or anything dateutil parses; the YEAR is ignored — the event recurs yearly.
    Returns 'YYYY-MM-DD'. Feb 29 falls back to Feb 28 in non-leap target years.
    """
    from dateutil import parser as dateparser
    from datetime import date as _date

    if today is None:
        today = datetime.now(timezone.utc).date()
    # Parse against a fixed LEAP-year Jan-1 reference: fills a bare month-day
    # without leaking today's day, and lets '02-29' parse (a non-leap default
    # year makes dateutil reject Feb 29). We only keep month/day anyway.
    parsed = dateparser.parse(date_str, default=datetime(2000, 1, 1))
    month, day = parsed.month, parsed.day

    def _mk(year):
        if month == 2 and day == 29:
            try:
                return _date(year, 2, 29)
            except ValueError:
                return _date(year, 2, 28)
        return _date(year, month, day)

    cand = _mk(today.year)
    if cand < today:
        cand = _mk(today.year + 1)
    return cand.isoformat()


def _occasion_canonical_md(date_str: str) -> str:
    """Return the canonical zero-padded 'MM-DD' for an occasion date, year-agnostic.

    Parses against a leap-year reference so a bare '02-29' is accepted and
    preserved (a non-leap default year makes dateutil reject Feb 29).
    """
    from dateutil import parser as dateparser
    parsed = dateparser.parse(date_str, default=datetime(2000, 1, 1))
    return f"{parsed.month:02d}-{parsed.day:02d}"


def _read_anno_md(description: str = "", fallback_due: str = "") -> Optional[str]:
    """Canonical 'MM-DD' for an anno task.

    Prefers the stored marker (survives a Feb-29 clamp); falls back to the due
    date's month/day when no marker is present (correct except it cannot recover
    a canonical Feb 29 that was already clamped away).
    """
    if description:
        m = _ANNO_MARKER_RE.search(description)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    if fallback_due and fallback_due != "0001-01-01T00:00:00Z":
        try:
            d = datetime.fromisoformat(fallback_due.replace("Z", "+00:00"))
            return f"{d.month:02d}-{d.day:02d}"
        except (ValueError, TypeError):
            pass
    return None


@mcp.tool()
@mcp_tool_with_fallback
def task_update(
    task_id: int = Field(description="ID of the task to update"),
    title: str = Field(default="", description="New title (empty = keep current)"),
    description: str = Field(default="", description="New description (empty = keep current)"),
    start_date: str = Field(default="", description="Event start time (ISO format) - sets DTSTART in calendar feed (empty = keep current)"),
    end_date: str = Field(default="", description="Event end time (ISO format) - sets DTEND in calendar feed (empty = keep current)"),
    due_date: str = Field(default="", description="Due date in ISO format - for deadlines (empty = keep current)"),
    priority: int = Field(default=-1, description="New priority (-1 = keep current, 0-5 to set)"),
    repeat_after: int = Field(default=-1, description="Repeat interval in seconds (-1=keep, 0=disable, 86400=daily)"),
    repeat_mode: int = Field(default=-1, description="0=from due date, 1=monthly, 2=from completion (-1=keep)"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance.")
) -> dict:
    """
    Update an existing task.

    Only specified fields are updated. Use empty strings or -1 to keep current values.
    start_date + end_date control the calendar event block (DTSTART/DTEND in Google Cal/Outlook).
    due_date is for deadlines and the Upcoming view.

    TIMEZONE: All dates must be UTC (Z suffix). Check the user's timezone before
    converting natural language times — stored in config under
    users.{user_id}.timezone_override (e.g., America/Los_Angeles = PST UTC-8 / PDT UTC-7).

    Recurring tasks: Set repeat_after to 0 to disable recurrence, or positive seconds for interval.
    """
    if instance:
        _tok = _forced_instance.set(instance)
        try:
            return _update_task_impl(task_id, title, description, start_date, end_date, due_date, priority, repeat_after, repeat_mode)
        finally:
            _forced_instance.reset(_tok)
    return _update_task_impl(task_id, title, description, start_date, end_date, due_date, priority, repeat_after, repeat_mode)


@mcp.tool()
@mcp_tool_with_fallback
def task_complete(
    task_id: int = Field(description="ID of the task to mark as complete"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance.")
) -> dict:
    """
    Mark a task as complete (done=true).

    Returns the updated task.
    """
    if instance:
        _tok = _forced_instance.set(instance)
        try:
            return _complete_task_impl(task_id)
        finally:
            _forced_instance.reset(_tok)
    return _complete_task_impl(task_id)


@mcp.tool()
@mcp_tool_with_fallback
def task_delete(
    task_id: int = Field(description="ID of the task to delete"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance.")
) -> dict:
    """
    Delete a task permanently.

    Returns confirmation of deletion.
    """
    if instance:
        _tok = _forced_instance.set(instance)
        try:
            return _delete_task_impl(task_id)
        finally:
            _forced_instance.reset(_tok)
    return _delete_task_impl(task_id)


@mcp.tool()
@mcp_tool_with_fallback
def task_set_position(
    task_id: int = Field(description="ID of the task to move"),
    project_id: int = Field(description="ID of the project containing the task"),
    view_id: int = Field(description="ID of the kanban view (get from get_kanban_view)"),
    bucket_id: int = Field(description="ID of the target bucket (get from list_buckets)"),
    apply_sort: bool = Field(default=False, description="If true, calculate correct position based on bucket's sort strategy from project config")
) -> dict:
    """
    Move a task to a kanban bucket.

    First use get_kanban_view to get the view_id, then list_buckets to find bucket_id.

    If apply_sort=True, the task will be positioned according to the bucket's
    sort_strategy from project config (e.g., by start_date). Otherwise, it's
    appended to the bucket.
    """
    return _set_task_position_impl(task_id, project_id, view_id, bucket_id, apply_sort)


@mcp.tool()
@mcp_tool_with_fallback
def task_add_label(
    task_id: int = Field(description="ID of the task"),
    label_id: int = Field(description="ID of the label to add (get from list_labels)")
) -> dict:
    """
    Add a label to a task.

    Use list_labels to find available label IDs.
    """
    return _add_label_to_task_impl(task_id, label_id)


@mcp.tool()
@mcp_tool_with_fallback
def task_assign_user(
    task_id: int = Field(description="ID of the task"),
    user_id: int = Field(description="ID of the user to assign")
) -> dict:
    """
    Assign a user to a task.

    Returns confirmation of assignment.
    """
    return _assign_user_impl(task_id, user_id)


@mcp.tool()
@mcp_tool_with_fallback
def task_unassign_user(
    task_id: int = Field(description="ID of the task"),
    user_id: int = Field(description="ID of the user to unassign")
) -> dict:
    """
    Remove a user from a task.

    Returns confirmation of removal.
    """
    return _unassign_user_impl(task_id, user_id)


def _format_reminder_input(reminder: str) -> dict:
    """Format a reminder datetime string into API format."""
    return {
        "reminder": reminder,
        "relative_period": 0,
        "relative_to": ""
    }


def _set_reminders_impl(task_id: int, reminders: list[str]) -> dict:
    """Set reminders on a task. Replaces all existing reminders."""
    # GET current task to preserve other fields
    current = _request("GET", f"/api/v1/tasks/{task_id}")
    # Convert datetime strings to reminder objects with required fields
    current["reminders"] = [_format_reminder_input(r) for r in reminders]
    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)
    return _format_task(response)


@mcp.tool()
@mcp_tool_with_fallback
def task_set_reminders(
    task_id: int = Field(description="ID of the task"),
    reminders: list[str] = Field(description="List of reminder datetimes in ISO format (e.g., ['2025-12-20T10:00:00Z']). Pass empty list to clear all reminders.")
) -> dict:
    """
    Set reminders on a task.

    Replaces all existing reminders with the provided list.
    Each reminder is an ISO datetime when a notification will be sent.
    Pass an empty list to clear all reminders.

    TIMEZONE: Reminder times must be UTC (Z suffix). Check users.{user_id}.timezone_override
    before converting natural language times (e.g., America/Los_Angeles = UTC-8 PST / UTC-7 PDT).

    Example: reminders=["2025-12-19T09:00:00Z", "2025-12-19T13:00:00Z"]
    """
    return _set_reminders_impl(task_id, reminders)


def _format_comment(comment: dict) -> dict:
    """Format comment for MCP response."""
    author = comment.get("author") or {}
    return {
        "id": comment["id"],
        "comment": comment.get("comment", ""),
        "author_id": author.get("id", 0),
        "author_username": author.get("username", ""),
        "author_name": author.get("name", ""),
        "created": comment.get("created"),
        "updated": comment.get("updated"),
        "task_id": comment.get("task_id", 0),
    }


def _add_comment_impl(task_id: int, comment_text: str, author_name: str = None,
                      instance: Optional[str] = None) -> dict:
    """Add a comment to a task.

    Args:
        task_id: Task ID to add comment to
        comment_text: Comment text (supports markdown)
        author_name: Optional custom author name (for agent/system comments)
        instance: Optional instance override (the task's instance)

    Returns:
        Created comment dict
    """
    # Security: Sanitize comment text (escape HTML before markdown)
    if _is_html(comment_text):
        # Already HTML, pass through
        data = {"comment": comment_text}
    else:
        # Sanitize and convert markdown to HTML
        data = {"comment": md_to_html(_sanitize_description(comment_text))}

    # Note: author_name is not directly supported by Vikunja API
    # Comments are always created by the authenticated user
    # If author_name is provided, we could prepend it to the comment text
    if author_name:
        data["comment"] = f"<p><strong>{_sanitize_title(author_name)}:</strong></p>\n{data['comment']}"

    response = _request("PUT", f"/api/v1/tasks/{task_id}/comments", json=data, instance=instance)
    return _format_comment(response)


def _get_comments_impl(task_id: int, instance: Optional[str] = None) -> list[dict]:
    """Get all comments for a task.

    Args:
        task_id: Task ID to get comments for
        instance: Optional instance override (the task's instance)

    Returns:
        List of comment dicts, sorted by creation date (oldest first)
    """
    response = _request("GET", f"/api/v1/tasks/{task_id}/comments", instance=instance)
    # Response is a list of comments
    if isinstance(response, dict):
        # In case API returns wrapped response
        comments = response.get("comments", [])
    else:
        comments = response

    return [_format_comment(c) for c in comments]


def _update_comment_impl(task_id: int, comment_id: int, comment_text: str,
                         instance: Optional[str] = None) -> dict:
    """Edit an existing comment on a task.

    Args:
        task_id: Task the comment belongs to
        comment_id: ID of the comment to edit
        comment_text: New comment text (markdown or HTML)
        instance: Optional instance override (the task's instance)

    Returns:
        Updated comment dict
    """
    if _is_html(comment_text):
        data = {"comment": comment_text}
    else:
        data = {"comment": md_to_html(_sanitize_description(comment_text))}
    response = _request("POST", f"/api/v1/tasks/{task_id}/comments/{comment_id}",
                        json=data, instance=instance)
    return _format_comment(response)


def _delete_comment_impl(task_id: int, comment_id: int,
                         instance: Optional[str] = None) -> dict:
    """Delete a comment from a task.

    Args:
        task_id: Task the comment belongs to
        comment_id: ID of the comment to delete
        instance: Optional instance override (the task's instance)

    Returns:
        {"success": True, "deleted_comment_id": comment_id}
    """
    _request("DELETE", f"/api/v1/tasks/{task_id}/comments/{comment_id}", instance=instance)
    return {"success": True, "deleted_comment_id": comment_id}


def _list_recent_comments_impl(project_id: int, limit: int = 10,
                               instance: Optional[str] = None) -> list[dict]:
    """List recent comments across tasks in a project.

    Args:
        project_id: Project ID to get comments from
        limit: Maximum number of comments to return
        instance: Optional instance override (the project's instance)

    Returns:
        List of recent comments, sorted by creation date (newest first)
    """
    all_comments = []

    # Get tasks from specific project
    tasks = _list_tasks_impl(project_id, include_completed=True, label_filter="", instance=instance)
    for task in tasks:
        try:
            comments = _get_comments_impl(task["id"], instance=instance)
            all_comments.extend(comments)
        except Exception as e:
            logger.warning(f"Failed to get comments for task {task['id']}: {e}")
            continue

    # Sort by created date (newest first)
    all_comments.sort(key=lambda c: c.get("created", ""), reverse=True)

    # Return limited results
    return all_comments[:limit]


@mcp.tool()
@mcp_tool_with_fallback
def comment_add(
    task_id: int = Field(description="ID of the task to add comment to"),
    comment_text: str = Field(description="Comment text (supports markdown)"),
    author_name: str = Field(default=None, description="Optional custom author name (for agent/system comments)"),
    instance: str = Field(default="", description="Instance the task lives on (e.g. 'personal'). Empty = active instance. MUST match the task's instance or you'll get a 403.")
) -> dict:
    """
    Add a comment to a task.

    Comments support markdown formatting. If author_name is provided, it will be
    prepended to the comment text (useful for agent/system comments).

    Returns the created comment with id, text, author info, and timestamps.
    """
    return _add_comment_impl(task_id, comment_text, author_name, instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def comment_list(
    task_id: int = Field(description="ID of the task to get comments from"),
    instance: str = Field(default="", description="Instance the task lives on (e.g. 'personal'). Empty = active instance. MUST match the task's instance or you'll get a 403.")
) -> list[dict]:
    """
    Get all comments for a task.

    Returns a list of comments sorted by creation date (oldest first).
    Each comment includes id, text, author info, and timestamps.
    """
    return _get_comments_impl(task_id, instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def comment_update(
    task_id: int = Field(description="ID of the task the comment belongs to"),
    comment_id: int = Field(description="ID of the comment to edit"),
    comment_text: str = Field(description="New comment text (supports markdown)"),
    instance: str = Field(default="", description="Instance the task lives on (e.g. 'personal'). Empty = active instance. MUST match the task's instance or you'll get a 403.")
) -> dict:
    """
    Edit an existing comment on a task.

    Replaces the comment body. Returns the updated comment with id, text, author
    info, and timestamps.
    """
    return _update_comment_impl(task_id, comment_id, comment_text, instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def comment_delete(
    task_id: int = Field(description="ID of the task the comment belongs to"),
    comment_id: int = Field(description="ID of the comment to delete"),
    instance: str = Field(default="", description="Instance the task lives on (e.g. 'personal'). Empty = active instance. MUST match the task's instance or you'll get a 403.")
) -> dict:
    """
    Delete a comment from a task.

    Returns {"success": true, "deleted_comment_id": <id>}.
    """
    return _delete_comment_impl(task_id, comment_id, instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def comment_recent(
    project_id: int = Field(description="Project ID to get recent comments from"),
    limit: int = Field(default=10, description="Maximum number of comments to return"),
    instance: str = Field(default="", description="Instance the project lives on (e.g. 'personal'). Empty = active instance. MUST match the project's instance or you'll get a 403.")
) -> list[dict]:
    """
    List recent comments across tasks in a project.

    Returns comments sorted by creation date (newest first), limited to the
    specified number of comments.
    """
    return _list_recent_comments_impl(project_id, limit, instance=instance or None)


def _list_labels_impl(instance: Optional[str] = None) -> list[dict]:
    # Fetch all pages for labels (instance-scoped — labels are per-instance)
    response = _fetch_all_pages("GET", "/api/v1/labels", per_page=50, max_pages=10, instance=instance)
    return [_format_label(l) for l in response]


def _create_label_impl(title: str, hex_color: str, instance: Optional[str] = None) -> dict:
    # Security: Sanitize title (strip HTML)
    data = {"title": _sanitize_title(title), "hex_color": hex_color}
    response = _request("PUT", "/api/v1/labels", json=data, instance=instance)
    return _format_label(response)


def _delete_label_impl(label_id: int) -> dict:
    _request("DELETE", f"/api/v1/labels/{label_id}")
    return {"deleted": True, "label_id": label_id}


def _analyze_project_dimensions_impl(project_id: int) -> dict:
    """Analyze a project's data to suggest meaningful kanban groupings.

    Returns available labels, priorities in use, assignees, and suggested
    kanban configurations based on the actual data.
    """
    # Validate project exists first
    try:
        project = _request("GET", f"/api/v1/projects/{project_id}")
        project_title = project.get("title", f"Project {project_id}")
    except ValueError as e:
        if "not found" in str(e).lower() or "does not exist" in str(e).lower():
            return {
                "error": f"Project {project_id} not found",
                "project_id": project_id,
                "suggestion": "Use list_projects to find valid project IDs"
            }
        raise

    # Get all tasks for this project
    tasks = _fetch_all_pages("GET", f"/api/v1/projects/{project_id}/tasks", per_page=50, max_pages=100)

    # Get all labels (global, but we'll filter to those used in project)
    all_labels = _fetch_all_pages("GET", "/api/v1/labels", per_page=50, max_pages=10)

    # Analyze dimensions
    labels_in_project = {}
    priorities_in_use = set()
    assignees = {}

    for task in tasks:
        # Track priorities
        priority = task.get("priority", 0)
        if priority > 0:
            priorities_in_use.add(priority)

        # Track labels used in this project
        for label in task.get("labels") or []:
            label_id = label.get("id")
            if label_id not in labels_in_project:
                labels_in_project[label_id] = {
                    "id": label_id,
                    "title": label.get("title", ""),
                    "hex_color": label.get("hex_color", ""),
                    "task_count": 0
                }
            labels_in_project[label_id]["task_count"] += 1

        # Track assignees
        for assignee in task.get("assignees") or []:
            user_id = assignee.get("id")
            username = assignee.get("username", assignee.get("name", f"user_{user_id}"))
            if user_id not in assignees:
                assignees[user_id] = {
                    "id": user_id,
                    "username": username,
                    "task_count": 0
                }
            assignees[user_id]["task_count"] += 1

    # Build suggestions
    suggestions = []

    # Suggest label-based kanban if multiple labels exist
    labels_list = sorted(labels_in_project.values(), key=lambda x: -x["task_count"])
    if len(labels_list) >= 2:
        top_labels = labels_list[:5]  # Top 5 by usage
        suggestions.append({
            "name": "By Label",
            "description": f"Group by labels: {', '.join(l['title'] for l in top_labels)}",
            "bucket_configuration_mode": "filter",
            "buckets": [
                {"title": l["title"], "filter": f"labels in {l['id']}", "task_count": l["task_count"]}
                for l in top_labels
            ]
        })

    # Suggest priority-based kanban if multiple priorities used
    if len(priorities_in_use) >= 2:
        priority_names = {5: "🔥 Urgent (P5)", 4: "High (P4)", 3: "Medium (P3)", 2: "Low (P2)", 1: "Minimal (P1)"}
        sorted_priorities = sorted(priorities_in_use, reverse=True)
        suggestions.append({
            "name": "By Priority",
            "description": f"Group by priority levels: {', '.join(str(p) for p in sorted_priorities)}",
            "bucket_configuration_mode": "filter",
            "buckets": [
                {"title": priority_names.get(p, f"Priority {p}"), "filter": f"priority = {p}"}
                for p in sorted_priorities
            ]
        })

    # Suggest assignee-based kanban if multiple assignees
    assignees_list = sorted(assignees.values(), key=lambda x: -x["task_count"])
    if len(assignees_list) >= 2:
        suggestions.append({
            "name": "By Assignee",
            "description": f"Group by who's responsible: {', '.join(a['username'] for a in assignees_list[:5])}",
            "bucket_configuration_mode": "filter",
            "buckets": [
                {"title": a["username"], "filter": f"assignees in {a['id']}", "task_count": a["task_count"]}
                for a in assignees_list[:5]
            ]
        })

    # Always suggest a status-based option
    suggestions.append({
        "name": "By Status",
        "description": "Traditional workflow: To Do → In Progress → Done",
        "bucket_configuration_mode": "manual",
        "buckets": [
            {"title": "To Do", "filter": None},
            {"title": "In Progress", "filter": None},
            {"title": "Done", "filter": None}
        ],
        "note": "Requires manually dragging tasks between buckets"
    })

    return {
        "project_id": project_id,
        "project_title": project_title,
        "task_count": len(tasks),
        "labels": labels_list,
        "priorities_in_use": sorted(priorities_in_use, reverse=True),
        "assignees": assignees_list,
        "suggested_kanbans": suggestions,
        "recommendation": suggestions[0]["name"] if suggestions else "By Status"
    }


@mcp.tool()
@mcp_tool_with_fallback
def project_analyze(
    project_id: int = Field(description="Project ID to analyze")
) -> dict:
    """
    Analyze a project's data to discover meaningful grouping dimensions.

    CALL THIS BEFORE creating a kanban view to understand the data.

    Returns:
    - labels: Labels used in this project with task counts
    - priorities_in_use: Priority levels that have tasks
    - assignees: Team members with assigned tasks
    - suggested_kanbans: Ready-to-use kanban configurations based on actual data

    The suggested_kanbans include filter queries you can use directly with
    create_view and create_bucket to build meaningful views.
    """
    return _analyze_project_dimensions_impl(project_id)


@mcp.tool()
@mcp_tool_with_fallback
def label_list() -> list[dict]:
    """
    List all available labels.

    Returns labels with IDs, titles, and colors.
    Use label IDs with add_label_to_task.
    """
    return _list_labels_impl()


@mcp.tool()
@mcp_tool_with_fallback
def label_create(
    title: str = Field(description="Label title"),
    hex_color: str = Field(description="Color in hex format (e.g., '#FF0000' for red)")
) -> dict:
    """
    Create a new label.

    Returns the created label with its assigned ID. If the title collides with a
    reserved keyword (fa-2y1n), the label is still created but the result carries
    a non-blocking ``reserved_warning`` — the system's own label creation goes
    through _create_label_impl directly and never triggers this.
    """
    result = _create_label_impl(title, hex_color)
    note = _reserved_label_conflict(title)
    if note:
        result["reserved_warning"] = (
            f"'{title}' is a reserved keyword: {note} A label with this title will "
            f"trigger that system behavior across all instances — rename it if that "
            f"isn't intended."
        )
    return result


@mcp.tool()
@mcp_tool_with_fallback
def label_delete(
    label_id: int = Field(description="ID of the label to delete")
) -> dict:
    """
    Delete a label.

    Returns confirmation of deletion.
    """
    return _delete_label_impl(label_id)


def _list_views_impl(project_id: int, instance: str = None) -> list[dict]:
    """List all views for a project (list, kanban, gantt, table)."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views", instance=instance)
    return [_format_view(v) for v in response]


def _get_view_tasks_impl(project_id: int, view_id: int) -> list[dict]:
    """Get tasks via a specific view endpoint - returns tasks with bucket info for kanban views."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/tasks")
    # Kanban views return buckets with nested tasks
    # List/Gantt views return flat task arrays
    tasks = []
    for item in response:
        if "tasks" in item:
            # This is a bucket - extract tasks with bucket info
            bucket_id = item["id"]
            bucket_title = item["title"]
            for task in (item.get("tasks") or []):
                formatted = _format_task(task)
                formatted["bucket_id"] = bucket_id
                formatted["bucket_title"] = bucket_title
                tasks.append(formatted)
        else:
            # This is a task (non-kanban view)
            tasks.append(_format_task(item))
    return tasks


def _list_tasks_by_bucket_impl(project_id: int, view_id: int) -> dict:
    """Get tasks grouped by bucket for kanban views."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/tasks")
    buckets = {}
    for item in response:
        if "tasks" in item:
            bucket_name = item["title"]
            buckets[bucket_name] = {
                "bucket_id": item["id"],
                "tasks": [_format_task(t) for t in (item.get("tasks") or [])]
            }
    return buckets


def _get_bucket_tasks_raw(project_id: int, view_id: int, bucket_id: int) -> list[dict]:
    """Get raw tasks in a specific bucket (includes position field)."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/tasks")
    for item in response:
        if item.get("id") == bucket_id and "tasks" in item:
            return item.get("tasks") or []
    return []


def _get_single_sort_key(task: dict, strategy: str):
    """Extract a single sort key from a task dict."""
    if strategy == "start_date":
        return task.get("start_date") or "9999-12-31"
    elif strategy == "due_date":
        return task.get("due_date") or "9999-12-31"
    elif strategy == "end_date":
        return task.get("end_date") or "9999-12-31"
    elif strategy == "priority":
        return -(task.get("priority") or 0)  # Negative for descending (high priority first)
    elif strategy in ("alphabetical", "title"):
        return (task.get("title") or "").lower()
    elif strategy in ("id", "created"):
        return task.get("id") or 0
    elif strategy == "position":
        return task.get("position") or 0
    return 0


def _get_task_sort_key(task: dict, strategy: str, then_by: str = None):
    """Extract sort key(s) from a task dict (API response format).

    Returns a tuple for stable multi-level sorting when then_by is provided.
    """
    primary = _get_single_sort_key(task, strategy)
    if then_by:
        secondary = _get_single_sort_key(task, then_by)
        return (primary, secondary)
    return primary


def _get_input_sort_key(task_input: dict, created_task: dict, strategy: str):
    """Extract sort key from task input (used during batch create)."""
    if strategy == "start_date":
        return task_input.get("start_date") or "9999-12-31"
    elif strategy == "due_date":
        return task_input.get("due_date") or "9999-12-31"
    elif strategy == "end_date":
        return task_input.get("end_date") or "9999-12-31"
    elif strategy == "priority":
        return -(task_input.get("priority") or 0)
    elif strategy == "alphabetical":
        return task_input.get("title", "").lower()
    elif strategy in ("id", "created"):
        return created_task.get("id") or 0
    return 0


def _set_view_position_impl(task_id: int, view_id: int, position: float) -> dict:
    """Set a task's position within a specific view (for Gantt ordering, etc.)."""
    response = _request("POST", f"/api/v1/tasks/{task_id}/position", json={
        "project_view_id": view_id,
        "position": position
    })
    return response


def _get_kanban_view_impl(project_id: int) -> dict:
    response = _request("GET", f"/api/v1/projects/{project_id}/views")
    kanban_views = [v for v in response if v.get("view_kind") == "kanban"]
    if not kanban_views:
        raise ValueError(f"No kanban view found for project {project_id}")
    return _format_view(kanban_views[0])


def _list_buckets_impl(project_id: int, view_id: int) -> list[dict]:
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/buckets")
    return [_format_bucket(b) for b in response]


def _create_bucket_impl(project_id: int, view_id: int, title: str, position: int = 0, limit: int = 0) -> dict:
    data = {"title": title, "position": position, "limit": limit}
    response = _request("PUT", f"/api/v1/projects/{project_id}/views/{view_id}/buckets", json=data)
    return _format_bucket(response)


def _delete_bucket_impl(project_id: int, view_id: int, bucket_id: int) -> dict:
    _request("DELETE", f"/api/v1/projects/{project_id}/views/{view_id}/buckets/{bucket_id}")
    return {"deleted": True, "bucket_id": bucket_id}


def _create_view_impl(project_id: int, title: str, view_kind: str, filter_query: str = None, delete_default_buckets: bool = True) -> dict:
    """Create a new view for a project.

    Args:
        project_id: Project ID
        title: View title
        view_kind: View type (list, kanban, gantt, table)
        filter_query: Optional filter query string
        delete_default_buckets: For kanban views, delete the auto-created
            To-Do/Doing/Done buckets (default True). Set False to keep them.
    """
    # Get existing views to determine position for new view (place at end)
    existing_views = _request("GET", f"/api/v1/projects/{project_id}/views")
    max_position = max([v.get("position", 0) for v in existing_views], default=0)

    data = {
        "title": title,
        "view_kind": view_kind,
        "position": max_position + 100  # Place at end with 100-unit spacing
    }

    # CRITICAL: For kanban views, set bucket_configuration_mode to "manual"
    # Without this, Vikunja defaults to "none" which groups by labels,
    # causing each task to appear as its own column instead of in buckets
    if view_kind == "kanban":
        data["bucket_configuration_mode"] = "manual"

    # Add filter if provided (Vikunja expects filter as a string, not an object)
    # Fix: solutions-nwidy
    if filter_query:
        data["filter"] = filter_query

    response = _request("PUT", f"/api/v1/projects/{project_id}/views", json=data)
    view_id = response.get("id")

    # For kanban views, Vikunja auto-creates default To-Do/Doing/Done buckets.
    # Delete them if delete_default_buckets is True (default) so user can create custom buckets.
    if view_kind == "kanban" and delete_default_buckets and view_id:
        try:
            buckets = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/buckets")
            for bucket in buckets:
                bucket_id = bucket.get("id")
                if bucket_id:
                    try:
                        _request("DELETE", f"/api/v1/projects/{project_id}/views/{view_id}/buckets/{bucket_id}")
                    except Exception:
                        pass  # Ignore errors deleting individual buckets
        except Exception:
            pass  # Ignore errors listing/deleting buckets

    return _format_view(response)


def _delete_view_impl(project_id: int, view_id: int) -> dict:
    """
    Delete a view.
    
    Args:
        project_id: Project ID
        view_id: View ID to delete
    
    Returns:
        Success message
    """
    _request("DELETE", f"/api/v1/projects/{project_id}/views/{view_id}")
    
    return {
        "success": True,
        "message": f"Deleted view {view_id} from project {project_id}"
    }


def _update_view_impl(project_id: int, view_id: int, title: str = None, filter_query: str = None) -> dict:
    """Update a view's title and/or filter."""
    data = {}
    
    if title is not None:
        data["title"] = title
    
    # Vikunja expects filter as a string, not an object
    # Fix: solutions-nwidy
    if filter_query is not None:
        data["filter"] = filter_query
    
    response = _request("POST", f"/api/v1/projects/{project_id}/views/{view_id}", json=data)
    return _format_view(response)


def _setup_kanban_board_impl(
    project_id: int = None,
    project_title: str = None,
    template: str = "gtd",
    custom_buckets: list = None,
    view_title: str = "Kanban",
    delete_default_buckets: bool = True,
    migrate_tasks: dict = None
) -> dict:
    """
    Rapid kanban board setup with templates and task migration.

    This is the ONE tool agents should use for kanban board creation.
    Replaces 26+ API calls with a single tool call.

    Can optionally create the project too (project_title param).
    """
    project_created = False

    # 1. Create project if project_title provided (and no project_id)
    if project_title and not project_id:
        project_data = {"title": project_title}
        project = _request("PUT", "/api/v1/projects", json=project_data)
        project_id = project["id"]
        project_created = True

    if not project_id:
        raise ValueError("Either project_id or project_title is required")

    # 2. Get or create kanban view
    views = _request("GET", f"/api/v1/projects/{project_id}/views")
    kanban_views = [v for v in views if v.get("view_kind") == "kanban" and v.get("title") == view_title]

    # Track default views/buckets to clean up later
    default_kanban_view_id = None
    if project_created:
        # New projects have a default kanban view we'll want to delete
        default_kanbans = [v for v in views if v.get("view_kind") == "kanban" and v.get("title") != view_title]
        if default_kanbans:
            default_kanban_view_id = default_kanbans[0]["id"]

    if kanban_views:
        view = kanban_views[0]
    else:
        # Create new kanban view with CRITICAL bucket_configuration_mode
        max_position = max([v.get("position", 0) for v in views], default=0)
        view_data = {
            "title": view_title,
            "view_kind": "kanban",
            "bucket_configuration_mode": "manual",  # CRITICAL
            "position": max_position + 100
        }
        view = _request("PUT", f"/api/v1/projects/{project_id}/views", json=view_data)

    view_id = view["id"]

    # 3. Get bucket configuration
    if template == "custom":
        if not custom_buckets:
            raise ValueError("custom_buckets required when template='custom'")
        buckets_config = custom_buckets
    else:
        if template not in KANBAN_TEMPLATES:
            raise ValueError(f"Unknown template: {template}. Available: {list(KANBAN_TEMPLATES.keys())}")
        buckets_config = KANBAN_TEMPLATES[template]

    # 4. Create template buckets
    created_buckets = []
    for config in buckets_config:
        bucket_data = {
            "title": config["title"],
            "position": config.get("position", 0),
            "limit": config.get("limit", 0)
        }
        bucket = _request("PUT", f"/api/v1/projects/{project_id}/views/{view_id}/buckets", json=bucket_data)
        created_buckets.append({
            "id": bucket["id"],
            "title": bucket["title"],
            "position": bucket.get("position", 0),
            "limit": bucket.get("limit", 0)
        })

    # 5. NOW delete default buckets (after template buckets exist, so we're not deleting the last bucket)
    buckets_deleted = 0
    if delete_default_buckets:
        existing_buckets = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/buckets")
        created_bucket_ids = {b["id"] for b in created_buckets}
        for bucket in existing_buckets:
            # Delete any bucket we didn't just create (i.e., the defaults like Backlog, Done, etc.)
            if bucket["id"] not in created_bucket_ids:
                try:
                    _request("DELETE", f"/api/v1/projects/{project_id}/views/{view_id}/buckets/{bucket['id']}")
                    buckets_deleted += 1
                except Exception:
                    pass  # Ignore errors

    # 6. Delete default kanban view if we created a new project
    if default_kanban_view_id:
        try:
            _request("DELETE", f"/api/v1/projects/{project_id}/views/{default_kanban_view_id}")
        except Exception:
            pass  # Ignore errors

    # 7. Migrate tasks if requested
    tasks_migrated = 0
    migration_summary = {}

    if migrate_tasks:
        # Get all tasks in project
        tasks = _request("GET", f"/api/v1/projects/{project_id}/tasks")

        # Build label→bucket mapping (by title)
        bucket_map = {b["title"]: b["id"] for b in created_buckets}

        for task in tasks:
            task_labels = [label.get("title") for label in task.get("labels", [])]

            # Find matching label→bucket mapping
            for label_title, bucket_title in migrate_tasks.items():
                if label_title in task_labels and bucket_title in bucket_map:
                    bucket_id = bucket_map[bucket_title]

                    # Two-step bucket assignment (CRITICAL for Vikunja API)
                    try:
                        # Step 1: Add to bucket
                        _request("POST",
                                f"/api/v1/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
                                json={"task_id": task["id"], "bucket_id": bucket_id,
                                     "project_view_id": view_id, "project_id": project_id})

                        # Step 2: Commit position
                        _request("POST", f"/api/v1/tasks/{task['id']}/position",
                                json={"project_view_id": view_id, "task_id": task["id"]})

                        tasks_migrated += 1
                        migration_summary[bucket_title] = migration_summary.get(bucket_title, 0) + 1
                    except Exception:
                        # Continue on error
                        pass
                    break  # Only assign to first matching bucket

    result = {
        "view_id": view_id,
        "view_title": view_title,
        "buckets_created": len(created_buckets),
        "buckets": created_buckets,
        "tasks_migrated": tasks_migrated,
        "migration_summary": migration_summary
    }

    if project_created:
        result["project_id"] = project_id
        result["project_created"] = True

    return result


def _bulk_relabel_tasks_impl(
    project_id: int,
    task_ids: list[int],
    add_labels: list[str] = None,
    remove_labels: list[str] = None,
    set_labels: list[str] = None
) -> dict:
    """
    Bulk update labels on multiple tasks.
    
    Args:
        project_id: Project ID (for context, labels are global)
        task_ids: List of task IDs to update
        add_labels: Labels to add (by title, will be created if needed)
        remove_labels: Labels to remove (by title)
        set_labels: Replace all labels with this list (by title)
    
    Returns:
        dict with updated_count and details
    """
    if not task_ids:
        return {"updated_count": 0, "details": []}
    
    # Get all labels (labels are global in Vikunja)
    # Note: Labels API is paginated, need to fetch all pages
    all_labels = []
    page = 1
    while True:
        labels_page = _request("GET", f"/api/v1/labels?page={page}")
        if not labels_page:
            break
        all_labels.extend(labels_page)
        page += 1
        # Safety limit
        if page > 100:
            break
    
    label_map = {label["title"]: label["id"] for label in all_labels}
    
    # Create missing labels if needed
    all_label_titles = set()
    if add_labels:
        all_label_titles.update(add_labels)
    if set_labels:
        all_label_titles.update(set_labels)
    
    for title in all_label_titles:
        if title not in label_map:
            # Create label
            new_label = _request("PUT", "/api/v1/labels", json={"title": title, "hex_color": ""})
            label_map[title] = new_label["id"]
    
    # Update each task
    results = []
    for task_id in task_ids:
        try:
            # Get current task labels
            # Note: Vikunja returns null (None) for labels, not missing key, so use `or []`
            task = _request("GET", f"/api/v1/tasks/{task_id}")
            current_label_ids = {label["id"] for label in (task.get("labels") or [])}
            
            # Calculate label changes
            if set_labels is not None:
                # Replace all labels - remove all current, add all new
                labels_to_remove = current_label_ids
                labels_to_add = {label_map[title] for title in set_labels if title in label_map}
            else:
                # Incremental changes
                labels_to_remove = set()
                labels_to_add = set()
                
                if remove_labels:
                    labels_to_remove = {label_map[title] for title in remove_labels if title in label_map}
                
                if add_labels:
                    labels_to_add = {label_map[title] for title in add_labels if title in label_map}
                    # Don't add labels that are already on the task
                    labels_to_add = labels_to_add - current_label_ids
            
            # Apply changes using dedicated endpoints
            for label_id in labels_to_remove:
                if label_id in current_label_ids:
                    _request("DELETE", f"/api/v1/tasks/{task_id}/labels/{label_id}")
            
            for label_id in labels_to_add:
                _request("PUT", f"/api/v1/tasks/{task_id}/labels", json={"label_id": label_id})
            
            final_count = len(current_label_ids - labels_to_remove | labels_to_add)
            results.append({"task_id": task_id, "success": True, "label_count": final_count})
        except Exception as e:
            results.append({"task_id": task_id, "success": False, "error": str(e)})
    
    updated_count = sum(1 for r in results if r["success"])
    return {
        "updated_count": updated_count,
        "total_tasks": len(task_ids),
        "details": results
    }


def _bulk_set_task_positions_impl(
    project_id: int,
    view_id: int,
    assignments: list[dict]
) -> dict:
    """
    Bulk assign tasks to buckets in a kanban view.
    
    Args:
        project_id: Project ID
        view_id: View ID
        assignments: List of {task_id, bucket_id, position?} dicts
    
    Returns:
        dict with moved_count, tasks, and errors
    """
    if not assignments:
        return {"moved_count": 0, "tasks": [], "errors": []}
    
    results = []
    errors = []
    
    for assignment in assignments:
        task_id = assignment["task_id"]
        bucket_id = assignment["bucket_id"]
        position = assignment.get("position")
        
        try:
            # Use existing set_task_position implementation
            _set_task_position_impl(
                task_id=task_id,
                project_id=project_id,
                view_id=view_id,
                bucket_id=bucket_id,
                apply_sort=False
            )
            results.append({"task_id": task_id, "bucket_id": bucket_id, "success": True})
        except Exception as e:
            errors.append({"task_id": task_id, "bucket_id": bucket_id, "error": str(e)})
    
    return {
        "moved_count": len(results),
        "total_assignments": len(assignments),
        "tasks": results,
        "errors": errors
    }


def _move_tasks_by_label_to_buckets_impl(
    project_id: int,
    view_id: int,
    label_to_bucket_map: dict[str, int]
) -> dict:
    """
    Move tasks to buckets based on label→bucket mappings.
    
    Args:
        project_id: Project ID
        view_id: View ID
        label_to_bucket_map: Dict mapping label titles to bucket IDs
            e.g., {"🎯 Phase 1: MVP": 39602, "🚀 Phase 2: Content": 39603}
    
    Returns:
        dict with moved_count, by_label breakdown, and errors
    """
    if not label_to_bucket_map:
        return {"moved_count": 0, "by_label": {}, "errors": []}
    
    # Get all labels for ID resolution (labels are global, paginated)
    all_labels = []
    page = 1
    while True:
        labels_page = _request("GET", f"/api/v1/labels?page={page}")
        if not labels_page:
            break
        all_labels.extend(labels_page)
        page += 1
        if page > 100:  # Safety limit
            break
    
    label_id_map = {label["title"]: label["id"] for label in all_labels}
    
    # Get all tasks in the project
    all_tasks = _request("GET", f"/api/v1/projects/{project_id}/tasks")
    
    # Build assignments
    assignments = []
    by_label = {label_title: 0 for label_title in label_to_bucket_map.keys()}
    
    for task in all_tasks:
        task_labels = {label["title"] for label in task.get("labels", [])}
        
        # Check if task has any of the target labels
        for label_title, bucket_id in label_to_bucket_map.items():
            if label_title in task_labels:
                assignments.append({
                    "task_id": task["id"],
                    "bucket_id": bucket_id
                })
                by_label[label_title] += 1
                break  # Only assign to first matching label
    
    # Bulk assign
    result = _bulk_set_task_positions_impl(project_id, view_id, assignments)
    result["by_label"] = by_label
    
    return result


def _bulk_create_labels_impl(
    labels: list[dict]
) -> dict:
    """
    Bulk create labels.
    
    Args:
        labels: List of {title: str, hex_color?: str} dicts
    
    Returns:
        dict with created_count, labels, and errors
    """
    if not labels:
        return {"created_count": 0, "labels": [], "errors": []}
    
    # Get existing labels to avoid duplicates (labels are global, paginated)
    existing_labels = []
    page = 1
    while True:
        labels_page = _request("GET", f"/api/v1/labels?page={page}")
        if not labels_page:
            break
        existing_labels.extend(labels_page)
        page += 1
        if page > 100:  # Safety limit
            break
    
    existing_titles = {label["title"] for label in existing_labels}
    
    results = []
    errors = []
    skipped = []
    
    for label_spec in labels:
        title = label_spec.get("title")
        hex_color = label_spec.get("hex_color", "")
        
        if not title:
            errors.append({"title": None, "error": "Title is required"})
            continue
        
        # Skip if already exists
        if title in existing_titles:
            skipped.append({"title": title, "reason": "Already exists"})
            continue
        
        try:
            new_label = _request("PUT", "/api/v1/labels", 
                                json={"title": title, "hex_color": hex_color})
            results.append({
                "id": new_label["id"],
                "title": new_label["title"],
                "hex_color": new_label["hex_color"],
                "success": True
            })
            existing_titles.add(title)  # Track for subsequent labels in same batch
        except Exception as e:
            errors.append({"title": title, "error": str(e)})
    
    return {
        "created_count": len(results),
        "total_requested": len(labels),
        "labels": results,
        "skipped": skipped,
        "errors": errors
    }


def _create_filtered_view_impl(
    project_id: int,
    title: str,
    view_kind: str,
    filter_query: str,
    bucket_config_mode: str = "manual"
) -> dict:
    """
    Create a filtered view using saved filter (shows only tasks matching criteria).
    
    Note: Vikunja doesn't support filters on regular views. Instead, this creates
    a "saved filter" which is actually a virtual project that shows filtered tasks.
    The saved filter automatically gets a default view of the specified kind.
    
    Args:
        project_id: Parent project ID (for context, saved filters are cross-project)
        title: Filter/view title
        view_kind: View type ("kanban", "list", "gantt", "table")
        filter_query: Filter query (e.g., 'labels in 7350' or 'priority >= 3')
        bucket_config_mode: "manual" for custom buckets, "none" for auto-grouping
    
    Returns:
        Created saved filter dict (acts as a project)
    """
    # Create saved filter (this creates a virtual project)
    filter_data = {
        "title": title,
        "filters": filter_query
    }
    
    saved_filter = _request("PUT", "/api/v1/filters", json=filter_data)
    
    # The saved filter is now a project - update its default view to the desired kind
    filter_project_id = saved_filter["id"]
    views = _request("GET", f"/api/v1/projects/{filter_project_id}/views")
    
    if views:
        # Update the first view to the desired kind and bucket mode
        default_view = views[0]
        _request("POST", f"/api/v1/projects/{filter_project_id}/views/{default_view['id']}", 
                json={
                    "view_kind": view_kind,
                    "bucket_configuration_mode": bucket_config_mode
                })
    
    return {
        "id": saved_filter["id"],
        "title": saved_filter["title"],
        "is_saved_filter": True,
        "filter": filter_query,
        "view_kind": view_kind
    }


def _create_bucket_filtered_kanban_impl(
    project_id: int,
    title: str,
    bucket_filters: dict[str, str]
) -> dict:
    """
    Create a kanban view with filter-based buckets (project-scoped).
    
    Each bucket shows only tasks matching its filter within the project.
    
    Args:
        project_id: Project ID
        title: View title
        bucket_filters: Dict mapping bucket titles to filter queries
            e.g., {"Quick Wins": "labels in 7344", "Deep Work": "labels in 7345"}
    
    Returns:
        Created view with buckets
    """
    # Get existing views to position at end
    existing_views = _request("GET", f"/api/v1/projects/{project_id}/views")
    max_position = max([v.get("position", 0) for v in existing_views], default=0)
    
    # Create view with filter-based bucket configuration
    view_data = {
        "title": title,
        "view_kind": "kanban",
        "position": max_position + 100,
        "bucket_configuration_mode": "filter"
    }
    
    new_view = _request("PUT", f"/api/v1/projects/{project_id}/views", json=view_data)
    view_id = new_view["id"]
    
    # Create buckets with filters
    buckets = []
    position = 100
    for bucket_title, filter_query in bucket_filters.items():
        bucket_data = {
            "title": bucket_title,
            "limit": 0,
            "position": position,
            "filter": filter_query
        }
        bucket = _request("PUT", f"/api/v1/projects/{project_id}/views/{view_id}/buckets", 
                         json=bucket_data)
        buckets.append({
            "id": bucket["id"],
            "title": bucket["title"],
            "filter": filter_query
        })
        position += 100
    
    return {
        "id": view_id,
        "title": new_view["title"],
        "view_kind": "kanban",
        "bucket_configuration_mode": "filter",
        "buckets": buckets
    }


@mcp.tool()
@mcp_tool_with_fallback
def view_list(
    project_id: int = Field(description="ID of the project"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the project's instance or you'll get 403.")
) -> list[dict]:
    """
    List all views for a project.

    Returns views with IDs, titles, and view_kind (list, kanban, gantt, table).
    Use view IDs with get_view_tasks to fetch tasks via that view.
    """
    return _list_views_impl(project_id, instance=instance or None)


@mcp.tool()
@mcp_tool_with_fallback
def view_create(
    project_id: int = Field(description="ID of the project"),
    title: str = Field(description="View title (e.g., 'High Priority Tasks')"),
    view_kind: str = Field(description="View type: list, kanban, gantt, or table"),
    filter_query: str = Field(default=None, description="Optional filter query (e.g., 'priority >= 3 && done = false', 'labels in 7350')"),
    bucket_config_mode: str = Field(default="", description="Bucket mode for kanban: 'manual' (default for kanban) or 'none'. Ignored for non-kanban views.")
) -> dict:
    """
    Create a new view for a project with optional filter.

    Filter syntax: SQL-like queries with fields like done, priority, dueDate, labels.
    Examples: 'done = false', 'priority >= 3 && done = false', 'labels in 7350'
    Date math: now, now+1d, now/w (start of week)

    For filtered kanban views, use bucket_config_mode='manual' (default).
    """
    if filter_query and bucket_config_mode:
        return _create_filtered_view_impl(project_id, title, view_kind, filter_query, bucket_config_mode)
    return _create_view_impl(project_id, title, view_kind, filter_query)


@mcp.tool()
@mcp_tool_with_fallback
def view_delete(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="View ID to delete")
) -> dict:
    """
    Delete a view from a project.
    
    Use this to clean up test views or remove unwanted views.
    
    Example:
    view_delete(project_id=14259, view_id=55019)
    
    Returns: {success: true, message: "..."}
    """
    return _delete_view_impl(project_id, view_id)


@mcp.tool()
@mcp_tool_with_fallback
def view_update(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view to update"),
    title: str = Field(default=None, description="New title (optional)"),
    filter_query: str = Field(default=None, description="New filter query (optional)")
) -> dict:
    """
    Update a view's title and/or filter query.
    
    At least one of title or filter_query must be provided.
    """
    return _update_view_impl(project_id, view_id, title, filter_query)


@mcp.tool()
@mcp_tool_with_fallback
def view_get_tasks(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from list_views)")
) -> list[dict]:
    """
    Get tasks via a specific view endpoint.

    For kanban views, returns tasks with bucket_id and bucket_title populated.
    For list/gantt views, returns flat task list.
    Use list_tasks_by_bucket for grouped kanban view.
    """
    return _get_view_tasks_impl(project_id, view_id)


@mcp.tool()
@mcp_tool_with_fallback
def kanban_tasks_by_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the kanban view (get from list_views)")
) -> dict:
    """
    Get tasks grouped by kanban bucket.

    Returns dict with bucket names as keys, each containing bucket_id and tasks array.
    Use this to understand workflow state without asking user which bucket tasks are in.

    Example response: {"📝 To-Do": {"bucket_id": 123, "tasks": [...]}, "🔥 In Progress": {...}}
    """
    return _list_tasks_by_bucket_impl(project_id, view_id)


@mcp.tool()
@mcp_tool_with_fallback
def view_set_position(
    task_id: int = Field(description="ID of the task"),
    view_id: int = Field(description="ID of the view (Gantt, List, etc.)"),
    position: float = Field(description="Position value (lower = earlier in list)")
) -> dict:
    """
    Set a task's position within a specific view.

    Use this to order tasks in Gantt, List, or Table views.
    Position is a float - use increments (e.g., 1000, 2000, 3000) for easy reordering.
    """
    return _set_view_position_impl(task_id, view_id, position)


@mcp.tool()
@mcp_tool_with_fallback
def kanban_get(
    project_id: int = Field(description="ID of the project")
) -> dict:
    """
    Get the kanban view for a project.

    Returns the view ID needed for bucket operations and task positioning.
    Every project has a default kanban view created automatically.
    """
    return _get_kanban_view_impl(project_id)


@mcp.tool()
@mcp_tool_with_fallback
def kanban_list_buckets(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from get_kanban_view)")
) -> list[dict]:
    """
    List all kanban buckets (columns) in a view.

    Returns buckets with IDs, titles, positions, and WIP limits.
    Use bucket IDs with set_task_position to move tasks.
    """
    return _list_buckets_impl(project_id, view_id)


@mcp.tool()
@mcp_tool_with_fallback
def kanban_create_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from get_kanban_view)"),
    title: str = Field(description="Bucket/column title"),
    position: int = Field(default=0, description="Sort position (0 = first)"),
    limit: int = Field(default=0, description="WIP limit (0 = no limit)")
) -> dict:
    """
    Create a new kanban bucket (column).

    Returns the created bucket with its assigned ID.
    """
    return _create_bucket_impl(project_id, view_id, title, position, limit)


@mcp.tool()
@mcp_tool_with_fallback
def batch_relabel(
    project_id: int = Field(description="Project ID for label resolution"),
    task_ids: list[int] = Field(description="List of task IDs to update"),
    add_labels: list[str] = Field(default=None, description="Labels to add (will be created if needed)"),
    remove_labels: list[str] = Field(default=None, description="Labels to remove"),
    set_labels: list[str] = Field(default=None, description="Replace all labels with this list")
) -> dict:
    """
    Bulk update labels on multiple tasks. Useful for reorganizing task categorization.
    
    You can either:
    - add_labels: Add labels while keeping existing ones
    - remove_labels: Remove specific labels
    - set_labels: Replace ALL labels with a new set
    
    Labels are referenced by title and will be created if they don't exist.
    """
    return _bulk_relabel_tasks_impl(project_id, task_ids, add_labels, remove_labels, set_labels)


@mcp.tool()
@mcp_tool_with_fallback
def batch_assign_buckets(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="View ID (kanban view)"),
    assignments: list[dict] = Field(description="List of {task_id: int, bucket_id: int, position?: float}")
) -> dict:
    """
    Bulk assign tasks to buckets in a kanban view. Much faster than individual set_task_position calls.
    
    Example:
    bulk_set_task_positions(
        project_id=14259,
        view_id=55017,
        assignments=[
            {"task_id": 244095, "bucket_id": 39602},
            {"task_id": 244107, "bucket_id": 39603},
            ...
        ]
    )
    
    Returns: {moved_count, total_assignments, tasks: [{task_id, bucket_id, success}], errors: []}
    """
    return _bulk_set_task_positions_impl(project_id, view_id, assignments)


@mcp.tool()
@mcp_tool_with_fallback
def batch_label_to_buckets(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="View ID (kanban view)"),
    label_to_bucket_map: dict = Field(description="Map of label titles to bucket IDs, e.g., {'Phase 1': 39602}")
) -> dict:
    """
    Move tasks to buckets based on their labels. High-level tool for setting up kanban views.
    
    Finds all tasks with each label and moves them to the corresponding bucket.
    Useful for organizing tasks into alternative views (e.g., "By Phase" vs "By Domain").
    
    Example:
    batch_label_to_buckets(
        project_id=14259,
        view_id=55017,
        label_to_bucket_map={
            "🎯 Phase 1: MVP": 39602,
            "🚀 Phase 2: Content": 39603,
            "💎 Phase 3: Polish": 39604
        }
    )
    
    Returns: {moved_count, by_label: {"Phase 1": 8, "Phase 2": 5}, errors: []}
    """
    return _move_tasks_by_label_to_buckets_impl(project_id, view_id, label_to_bucket_map)


@mcp.tool()
@mcp_tool_with_fallback
def kanban_setup(
    project_id: int = Field(default=None, description="ID of existing project (or use project_title to create new)"),
    project_title: str = Field(default=None, description="Create new project with this title (alternative to project_id)"),
    template: str = Field(default="gtd", description="Template: gtd, sprint, kitchen, payables, talks, or custom"),
    custom_buckets: list = Field(default=None, description="For template='custom': list of {title, position, limit?}"),
    view_title: str = Field(default="Kanban", description="Name for the kanban view"),
    delete_default_buckets: bool = Field(default=True, description="Delete auto-created Backlog/Done buckets"),
    migrate_tasks: dict = Field(default=None, description="Map label titles to bucket titles for task migration")
) -> dict:
    """
    Rapid kanban board setup with templates and optional task migration.

    Creates a complete kanban board in one call with predefined workflow templates.
    Can also create the project in the same call (use project_title instead of project_id).
    Replaces 26+ API calls with a single tool call.

    Templates:
    - gtd: Getting Things Done (Inbox, Next, Waiting, Someday, Done)
    - sprint: Agile sprint (Backlog, To Do, In Progress, Review, Done)
    - kitchen: Cooking workflow (9 stages from Idea to Done)
    - payables: Finance workflow (Decision Queue, Approved, Paid)
    - talks: Speaking pipeline (Ideas, Submitted, Accepted, Preparing, Delivered)
    - custom: Use custom_buckets parameter

    Examples:
        # Existing project
        kanban_setup(project_id=123, template="sprint")

        # New project + board in one call
        kanban_setup(project_title="My Recipes", template="kitchen")

    Returns: {view_id, view_title, buckets_created, buckets: [...], project_id?, project_created?}
    """
    return _setup_kanban_board_impl(
        project_id=project_id,
        project_title=project_title,
        template=template,
        custom_buckets=custom_buckets,
        view_title=view_title,
        delete_default_buckets=delete_default_buckets,
        migrate_tasks=migrate_tasks
    )


@mcp.tool()
@mcp_tool_with_fallback
def batch_create_labels(
    labels: list[dict] = Field(description="List of {title: str, hex_color?: str} label specs")
) -> dict:
    """
    Bulk create labels. Useful for setting up a new labeling system.
    
    Skips labels that already exist (by title). hex_color is optional (defaults to empty).
    
    Example:
    bulk_create_labels(
        labels=[
            {"title": "Easy win", "hex_color": "2ECC71"},
            {"title": "Ask for help", "hex_color": "E74C3C"},
            {"title": "Visit the Video Vault", "hex_color": "3498DB"}
        ]
    )
    
    Returns: {created_count, labels: [{id, title, hex_color}], skipped: [], errors: []}
    """
    return _bulk_create_labels_impl(labels)


@mcp.tool()
@mcp_tool_with_fallback
def kanban_delete_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from get_kanban_view)"),
    bucket_id: int = Field(description="ID of the bucket to delete")
) -> dict:
    """
    Delete a kanban bucket (column).

    WARNING: Tasks in this bucket may be moved to another bucket or become unassigned.
    Returns confirmation of deletion.
    """
    return _delete_bucket_impl(project_id, view_id, bucket_id)


def _create_task_relation_impl(task_id: int, relation_kind: str, other_task_id: int) -> dict:
    data = {"other_task_id": other_task_id, "relation_kind": relation_kind}
    response = _request("PUT", f"/api/v1/tasks/{task_id}/relations", json=data)
    return {"task_id": task_id, "other_task_id": other_task_id, "relation_kind": relation_kind, "created": True}


def _list_task_relations_impl(task_id: int, instance: str = None) -> list[dict]:
    response = _request("GET", f"/api/v1/tasks/{task_id}", instance=instance)
    relations = []
    related_tasks = response.get("related_tasks") or {}
    for relation_kind, tasks in related_tasks.items():
        if tasks:
            for task in tasks:
                relations.append(_format_relation(task_id, relation_kind, task))
    return relations


@mcp.tool()
@mcp_tool_with_fallback
def task_create_relation(
    task_id: int = Field(description="ID of the source task"),
    relation_kind: str = Field(description="Relation type: 'subtask', 'parenttask', 'related', 'blocking', 'blocked', 'duplicateof', 'duplicates', 'precedes', 'follows', 'copiedfrom', 'copiedto'"),
    other_task_id: int = Field(description="ID of the target task")
) -> dict:
    """
    Create a relation between two tasks.

    Relation types:
    - subtask/parenttask: Parent-child relationship
    - blocking/blocked: Task dependencies
    - related: General association
    - precedes/follows: Sequential ordering
    - duplicateof/duplicates: Duplicate tracking
    """
    return _create_task_relation_impl(task_id, relation_kind, other_task_id)


@mcp.tool()
@mcp_tool_with_fallback
def task_list_relations(
    task_id: int = Field(description="ID of the task"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the task's instance or you'll get 403.")
) -> list[dict]:
    """
    List all relations for a task.

    Returns relations showing how this task connects to other tasks
    (blocking, subtasks, related, etc.).
    """
    return _list_task_relations_impl(task_id, instance=instance or None)


def _batch_create_tasks_impl(
    project_id: int,
    tasks: list[dict],
    create_missing_labels: bool = True,
    create_missing_buckets: bool = False,
    use_project_config: bool = True,
    apply_sort: bool = True,
    apply_default_labels: bool = False
) -> dict:
    """
    Create multiple tasks with labels, relations, and bucket positions.

    Task schema:
    {
        "title": str,              # required
        "description": str,        # optional
        "start_date": str,         # optional, ISO format (calendar DTSTART)
        "end_date": str,           # optional, ISO format (calendar DTEND)
        "due_date": str,           # optional, ISO format (for deadlines)
        "priority": int,           # optional, 0-5
        "labels": list[str],       # optional, label names
        "bucket": str,             # optional, bucket name
        "ref": str,                # optional, local reference for relations
        "blocked_by": list[str],   # optional, refs of blocking tasks
        "blocks": list[str],       # optional, refs this task blocks
        "subtask_of": str,         # optional, ref of parent task
    }

    If use_project_config=True, applies default_bucket from config.
    If apply_default_labels=True (opt-in), applies default_labels from config to tasks without labels.
    If apply_sort=True, auto-positions tasks based on sort_strategy in config.
    """
    # Load project config if enabled
    project_config = None
    if use_project_config:
        config_result = _get_project_config_impl(project_id)
        project_config = config_result.get("config")

    # Apply config defaults to tasks
    if project_config:
        default_labels = project_config.get("default_labels", [])
        default_bucket = project_config.get("default_bucket", "")

        for task in tasks:
            # Apply default labels only if opt-in and task doesn't specify any
            if apply_default_labels and not task.get("labels") and default_labels:
                task["labels"] = default_labels.copy()
            # Apply default bucket if task doesn't specify one
            if not task.get("bucket") and default_bucket:
                task["bucket"] = default_bucket
    result = {
        "created": 0,
        "tasks": [],
        "labels_created": [],
        "relations_created": 0,
        "errors": []
    }

    # Step 1: Fetch existing labels and build name→id map
    existing_labels = _list_labels_impl()
    label_map = {l["title"]: l["id"] for l in existing_labels}

    # Step 2: Find all label names needed
    needed_labels = set()
    for task in tasks:
        for label_name in task.get("labels", []):
            if label_name not in label_map:
                needed_labels.add(label_name)

    # Step 3: Create missing labels if enabled
    if create_missing_labels and needed_labels:
        # Default colors for auto-created labels
        colors = ["#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c"]
        for i, label_name in enumerate(needed_labels):
            try:
                new_label = _create_label_impl(label_name, colors[i % len(colors)])
                label_map[label_name] = new_label["id"]
                result["labels_created"].append(label_name)
            except Exception as e:
                result["errors"].append(f"Failed to create label '{label_name}': {str(e)}")

    # Step 4: Fetch kanban view and buckets for bucket positioning
    view_id = None
    bucket_map = {}  # name → id

    # Check if any task needs bucket positioning
    needs_buckets = any(task.get("bucket") for task in tasks)
    if needs_buckets:
        try:
            view = _get_kanban_view_impl(project_id)
            view_id = view["id"]
            existing_buckets = _list_buckets_impl(project_id, view_id)
            bucket_map = {b["title"]: b["id"] for b in existing_buckets}

            # Create missing buckets if enabled
            if create_missing_buckets:
                needed_buckets = set()
                for task in tasks:
                    bucket_name = task.get("bucket")
                    if bucket_name and bucket_name not in bucket_map:
                        needed_buckets.add(bucket_name)

                for i, bucket_name in enumerate(needed_buckets):
                    try:
                        new_bucket = _create_bucket_impl(project_id, view_id, bucket_name, position=len(existing_buckets) + i)
                        bucket_map[bucket_name] = new_bucket["id"]
                    except Exception as e:
                        result["errors"].append(f"Failed to create bucket '{bucket_name}': {str(e)}")
        except Exception as e:
            result["errors"].append(f"Failed to get kanban view: {str(e)}")

    # Step 5: Create all tasks and build ref→id map
    ref_map = {}  # ref → task_id
    created_tasks = []  # list of (task_input, created_task)

    for task_input in tasks:
        try:
            created_task = _create_task_impl(
                project_id=project_id,
                title=task_input["title"],
                description=task_input.get("description", ""),
                start_date=task_input.get("start_date", ""),
                end_date=task_input.get("end_date", ""),
                due_date=task_input.get("due_date", ""),
                priority=task_input.get("priority", 0)
            )

            result["created"] += 1
            result["tasks"].append({
                "ref": task_input.get("ref"),
                "id": created_task["id"],
                "title": created_task["title"]
            })

            # Track ref for relations
            ref = task_input.get("ref")
            if ref:
                ref_map[ref] = created_task["id"]

            created_tasks.append((task_input, created_task))

        except Exception as e:
            result["errors"].append(f"Failed to create task '{task_input.get('title', '?')}': {str(e)}")

    # Step 6: Add labels to tasks
    for task_input, created_task in created_tasks:
        for label_name in task_input.get("labels", []):
            label_id = label_map.get(label_name)
            if label_id:
                try:
                    _add_label_to_task_impl(created_task["id"], label_id)
                except Exception as e:
                    result["errors"].append(f"Failed to add label '{label_name}' to task {created_task['id']}: {str(e)}")
            else:
                result["errors"].append(f"Label '{label_name}' not found for task {created_task['id']}")

    # Step 7: Create relations
    for task_input, created_task in created_tasks:
        task_id = created_task["id"]

        # blocked_by: this task is blocked by other tasks
        for blocker_ref in task_input.get("blocked_by", []):
            blocker_id = ref_map.get(blocker_ref)
            if blocker_id:
                try:
                    _create_task_relation_impl(task_id, "blocked", blocker_id)
                    result["relations_created"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to create blocked relation for task {task_id}: {str(e)}")
            else:
                result["errors"].append(f"Unknown ref '{blocker_ref}' in blocked_by for task {task_id}")

        # blocks: this task blocks other tasks
        for blocked_ref in task_input.get("blocks", []):
            blocked_id = ref_map.get(blocked_ref)
            if blocked_id:
                try:
                    _create_task_relation_impl(task_id, "blocking", blocked_id)
                    result["relations_created"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to create blocking relation for task {task_id}: {str(e)}")
            else:
                result["errors"].append(f"Unknown ref '{blocked_ref}' in blocks for task {task_id}")

        # subtask_of: this task is a subtask of another
        parent_ref = task_input.get("subtask_of")
        if parent_ref:
            parent_id = ref_map.get(parent_ref)
            if parent_id:
                try:
                    _create_task_relation_impl(task_id, "parenttask", parent_id)
                    result["relations_created"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to create subtask relation for task {task_id}: {str(e)}")
            else:
                result["errors"].append(f"Unknown ref '{parent_ref}' in subtask_of for task {task_id}")

    # Step 8: Set bucket positions
    if view_id and bucket_map:
        for task_input, created_task in created_tasks:
            bucket_name = task_input.get("bucket")
            if bucket_name:
                bucket_id = bucket_map.get(bucket_name)
                if bucket_id:
                    try:
                        _set_task_position_impl(created_task["id"], project_id, view_id, bucket_id)
                    except Exception as e:
                        result["errors"].append(f"Failed to set bucket for task {created_task['id']}: {str(e)}")
                else:
                    result["errors"].append(f"Bucket '{bucket_name}' not found for task {created_task['id']}")

    # Step 9: Auto-sort tasks based on project config sort_strategy
    # This finds the correct insertion point among existing tasks
    if apply_sort and project_config and view_id:
        sort_strategy = project_config.get("sort_strategy", {})
        default_strategy = sort_strategy.get("default", "manual")
        bucket_strategies = sort_strategy.get("buckets", {})

        # Group newly created tasks by bucket
        tasks_by_bucket = {}  # bucket_name → [(task_input, created_task)]
        for task_input, created_task in created_tasks:
            bucket_name = task_input.get("bucket")
            if bucket_name:
                if bucket_name not in tasks_by_bucket:
                    tasks_by_bucket[bucket_name] = []
                tasks_by_bucket[bucket_name].append((task_input, created_task))

        # Sort and position tasks in each bucket
        for bucket_name, bucket_tasks in tasks_by_bucket.items():
            strategy = bucket_strategies.get(bucket_name, default_strategy)

            if strategy == "manual":
                # Manual: skip auto-sort (bucket position already set in Step 8)
                continue

            bucket_id = bucket_map.get(bucket_name)
            if not bucket_id:
                continue

            # Fetch existing tasks in bucket with positions
            try:
                existing_raw = _get_bucket_tasks_raw(project_id, view_id, bucket_id)
            except Exception as e:
                result["errors"].append(f"Failed to fetch existing tasks in bucket '{bucket_name}': {str(e)}")
                continue

            # Filter out the newly created tasks (they're already in the bucket from Step 8)
            new_task_ids = {created_task["id"] for _, created_task in bucket_tasks}
            existing_raw = [t for t in existing_raw if t["id"] not in new_task_ids]

            # Build sorted list of (sort_key, position) for existing tasks
            existing_sorted = []
            for task in existing_raw:
                key = _get_task_sort_key(task, strategy)
                pos = task.get("position", 0)
                existing_sorted.append((key, pos))
            existing_sorted.sort(key=lambda x: x[0])

            # Extract just the sort keys for bisect
            existing_keys = [x[0] for x in existing_sorted]

            # For each new task, find insertion point and calculate position
            for task_input, created_task in bucket_tasks:
                new_key = _get_input_sort_key(task_input, created_task, strategy)

                # Binary search to find insertion point
                insert_idx = bisect.bisect_left(existing_keys, new_key)

                # Calculate position between neighbors
                if not existing_sorted:
                    # No existing tasks - use standard position
                    new_pos = 1000.0
                elif insert_idx == 0:
                    # Insert at beginning - half of first position
                    first_pos = existing_sorted[0][1]
                    new_pos = first_pos / 2 if first_pos > 0 else -1000.0
                elif insert_idx >= len(existing_sorted):
                    # Insert at end - add gap after last
                    last_pos = existing_sorted[-1][1]
                    new_pos = last_pos + 1000.0
                else:
                    # Insert between two tasks - midpoint
                    prev_pos = existing_sorted[insert_idx - 1][1]
                    next_pos = existing_sorted[insert_idx][1]
                    new_pos = (prev_pos + next_pos) / 2

                try:
                    _set_view_position_impl(created_task["id"], view_id, new_pos)
                except Exception as e:
                    result["errors"].append(f"Failed to set position for task {created_task['id']}: {str(e)}")

                # Insert into existing_sorted for subsequent calculations
                existing_sorted.insert(insert_idx, (new_key, new_pos))
                existing_keys.insert(insert_idx, new_key)

    return result


def _setup_project_impl(
    project_id: int,
    buckets: list[str] = None,
    labels: list[dict] = None,
    tasks: list[dict] = None
) -> dict:
    """
    Set up a project with buckets, labels, and tasks in one operation.

    labels schema: [{"name": str, "color": str}]
    tasks schema: same as batch_create_tasks
    """
    buckets = buckets or []
    labels = labels or []
    tasks = tasks or []

    result = {
        "buckets_created": [],
        "labels_created": [],
        "tasks_result": None,
        "errors": []
    }

    # Step 1: Get kanban view
    view_id = None
    if buckets:
        try:
            view = _get_kanban_view_impl(project_id)
            view_id = view["id"]
        except Exception as e:
            result["errors"].append(f"Failed to get kanban view: {str(e)}")
            return result

    # Step 2: Create missing buckets
    if view_id and buckets:
        existing_buckets = _list_buckets_impl(project_id, view_id)
        existing_names = {b["title"] for b in existing_buckets}

        for i, bucket_name in enumerate(buckets):
            if bucket_name not in existing_names:
                try:
                    _create_bucket_impl(project_id, view_id, bucket_name, position=i)
                    result["buckets_created"].append(bucket_name)
                except Exception as e:
                    result["errors"].append(f"Failed to create bucket '{bucket_name}': {str(e)}")

    # Step 3: Create missing labels
    if labels:
        existing_labels = _list_labels_impl()
        existing_label_names = {l["title"] for l in existing_labels}

        for label in labels:
            label_name = label.get("name", "")
            if label_name and label_name not in existing_label_names:
                try:
                    _create_label_impl(label_name, label.get("color", "#3498db"))
                    result["labels_created"].append(label_name)
                except Exception as e:
                    result["errors"].append(f"Failed to create label '{label_name}': {str(e)}")

    # Step 4: Create tasks using batch_create_tasks
    if tasks:
        result["tasks_result"] = _batch_create_tasks_impl(
            project_id=project_id,
            tasks=tasks,
            create_missing_labels=False,  # already done above
            create_missing_buckets=False  # already done above
        )

    return result


@mcp.tool()
@mcp_tool_with_fallback
def batch_create_tasks(
    project_id: int = Field(description="ID of the project to create tasks in"),
    tasks: list[dict] = Field(description="List of task objects. Each task: {title: str (required), description: str, start_date: str (ISO, calendar DTSTART), end_date: str (ISO, calendar DTEND), due_date: str (ISO, deadline), priority: int (0-5), labels: list[str], bucket: str, ref: str, blocked_by: list[str], blocks: list[str], subtask_of: str}"),
    create_missing_labels: bool = Field(default=True, description="Auto-create labels that don't exist"),
    create_missing_buckets: bool = Field(default=False, description="Auto-create buckets that don't exist"),
    use_project_config: bool = Field(default=True, description="Apply default_bucket from project config"),
    apply_sort: bool = Field(default=True, description="Auto-position tasks based on sort_strategy in project config"),
    apply_default_labels: bool = Field(default=False, description="Apply default_labels from config to tasks without labels (opt-in)"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the project's instance or you'll get 403.")
) -> dict:
    """
    Create multiple tasks at once with labels, relations, and bucket positions.

    Reduces API calls by batching operations. Use 'ref' field to create relations
    between tasks in the same batch. Labels are matched by name (case-sensitive).

    If use_project_config=True, applies default_bucket from config.
    If apply_default_labels=True, applies default_labels from config to tasks without labels.
    If apply_sort=True, auto-positions tasks based on sort_strategy (start_date, due_date, etc.).

    start_date/end_date set the calendar event block (DTSTART/DTEND → Google Cal/Outlook).
    Use real times, not full-day spans, unless the user explicitly wants Gantt bars.

    Example:
    tasks=[
        {"title": "Team standup (9am)", "ref": "standup", "start_date": "2025-01-15T17:00:00Z", "end_date": "2025-01-15T17:30:00Z"},
        {"title": "Implement API", "ref": "impl", "blocked_by": ["standup"]},
    ]

    Returns: {created: int, tasks: [{ref, id, title}], labels_created: [], relations_created: int, errors: []}
    """
    if instance:
        _tok = _forced_instance.set(instance)
        try:
            return _batch_create_tasks_impl(project_id, tasks, create_missing_labels, create_missing_buckets, use_project_config, apply_sort, apply_default_labels)
        finally:
            _forced_instance.reset(_tok)
    return _batch_create_tasks_impl(project_id, tasks, create_missing_labels, create_missing_buckets, use_project_config, apply_sort, apply_default_labels)


@mcp.tool()
@mcp_tool_with_fallback
def project_setup(
    project_id: int = Field(description="ID of the project to set up"),
    buckets: list[str] = Field(default=[], description="Bucket names to ensure exist (created in order)"),
    labels: list[dict] = Field(default=[], description="Labels to ensure exist: [{name: str, color: str}]"),
    tasks: list[dict] = Field(default=[], description="Tasks to create (same schema as batch_create_tasks)"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance. MUST match the project's instance or you'll get 403.")
) -> dict:
    """
    Set up a project with kanban buckets, labels, and tasks in one operation.

    Higher-level tool that orchestrates bucket creation, label creation, and
    batch task creation. Use this to bootstrap a new project structure.

    Example:
    project_setup(
        project_id=1,
        buckets=["Backlog", "In Progress", "Done"],
        labels=[{"name": "bug", "color": "#e74c3c"}, {"name": "feature", "color": "#3498db"}],
        tasks=[{"title": "First task", "bucket": "Backlog", "labels": ["feature"]}]
    )

    Returns: {buckets_created: [], labels_created: [], tasks_result: {...}, errors: []}
    """
    if instance:
        _tok = _forced_instance.set(instance)
        try:
            return _setup_project_impl(project_id, buckets, labels, tasks)
        finally:
            _forced_instance.reset(_tok)
    return _setup_project_impl(project_id, buckets, labels, tasks)


def _batch_update_tasks_impl(updates: list[dict]) -> dict:
    """
    Update multiple tasks at once.

    Each update dict must have 'task_id' and any fields to update:
    title, description, start_date, end_date, due_date, priority, reminders
    """
    result = {
        "updated": 0,
        "tasks": [],
        "errors": []
    }

    for update in updates:
        task_id = update.get("task_id")
        if not task_id:
            result["errors"].append("Update missing task_id")
            continue

        try:
            # GET current task to preserve fields
            current = _request("GET", f"/api/v1/tasks/{task_id}")

            # Apply updates
            if "title" in update:
                current["title"] = update["title"]
            if "description" in update:
                current["description"] = md_to_html(update["description"])
            if "start_date" in update:
                current["start_date"] = update["start_date"]
            if "end_date" in update:
                current["end_date"] = update["end_date"]
            if "due_date" in update:
                current["due_date"] = update["due_date"]
            if "priority" in update:
                current["priority"] = update["priority"]
            if "reminders" in update:
                current["reminders"] = [_format_reminder_input(r) for r in update["reminders"]]

            # POST updated task
            response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)
            result["updated"] += 1
            result["tasks"].append({
                "id": task_id,
                "title": response.get("title", "")
            })
        except Exception as e:
            result["errors"].append(f"Failed to update task {task_id}: {str(e)}")

    return result


@mcp.tool()
@mcp_tool_with_fallback
def batch_update_tasks(
    updates: list[dict] = Field(description="List of updates. Each: {task_id: int (required), title: str, description: str, start_date: str, end_date: str, due_date: str, priority: int, reminders: list[str]}"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance.")
) -> dict:
    """
    Update multiple tasks at once.

    Saves round trips when renaming multiple tasks or setting reminders on several tasks.
    Each update must include task_id and any fields to change.

    TIMEZONE: All dates must be UTC (Z suffix). Check users.{user_id}.timezone_override
    before converting natural language times (e.g., America/Los_Angeles = UTC-8 PST / UTC-7 PDT).

    Example:
    updates=[
        {"task_id": 123, "title": "New title", "priority": 3},
        {"task_id": 456, "reminders": ["2025-12-20T10:00:00Z"]},
        {"task_id": 789, "due_date": "2025-12-25T17:00:00Z"}
    ]

    Returns: {updated: int, tasks: [{id, title}], errors: []}
    """
    if instance:
        _tok = _forced_instance.set(instance)
        try:
            return _batch_update_tasks_impl(updates)
        finally:
            _forced_instance.reset(_tok)
    return _batch_update_tasks_impl(updates)


def _batch_set_positions_impl(view_id: int, positions: list[dict]) -> dict:
    """
    Set positions for multiple tasks in a view.

    positions: [{task_id: int, position: float}, ...]
    """
    result = {
        "updated": 0,
        "tasks": [],
        "errors": []
    }

    for pos in positions:
        task_id = pos.get("task_id")
        position = pos.get("position")

        if not task_id:
            result["errors"].append("Position entry missing task_id")
            continue
        if position is None:
            result["errors"].append(f"Position entry for task {task_id} missing position")
            continue

        try:
            _set_view_position_impl(task_id, view_id, position)
            result["updated"] += 1
            result["tasks"].append({"task_id": task_id, "position": position})
        except Exception as e:
            result["errors"].append(f"Failed to set position for task {task_id}: {str(e)}")

    return result


@mcp.tool()
@mcp_tool_with_fallback
def batch_reorder_tasks(
    view_id: int = Field(description="ID of the view (get from get_kanban_view)"),
    positions: list[dict] = Field(description="List of {task_id: int, position: float}")
) -> dict:
    """
    Reorder tasks within a view by setting their positions in bulk.

    More efficient than calling set_view_position for each task individually.
    Does NOT move between buckets — use bulk_set_task_positions for that.

    Example:
    positions=[
        {"task_id": 123, "position": 1000},
        {"task_id": 456, "position": 2000},
        {"task_id": 789, "position": 3000}
    ]

    Returns: {updated: int, tasks: [{task_id, position}], errors: []}
    """
    return _batch_set_positions_impl(view_id, positions)


def _sort_bucket_impl(
    project_id: int,
    view_id: int,
    bucket_id: int,
    sort_by: str = None,
    then_by: str = None
) -> dict:
    """
    Re-sort all tasks in a bucket.

    Args:
        project_id: Project ID
        view_id: View ID
        bucket_id: Bucket ID to sort
        sort_by: Primary sort field (overrides config). Options: due_date, start_date,
                 end_date, priority, title, created, position
        then_by: Secondary sort field for ties (e.g., sort by due_date, then by title)

    Fetches all tasks in bucket, sorts by strategy, assigns new positions with gaps.
    """
    result = {
        "sorted": 0,
        "tasks": [],
        "strategy": "manual",
        "then_by": None,
        "errors": []
    }

    # Determine sort strategy
    strategy = sort_by  # Use explicit parameter if provided

    if not strategy:
        # Fall back to project config
        config_result = _get_project_config_impl(project_id)
        project_config = config_result.get("config")
        if not project_config:
            result["errors"].append("No project config found and no sort_by specified")
            return result

        sort_strategy = project_config.get("sort_strategy", {})
        default_strategy = sort_strategy.get("default", "manual")
        bucket_strategies = sort_strategy.get("buckets", {})

        # Get bucket name from bucket_id
        buckets = _list_buckets_impl(project_id, view_id)
        bucket_name = None
        for b in buckets:
            if b["id"] == bucket_id:
                bucket_name = b["title"]
                break

        if not bucket_name:
            result["errors"].append(f"Bucket {bucket_id} not found")
            return result

        strategy = bucket_strategies.get(bucket_name, default_strategy)

    result["strategy"] = strategy
    result["then_by"] = then_by

    if strategy == "manual":
        result["errors"].append("Bucket uses manual sorting - no auto-sort applied. Specify sort_by to override.")
        return result

    # Fetch all tasks in bucket
    tasks_raw = _get_bucket_tasks_raw(project_id, view_id, bucket_id)
    if not tasks_raw:
        return result

    # Sort tasks by strategy (with optional secondary sort)
    sorted_tasks = sorted(tasks_raw, key=lambda t: _get_task_sort_key(t, strategy, then_by))

    # Assign new positions with gaps (1000, 2000, 3000...)
    positions = []
    for i, task in enumerate(sorted_tasks):
        position = (i + 1) * 1000.0
        positions.append({"task_id": task["id"], "position": position})

    # Apply positions in batch
    batch_result = _batch_set_positions_impl(view_id, positions)
    result["sorted"] = batch_result["updated"]
    result["tasks"] = batch_result["tasks"]
    result["errors"].extend(batch_result["errors"])

    return result


@mcp.tool()
@mcp_tool_with_fallback
def kanban_sort_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the kanban view (get from get_kanban_view)"),
    bucket_id: int = Field(description="ID of the bucket to sort (get from list_buckets)"),
    sort_by: str = Field(default=None, description="Primary sort field: due_date, start_date, end_date, priority, title, created, position. Overrides config if specified."),
    then_by: str = Field(default=None, description="Secondary sort for ties. E.g., sort_by=due_date, then_by=title for alphabetical within same date.")
) -> dict:
    """
    Re-sort all tasks in a bucket with optional two-level sorting.

    Supports two-level sorting for stable ordering:
    - sort_by=due_date, then_by=title → alphabetical within each date
    - sort_by=due_date, then_by=priority → urgent first within each date
    - sort_by=priority, then_by=due_date → urgent first, then by deadline

    If sort_by not specified, uses project config. If config strategy is 'manual',
    no sorting is applied unless sort_by is explicitly provided.

    Returns: {sorted: int, tasks: [{task_id, position}], strategy: str, then_by: str, errors: []}
    """
    return _sort_bucket_impl(project_id, view_id, bucket_id, sort_by, then_by)


def _move_task_to_project_impl(task_id: int, target_project_id: int) -> dict:
    """
    Move a task from its current project to a different project.

    Updates the task's project_id field.
    """
    # GET current task
    current = _request("GET", f"/api/v1/tasks/{task_id}")
    old_project_id = current.get("project_id")

    # Update project_id
    current["project_id"] = target_project_id

    # POST updated task
    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)

    return {
        "task_id": task_id,
        "title": response.get("title", ""),
        "old_project_id": old_project_id,
        "new_project_id": target_project_id,
        "moved": True
    }


def _move_task_to_project_by_name_impl(task_id: int, project_name: str) -> dict:
    """
    Move a task to a project by name (fuzzy match).

    Looks up the project by name, then moves the task.
    Returns error if no match or ambiguous match.
    """
    matches = _find_projects_by_name(project_name)

    if not matches:
        return {"error": f"No project found matching '{project_name}'"}

    if len(matches) > 1:
        # Multiple matches - return options for user to clarify
        options = [f"{m['instance']}: {m['name']} (ID {m['project_id']})" for m in matches[:5]]
        return {
            "error": "ambiguous_project",
            "message": f"Multiple projects match '{project_name}'",
            "options": options,
            "hint": "Please specify more precisely or use task_move with the exact target_project_id"
        }

    # Single match - proceed with move
    target = matches[0]
    result = _move_task_to_project_impl(task_id, target["project_id"])
    result["target_project_name"] = target["name"]
    result["target_instance"] = target["instance"]
    return result


@mcp.tool()
@mcp_tool_with_fallback
def task_move(
    task_id: int = Field(description="ID of the task to move"),
    target_project_id: int = Field(default=0, description="ID of the target project (use this OR project_name)"),
    project_name: str = Field(default="", description="Name of the target project — fuzzy matched (use this OR target_project_id)"),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = current instance.")
) -> dict:
    """
    Move a task to a different project. Provide EITHER target_project_id OR project_name.

    By name (preferred): task_move(task_id=123, project_name="kitchen")
    By ID: task_move(task_id=123, target_project_id=456)

    Uses fuzzy matching for names. If ambiguous, returns options for clarification.
    """
    if instance:
        _tok = _forced_instance.set(instance)
        try:
            if project_name:
                return _move_task_to_project_by_name_impl(task_id, project_name)
            if target_project_id:
                return _move_task_to_project_impl(task_id, target_project_id)
            return {"error": "Provide either target_project_id or project_name"}
        finally:
            _forced_instance.reset(_tok)
    if project_name:
        return _move_task_to_project_by_name_impl(task_id, project_name)
    if target_project_id:
        return _move_task_to_project_impl(task_id, target_project_id)
    return {"error": "Provide either target_project_id or project_name"}


def _complete_tasks_by_label_impl(project_id: int, label_filter: str) -> dict:
    """Complete all tasks matching a label filter."""
    tasks = _list_tasks_impl(project_id, include_completed=False, label_filter=label_filter)
    result = {"completed": 0, "tasks": [], "errors": []}

    for task in tasks:
        try:
            _complete_task_impl(task["id"])
            result["completed"] += 1
            result["tasks"].append({"id": task["id"], "title": task["title"]})
        except Exception as e:
            result["errors"].append(f"Failed to complete task {task['id']}: {str(e)}")

    return result


def _move_tasks_by_label_impl(project_id: int, label_filter: str, view_id: int, bucket_id: int) -> dict:
    """Move all tasks matching a label filter to a bucket."""
    tasks = _list_tasks_impl(project_id, include_completed=False, label_filter=label_filter)
    result = {"moved": 0, "tasks": [], "errors": []}

    for task in tasks:
        try:
            _set_task_position_impl(task["id"], project_id, view_id, bucket_id)
            result["moved"] += 1
            result["tasks"].append({"id": task["id"], "title": task["title"]})
        except Exception as e:
            result["errors"].append(f"Failed to move task {task['id']}: {str(e)}")

    return result


@mcp.tool()
@mcp_tool_with_fallback
def batch_complete_by_label(
    project_id: int = Field(description="ID of the project"),
    label_filter: str = Field(description="Label name to match (case-insensitive partial match)")
) -> dict:
    """
    Complete all tasks matching a label.

    Marks all incomplete tasks with the matching label as done.
    Use after an event to sweep tasks: batch_complete_by_label(pid, "Sunday Party")

    Returns: {completed: int, tasks: [{id, title}], errors: []}
    """
    return _complete_tasks_by_label_impl(project_id, label_filter)


@mcp.tool()
@mcp_tool_with_fallback
def batch_move_by_label(
    project_id: int = Field(description="ID of the project"),
    label_filter: str = Field(description="Label name to match (case-insensitive partial match)"),
    view_id: int = Field(description="ID of the kanban view"),
    bucket_id: int = Field(description="ID of the target bucket")
) -> dict:
    """
    Move all tasks matching a label to a bucket.

    Moves all incomplete tasks with the matching label to the specified kanban bucket.
    Use for workflow transitions: batch_move_by_label(pid, "Sourdough", vid, done_bucket_id)

    Returns: {moved: int, tasks: [{id, title}], errors: []}
    """
    return _move_tasks_by_label_impl(project_id, label_filter, view_id, bucket_id)


def _get_project_config_impl(project_id: int) -> dict:
    """Get configuration for a project."""
    config = _load_config()
    project_config = config["projects"].get(str(project_id))
    return {"project_id": project_id, "config": project_config}


def _set_project_config_impl(project_id: int, project_config: dict) -> dict:
    """Set configuration for a project (replaces existing)."""
    config = _load_config()
    created = str(project_id) not in config["projects"]
    config["projects"][str(project_id)] = project_config
    _save_config(config)
    return {"project_id": project_id, "config": project_config, "created": created}


def _update_project_config_impl(project_id: int, updates: dict) -> dict:
    """Partially update configuration for a project (deep merge)."""
    config = _load_config()
    existing = config["projects"].get(str(project_id), {})
    merged = _deep_merge(existing, updates)
    config["projects"][str(project_id)] = merged
    _save_config(config)
    return {"project_id": project_id, "config": merged}


def _delete_project_config_impl(project_id: int) -> dict:
    """Delete configuration for a project."""
    config = _load_config()
    deleted = str(project_id) in config["projects"]
    if deleted:
        del config["projects"][str(project_id)]
        _save_config(config)
    return {"project_id": project_id, "deleted": deleted}


def _list_project_configs_impl() -> dict:
    """List all configured projects."""
    config = _load_config()
    projects = []
    for pid, pconfig in config["projects"].items():
        projects.append({
            "project_id": int(pid),
            "name": pconfig.get("name", f"Project {pid}")
        })
    return {"projects": projects}


def _get_project_ears(project_id: int) -> tuple[bool, str | None]:
    """Check if ears mode (!ears on) is enabled for a project.

    Returns:
        Tuple of (enabled, ears_since_timestamp)
    """
    config = _load_config()
    project_config = config.get("projects", {}).get(str(project_id), {})
    return (
        project_config.get("capture_enabled", False),  # DB field kept for compatibility
        project_config.get("capture_since")  # DB field kept for compatibility
    )


def _update_project_ears(project_id: int, enabled: bool):
    """Enable/disable ears mode (!ears on/off) for a project.

    When enabled, records the current timestamp so only tasks created
    after this point are processed.

    Args:
        project_id: Project ID
        enabled: True to enable, False to disable
    """
    config = _load_config()
    if "projects" not in config:
        config["projects"] = {}
    if str(project_id) not in config["projects"]:
        config["projects"][str(project_id)] = {}

    config["projects"][str(project_id)]["capture_enabled"] = enabled  # DB field kept for compatibility

    if enabled:
        # Record when ears mode started (only process tasks after this)
        from datetime import datetime, timezone
        config["projects"][str(project_id)]["capture_since"] = datetime.now(timezone.utc).isoformat()
    else:
        # Clear timestamp when disabled
        config["projects"][str(project_id)].pop("capture_since", None)

    _save_config(config)
    logger.info(f"[EARS] Project #{project_id} ears mode: {'ON' if enabled else 'OFF'}")


def _get_ears_enabled_projects() -> list[tuple[int, str]]:
    """Get all projects with ears mode (!ears on) enabled.

    Returns:
        List of (project_id, ears_since_timestamp) tuples
    """
    config = _load_config()
    enabled = []
    for pid, pconfig in config.get("projects", {}).items():
        if pconfig.get("capture_enabled") and pconfig.get("capture_since"):
            enabled.append((int(pid), pconfig["capture_since"]))
    return enabled


def _create_from_template_impl(
    project_id: int,
    template: str,
    anchor_time: str,
    labels: list[str] = None,
    title_suffix: str = "",
    bucket: str = None
) -> dict:
    """Create tasks from a project template with a target anchor time."""
    config = _load_config()
    project_config = config["projects"].get(str(project_id))
    if not project_config:
        raise ValueError(f"No config found for project {project_id}")

    templates = project_config.get("templates", {})
    if template not in templates:
        available = list(templates.keys()) if templates else "none"
        raise ValueError(f"Template '{template}' not found. Available: {available}")

    tmpl = templates[template]
    anchor_dt = datetime.fromisoformat(anchor_time.replace("Z", "+00:00"))

    # Build task list with calculated times
    tasks = []
    template_labels = tmpl.get("default_labels", [])
    all_labels = template_labels + (labels or [])

    for task_def in tmpl.get("tasks", []):
        offset_hours = task_def.get("offset_hours", 0)
        duration_hours = task_def.get("duration_hours", 1)

        start_dt = anchor_dt + timedelta(hours=offset_hours)
        end_dt = start_dt + timedelta(hours=duration_hours)

        # Format for Gantt visibility (full day spans)
        start_date = start_dt.strftime("%Y-%m-%dT00:00:00Z")
        end_date = start_dt.strftime("%Y-%m-%dT23:59:00Z")

        title = task_def["title"]
        if title_suffix:
            title = f"{title} {title_suffix}"

        task = {
            "title": title,
            "start_date": start_date,
            "end_date": end_date,
            "labels": all_labels.copy(),
        }

        if task_def.get("ref"):
            task["ref"] = task_def["ref"]
        if task_def.get("blocked_by"):
            task["blocked_by"] = task_def["blocked_by"]
        if bucket:
            task["bucket"] = bucket

        tasks.append(task)

    # Use batch_create_tasks to create all tasks
    result = _batch_create_tasks_impl(
        project_id=project_id,
        tasks=tasks,
        create_missing_labels=True,
        create_missing_buckets=False
    )

    return result


@mcp.tool()
@mcp_tool_with_fallback
def config_get(
    project_id: int = Field(description="ID of the Vikunja project")
) -> dict:
    """
    Get configuration for a project.

    Returns project-specific settings: sort strategy, default labels/bucket, templates, llm_instructions.
    Returns {"project_id": X, "config": null} if no config exists.
    """
    return _get_project_config_impl(project_id)


@mcp.tool()
@mcp_tool_with_fallback
def config_set(
    project_id: int = Field(description="ID of the Vikunja project"),
    config: dict = Field(description="Configuration object: {name, sort_strategy, default_labels, default_bucket, templates, llm_instructions}")
) -> dict:
    """
    Set configuration for a project (replaces existing).

    Config schema:
    - name: Human-readable project name
    - sort_strategy: {default: "manual"|"start_date"|..., buckets: {"Bucket": "strategy"}}
    - default_labels: Labels to auto-apply to new tasks
    - default_bucket: Default bucket for new tasks
    - templates: {name: {description, anchor, default_labels, tasks: [...]}}
    - llm_instructions: Instructions for LLM when working in this project (e.g., "In recipes, use grams instead of cups")

    Returns: {project_id, config, created: bool}
    """
    return _set_project_config_impl(project_id, config)


@mcp.tool()
@mcp_tool_with_fallback
def config_update(
    project_id: int = Field(description="ID of the Vikunja project"),
    updates: dict = Field(description="Fields to update (deep merged with existing)")
) -> dict:
    """
    Partially update configuration for a project.

    Deep merges updates with existing config. Use this to add a template
    or change a sort strategy without replacing the entire config.

    Example: {"sort_strategy": {"buckets": {"New Bucket": "start_date"}}}
    """
    return _update_project_config_impl(project_id, updates)


@mcp.tool()
@mcp_tool_with_fallback
def config_delete(
    project_id: int = Field(description="ID of the Vikunja project")
) -> dict:
    """
    Delete configuration for a project.

    Returns: {project_id, deleted: bool}
    """
    return _delete_project_config_impl(project_id)


@mcp.tool()
@mcp_tool_with_fallback
def config_list() -> dict:
    """
    List all configured projects.

    Returns: {projects: [{project_id, name}, ...]}
    """
    return _list_project_configs_impl()


@mcp.tool()
@mcp_tool_with_fallback
def project_create_from_template(
    project_id: int = Field(description="ID of the project to create tasks in"),
    template: str = Field(description="Template name (e.g., 'sourdough')"),
    anchor_time: str = Field(description="ISO datetime for the anchor task (e.g., '2025-12-21T09:00:00Z')"),
    labels: list[str] = Field(default=[], description="Additional labels beyond template defaults"),
    title_suffix: str = Field(default="", description="Append to task titles (e.g., '(Sun party)')"),
    bucket: str = Field(default="", description="Override default bucket placement")
) -> dict:
    """
    Create tasks from a project template with a target anchor time.

    Templates define task sequences with relative timing (offset_hours from anchor).
    The anchor task is the reference point (e.g., "bake" at T+0).

    Example: project_create_from_template(pid, "sourdough", "2025-12-21T09:00:00Z", labels=["🌟 Sunday Party"])
    → Creates 6 tasks with times calculated backward from 9am bake time

    Returns: {created: int, tasks: [{ref, id, title, start_date}], relations_created: int}
    """
    return _create_from_template_impl(
        project_id, template, anchor_time,
        labels if labels else None,
        title_suffix,
        bucket if bucket else None
    )


@mcp.tool()
@mcp_tool_with_fallback
def instance_list() -> dict:
    """
    List all configured Vikunja instances.

    Returns: {instances: [{name, url, is_current}, ...], current: str}
    """
    instances = _get_instances()

    # Get current instance - use user context if available
    user_id = _current_user_id.get()
    if user_id:
        current = _get_user_instance(user_id) or "default"
    else:
        current = _get_current_instance()

    return {
        "instances": [
            {
                "name": name,
                "url": inst.get("url"),
                "is_current": name == current
            }
            for name, inst in instances.items()
        ],
        "current": current
    }


@mcp.tool()
@mcp_tool_with_fallback
def ctx_get() -> dict:
    """
    Get the full current context: active instance, URL, project scope, and available instances.

    Use this to check what's active before running queries, or to inform the user.

    Returns: {instance, url, project_id, available_instances}
    """
    config = _load_config()
    mcp_context = config.get("mcp_context", {})
    instances = _get_instances()

    # Get current connection info
    try:
        instance, url, _ = _get_effective_instance_config()
    except ValueError:
        url_env = os.environ.get("VIKUNJA_URL")
        instance = "default (env)" if url_env else None
        url = url_env.rstrip('/') if url_env else None

    return {
        "instance": instance,
        "url": url,
        "project_id": mcp_context.get("project_id"),
        "available_instances": list(instances.keys()),
        "hint": "Use set_active_context to change defaults, or pass instance= to individual tools."
    }


@mcp.tool()
@mcp_tool_with_fallback
def instance_switch(
    name: str = Field(description="Name of the instance to switch to")
) -> dict:
    """
    Switch to a different Vikunja instance.

    All subsequent operations will use the selected instance's URL and token.

    Returns: {switched_to: str, url: str}
    """
    # Use user context if available, otherwise use YAML config
    user_id = _current_user_id.get()
    if user_id:
        result = _set_user_instance(user_id, name)
        if "error" in result:
            return result
        url = result.get("url", "")
    else:
        _set_current_instance(name)
        url, _ = _get_instance_config(name)

    return {
        "switched_to": name,
        "url": url
    }


@mcp.tool()
@mcp_tool_with_fallback
def instance_check_health(
    instance: str = Field(default="", description="Instance name to check (empty = current instance)")
) -> dict:
    """
    Check if the current Vikunja API token is valid and not expired.

    Returns token status, expiration info, and warnings if the token is about to expire.
    Useful for diagnosing authentication issues.

    Returns: {
        instance: str,
        url: str,
        token_valid: bool,
        token_type: str,  # "jwt" or "api_token"
        expires_at: str | null,  # ISO timestamp for JWT tokens
        days_until_expiry: int | null,
        warning: str | null,  # Warning message if expiring soon
        error: str | null  # Error message if invalid
    }
    """
    from datetime import datetime, timezone
    import base64
    import json

    # Get instance config - use user context if available
    try:
        if instance:
            # Specific instance requested - use YAML config
            instance_name = instance
            url, token = _get_instance_config(instance_name)
        else:
            # No instance specified - use effective config (user context or YAML)
            instance_name, url, token = _get_effective_instance_config()
    except Exception as e:
        return {
            "instance": instance or "unknown",
            "error": f"Failed to get instance config: {e}",
            "token_valid": False
        }

    if not instance_name:
        return {
            "error": "No instance selected. Use instance_switch() first.",
            "token_valid": False
        }
    
    if not token:
        return {
            "instance": instance_name,
            "url": url,
            "error": "No token configured for this instance",
            "token_valid": False
        }
    
    result = {
        "instance": instance_name,
        "url": url,
        "token_type": "jwt" if token.startswith("eyJ") else "api_token",
        "expires_at": None,
        "days_until_expiry": None,
        "warning": None,
        "error": None
    }
    
    # Check if JWT token and extract expiration
    if result["token_type"] == "jwt":
        try:
            # Decode JWT payload (second part)
            parts = token.split('.')
            if len(parts) >= 2:
                # Add padding if needed
                payload = parts[1]
                padding = 4 - len(payload) % 4
                if padding != 4:
                    payload += '=' * padding

                decoded = base64.b64decode(payload)
                payload_data = json.loads(decoded)

                if 'exp' in payload_data:
                    exp_timestamp = payload_data['exp']
                    exp_datetime = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
                    result["expires_at"] = exp_datetime.isoformat()

                    now = datetime.now(timezone.utc)
                    days_left = (exp_datetime - now).days
                    result["days_until_expiry"] = days_left

                    if days_left < 0:
                        result["error"] = f"Token expired {abs(days_left)} days ago on {exp_datetime.strftime('%Y-%m-%d')}"
                        result["token_valid"] = False
                        return result
                    elif days_left <= 7:
                        result["warning"] = f"⚠️ Token expires in {days_left} days on {exp_datetime.strftime('%Y-%m-%d')}. Generate a new token soon!"
                    elif days_left <= 30:
                        result["warning"] = f"Token expires in {days_left} days on {exp_datetime.strftime('%Y-%m-%d')}"
        except Exception as e:
            result["warning"] = f"Could not parse JWT expiration: {e}"
    else:
        # API token - check config for expiration date
        token_expires = _get_instance_token_expires(instance_name)
        if token_expires:
            try:
                exp_date = datetime.strptime(token_expires, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                result["expires_at"] = exp_date.isoformat()

                now = datetime.now(timezone.utc)
                days_left = (exp_date - now).days
                result["days_until_expiry"] = days_left

                if days_left < 0:
                    result["warning"] = f"⚠️ Token may have expired {abs(days_left)} days ago on {token_expires}. Generate a new token!"
                elif days_left <= 7:
                    result["warning"] = f"⚠️ Token expires in {days_left} days on {token_expires}. Generate a new token soon!"
                elif days_left <= 30:
                    result["warning"] = f"Token expires in {days_left} days on {token_expires}"
            except ValueError:
                pass  # Invalid date format, ignore
        else:
            result["note"] = "No expiration date tracked. Use connect_instance with token_expires to enable expiry warnings."

    # Test the token by making an API call
    # Use /api/v1/projects instead of /api/v1/user because API tokens may not have user permission
    try:
        # _request() returns parsed JSON dict on success, raises ValueError on error
        _request("GET", "/api/v1/projects", allow_instance_fallback=True)
        result["token_valid"] = True
    except ValueError as e:
        # _request() raises ValueError for 401, 404, 4xx errors
        result["token_valid"] = False
        if "401" in str(e) or "Authentication failed" in str(e):
            result["error"] = "Token is invalid or has been revoked"
        else:
            result["error"] = f"API error: {e}"
    except Exception as e:
        result["token_valid"] = False
        result["error"] = f"Failed to test token: {e}"
    
    return result


@mcp_tool_with_fallback
def instance_connect(
    name: str = Field(description="Name for the instance (e.g., 'cloud', 'factumerit')"),
    url: str = Field(description="Base URL of the Vikunja instance"),
    token: str = Field(description="API token (or env var reference like '${VIKUNJA_CLOUD_TOKEN}')"),
    token_expires: str = Field(default="", description="Optional: Token expiration date (YYYY-MM-DD) for tracking"),
    timezone: str = Field(default="", description="Optional: Timezone for date conversion (e.g., 'America/Los_Angeles')")
) -> dict:
    """
    [ADMIN] Connect to a Vikunja instance.

    Adds the instance to your local MCP config. Does not modify the Vikunja server.
    Auto-switches to this instance if it's your first connection.
    Requires admin privileges on current instance (if one exists).

    Optional fields:
    - token_expires: Track when the API token expires (for health check warnings)
    - timezone: Convert naive datetimes to UTC using this timezone

    Returns: {name, url, connected: true, switched_to?: str, hint?: str}
    """
    # Admin check - use current instance
    current = _get_current_instance()
    if current:
        admin_error = _require_admin(current)
        if admin_error:
            return admin_error

    return _connect_instance(name, url, token, token_expires, timezone)


@mcp.tool()
@mcp_tool_with_fallback
def instance_disconnect(
    name: str = Field(description="Name of the instance to disconnect")
) -> dict:
    """
    [ADMIN] Disconnect from a Vikunja instance.

    Removes the instance from your local MCP config only.
    Does NOT delete any data on the Vikunja server - you can reconnect anytime.
    Requires admin privileges on current instance.

    Returns: {name, disconnected: true}
    """
    # Admin check - use current instance
    current = _get_current_instance()
    if current:
        admin_error = _require_admin(current)
        if admin_error:
            return admin_error

    # Prevent disconnecting the current instance
    if name == current:
        return {"error": "Cannot disconnect the currently active instance. Switch first."}

    return _disconnect_instance(name)


@mcp.tool()
@mcp_tool_with_fallback
def instance_rename(
    old_name: str = Field(description="Current name of the instance"),
    new_name: str = Field(description="New name for the instance")
) -> dict:
    """
    [ADMIN] Rename a Vikunja instance.

    Updates all references in config (current_instance, xq, projects).
    Requires admin privileges on current instance.

    Returns: {old_name, new_name, renamed: true}
    """
    # Admin check - use current instance
    current = _get_current_instance()
    if current:
        admin_error = _require_admin(current)
        if admin_error:
            return admin_error

    return _rename_instance(old_name, new_name)


def _request_for_instance(instance_name: str, method: str, endpoint: str, **kwargs) -> dict:
    """Make request to a specific instance (for parallel fetching)."""
    url, token = _get_instance_config(instance_name)
    full_url = f"{url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    response = requests.request(method, full_url, headers=headers, **kwargs)
    if response.status_code >= 400:
        raise ValueError(f"API error ({response.status_code}): {response.text}")
    if method != "DELETE":
        return response.json()
    return {}


def _fetch_from_all_instances(method: str, endpoint: str, **kwargs) -> dict[str, any]:
    """Fetch from all configured instances in parallel.

    Returns: {instance_name: response_data, ...}
    Errors are captured as {instance_name: {"error": str}}
    """
    instances = _get_instances()
    results = {}

    if not instances:
        return results  # No instances configured

    with ThreadPoolExecutor(max_workers=len(instances)) as executor:
        futures = {
            executor.submit(_request_for_instance, name, method, endpoint, **kwargs): name
            for name in instances.keys()
        }
        for future in as_completed(futures):
            instance_name = futures[future]
            try:
                results[instance_name] = future.result()
            except Exception as e:
                results[instance_name] = {"error": str(e)}

    return results


def _fetch_all_pages_for_instance(
    instance_name: str,
    method: str,
    endpoint: str,
    per_page: int = 50,
    max_pages: int = 100,
    params: dict = None
) -> list:
    """Fetch all pages from a specific instance's paginated endpoint.

    Similar to _fetch_all_pages but for multi-instance context.
    """
    all_items = []
    params = dict(params) if params else {}
    params["per_page"] = per_page

    for page in range(1, max_pages + 1):
        params["page"] = page
        try:
            data = _request_for_instance(instance_name, method, endpoint, params=params)
        except Exception:
            break

        if not isinstance(data, list):
            break

        all_items.extend(data)

        if len(data) < per_page:
            break

    return all_items


def _fetch_all_pages_from_all_instances(
    method: str,
    endpoint: str,
    per_page: int = 50,
    max_pages: int = 100,
    params: dict = None
) -> dict[str, list]:
    """Fetch all pages from all instances in parallel.

    Each instance is fetched with full pagination (all pages).
    Returns: {instance_name: [all_items], ...}
    """
    instances = _get_instances()
    results = {}

    if not instances:
        return results  # No instances configured

    with ThreadPoolExecutor(max_workers=len(instances)) as executor:
        futures = {
            executor.submit(
                _fetch_all_pages_for_instance,
                name, method, endpoint, per_page, max_pages, params
            ): name
            for name in instances.keys()
        }
        for future in as_completed(futures):
            instance_name = futures[future]
            try:
                results[instance_name] = future.result()
            except Exception as e:
                results[instance_name] = {"error": str(e)}

    return results


@mcp.tool()
@mcp_tool_with_fallback
def project_list_all() -> dict:
    """
    List projects from ALL configured Vikunja instances.

    Returns: {projects: [{id, title, instance, ...}, ...], by_instance: {name: count}}
    """
    # Fetch all pages from all instances
    results = _fetch_all_pages_from_all_instances(
        "GET", "/api/v1/projects",
        per_page=50,
        max_pages=20  # Projects usually fewer than tasks
    )

    all_projects = []
    by_instance = {}

    for instance_name, data in results.items():
        if isinstance(data, dict) and "error" in data:
            by_instance[instance_name] = f"error: {data['error']}"
            continue

        by_instance[instance_name] = len(data)
        for project in data:
            all_projects.append({
                "id": project.get("id"),
                "title": project.get("title"),
                "instance": instance_name,
                "description": project.get("description", "")[:100],
            })

    # Sort by title
    all_projects.sort(key=lambda p: p.get("title", "").lower())

    return {
        "projects": all_projects,
        "total": len(all_projects),
        "by_instance": by_instance
    }


def _get_cached_task_list(cache_key: str) -> Optional[dict]:
    """Get cached task list if not expired."""
    if cache_key in _task_list_cache:
        result, timestamp = _task_list_cache[cache_key]
        if time.time() - timestamp < _TASK_LIST_CACHE_TTL_SECONDS:
            return result
        del _task_list_cache[cache_key]
    return None


def _set_cached_task_list(cache_key: str, result: dict):
    """Cache task list result."""
    _task_list_cache[cache_key] = (result, time.time())


def _invalidate_task_list_cache():
    """Clear task list cache (called on task create/update/delete)."""
    global _task_list_cache
    _task_list_cache = {}


def _list_all_tasks_impl(
    filter_due: str = "",
    include_done: bool = False,
    filter: str = "",
    page: int = 0,
    allow_truncated: bool = False,
    due_after: str = "",
    due_before: str = "",
    instance: str = "",
    project_id: int = 0,
    include_meta: bool = False
) -> dict:
    """Implementation for list_all_tasks - testable without decorator.

    include_meta: when True, each task additionally carries start_date, updated,
    bucket_id, and full labels (id+title+description) for the today-actions scorer
    (fa-gptz). Default False keeps the lean shape every existing caller — including
    the LLM-facing task_query tool — sees, so enriching costs no extra tokens
    unless a consumer opts in.
    """
    # Check cache first (30s TTL for repeated queries)
    cache_key = f"{filter_due}:{include_done}:{filter}:{page}:{due_after}:{due_before}:{instance}:{project_id}:{include_meta}"
    # fa-bglr.7: under a per-user calendar override the SAME instance name (commonly
    # "default") is shared across users, so an instance-keyed cache would let user B
    # read user A's cached tasks within the TTL. Namespace the key by the acting user's
    # identity (id, else a token fingerprint) to keep per-user reads isolated.
    if _per_user_override():
        ident = _current_user_id.get() or hashlib.sha256(
            (_current_vikunja_token.get() or "").encode()).hexdigest()[:16]
        cache_key = f"u={ident}|{cache_key}"
    cached = _get_cached_task_list(cache_key)
    if cached:
        cached["cached"] = True
        return cached

    per_page = 50  # Vikunja's actual page limit (ignores higher values)
    params = {}
    if filter:
        params["filter"] = filter

    # Choose endpoint based on whether we're querying a specific project
    # Using project-specific endpoint is more efficient and avoids delegation limits
    if project_id:
        endpoint = f"/api/v1/projects/{project_id}/tasks"
        logger.info(f"[list_all_tasks] Using project-specific endpoint: {endpoint}")
    else:
        endpoint = "/api/v1/tasks"
        logger.info(f"[list_all_tasks] Using all-tasks endpoint: {endpoint}")

    # Check if we have configured instances
    instances = _get_instances()

    # Determine instance name for single-instance mode
    # Use the requested instance name, or "default" if not specified
    single_instance_name = instance if instance else "default"

    # fa-bglr.7: a per-user calendar request pins the requesting user's own creds via
    # the override contextvars. It is single-instance by nature, so we must NOT fan out
    # across the OWNER's configured instances — that would fetch the user's tasks once
    # per owner-instance, key them by owner-instance names, then drop them all in the
    # post-filter below (user instance ∉ owner names), AND the fan-out runs in worker
    # threads that don't inherit the override. Force the single-instance path, keyed by
    # the requested (user's) instance name so the post-filter at L~8136 still matches.
    per_user = _per_user_override()

    # Fetch all pages unless specific page requested
    if page > 0:
        params["page"] = page
        params["per_page"] = per_page
        if instances and not per_user:
            results = _fetch_from_all_instances("GET", endpoint, params=params)
        else:
            # No configured instances (or per-user override) - use user token directly
            data = _request("GET", endpoint, params=params)
            results = {single_instance_name: data}
    else:
        # Fetch ALL pages
        if instances and not per_user:
            # Multi-instance: fetch from all configured instances
            results = _fetch_all_pages_from_all_instances(
                "GET", endpoint,
                per_page=per_page,
                max_pages=100,
                params=params
            )
        else:
            # Single instance (or per-user override): use user token directly
            data = _fetch_all_pages("GET", endpoint, per_page=per_page, max_pages=100, params=params)
            results = {single_instance_name: data}

    all_tasks = []
    by_instance = {}
    hit_limit = False
    high_priority_no_date = 0  # Track tasks with priority >= 3 but no due date
    now = datetime.now(timezone.utc)  # Use UTC for consistent comparison with Vikunja timestamps
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = today_start + timedelta(days=7)

    total_fetched = sum(len(d) if isinstance(d, list) else 0 for d in results.values())
    logger.info(f"[list_all_tasks] QUERY: filter_due={filter_due}, project_id={project_id}, include_done={include_done}")
    logger.info(f"[list_all_tasks] FETCHED: {total_fetched} total tasks from API")
    logger.info(f"[list_all_tasks] TIME: now={now}, today_start={today_start}, today_end={today_start.replace(hour=23, minute=59, second=59)}")

    # Debug: Log raw results for project-specific queries
    if project_id and total_fetched == 0:
        logger.warning(f"[list_all_tasks] No tasks returned for project {project_id}!")
        logger.warning(f"[list_all_tasks] Raw results: {results}")
        logger.warning(f"[list_all_tasks] Endpoint used: {endpoint}")

    for instance_name, data in results.items():
        # Filter by instance if specified
        if instance and instance_name != instance:
            continue

        if isinstance(data, dict) and "error" in data:
            by_instance[instance_name] = f"error: {data['error']}"
            continue

        # Check if we hit the per_page limit
        if len(data) >= per_page:
            hit_limit = True

        instance_count = 0
        for task in data:
            # Skip done tasks unless requested
            if not include_done and task.get("done"):
                logger.debug(f"[list_all_tasks] Skipping done task #{task.get('id')}")
                continue

            # Filter by project if specified
            if project_id and task.get("project_id") != project_id:
                logger.debug(f"[list_all_tasks] Skipping task #{task.get('id')} from project {task.get('project_id')} (want {project_id})")
                continue

            due_date_str = task.get("due_date")
            due_date = None
            if due_date_str and due_date_str != "0001-01-01T00:00:00Z":
                try:
                    due_date = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
                except:
                    pass

            # Count high-priority tasks (>= 3) without due dates for hint
            if not due_date and task.get("priority", 0) >= 3:
                high_priority_no_date += 1

            # Apply due date filter
            # Date filters:
            # - "today" / "due_today" = overdue + due today (actionable items)
            # - "week" / "due_this_week" = overdue + due this week
            # - "overdue" = strictly past due
            # - "no_due_date" = tasks without due date
            # Priority filters:
            # - "priority_5" = priority >= 5
            # - "priority_3_plus" = priority >= 3
            # - "focus" = priority >= 3 AND (overdue OR due today)
            if filter_due in ("today", "due_today"):
                # Include tasks due in the past 24 hours OR future today
                # This handles timezone differences (user may be in PST while server is UTC)
                yesterday_start = today_start - timedelta(days=1)
                today_end = today_start.replace(hour=23, minute=59, second=59)
                if not due_date or due_date < yesterday_start or due_date > today_end:
                    logger.debug(f"[filter_due=today] Skipping task #{task.get('id')}: due_date={due_date}, range={yesterday_start} to {today_end}")
                    continue
                else:
                    logger.debug(f"[filter_due=today] Including task #{task.get('id')}: due_date={due_date}")
            elif filter_due in ("week", "due_this_week"):
                if not due_date or due_date > week_end:
                    continue
            elif filter_due == "overdue":
                if not due_date or due_date >= now:
                    continue
            elif filter_due == "no_due_date":
                # Only tasks WITHOUT a due date
                if due_date:
                    continue
            elif filter_due == "priority_5":
                # Priority 5 only (urgent)
                if task.get("priority", 0) < 5:
                    continue
            elif filter_due == "priority_3_plus":
                # Priority 3 or higher
                if task.get("priority", 0) < 3:
                    continue
            elif filter_due == "focus":
                # Focus mode: high priority (3+) AND (overdue OR due today)
                if task.get("priority", 0) < 3:
                    continue
                today_end = today_start.replace(hour=23, minute=59, second=59)
                if not due_date or due_date > today_end:
                    continue

            # Apply due_after filter (ISO date string like "2025-12-19")
            if due_after:
                if not due_date:
                    continue  # No due date = excluded
                try:
                    after_date = datetime.fromisoformat(due_after.replace("Z", "+00:00"))
                    if len(due_after) == 10:  # Just date, no time
                        after_date = after_date.replace(tzinfo=None)
                        due_date_naive = due_date.replace(tzinfo=None) if due_date.tzinfo else due_date
                        if due_date_naive < after_date:
                            continue
                    elif due_date < after_date:
                        continue
                except ValueError:
                    pass  # Invalid date format, skip filter

            # Apply due_before filter (ISO date string like "2025-12-25")
            if due_before:
                if not due_date:
                    continue  # No due date = excluded
                try:
                    before_date = datetime.fromisoformat(due_before.replace("Z", "+00:00"))
                    if len(due_before) == 10:  # Just date, no time
                        before_date = before_date.replace(tzinfo=None)
                        due_date_naive = due_date.replace(tzinfo=None) if due_date.tzinfo else due_date
                        if due_date_naive >= before_date:
                            continue
                    elif due_date >= before_date:
                        continue
                except ValueError:
                    pass  # Invalid date format, skip filter

            instance_count += 1
            row = {
                "id": task.get("id"),
                "title": task.get("title"),
                "instance": instance_name,
                "project_id": task.get("project_id"),
                "due_date": due_date_str if due_date else None,
                "priority": task.get("priority", 0),
                "done": task.get("done", False),
            }
            if include_meta:
                # Extra fields powering the today-actions scorer (fa-gptz), opt-in
                # so the default (and the LLM-facing task_query) stays lean.
                # start_date drives the timeblock / snooze signals, `updated` drives
                # staleness, and full label objects (id/title/description) feed the
                # fa-3lri _label_metadata reader.
                row["start_date"] = task.get("start_date")
                # end_date is what makes a task an EVENT (today/08 §4.2, _task_kind);
                # without it every passed event reads as an overdue deed (auggie #3 HIGH).
                row["end_date"] = task.get("end_date")
                row["updated"] = task.get("updated") or task.get("updated_at")
                row["bucket_id"] = task.get("bucket_id", 0)
                row["labels"] = [
                    {"id": l.get("id"), "title": l.get("title"), "description": l.get("description", "")}
                    for l in (task.get("labels") or [])
                ]
                # Parsed deferral state (fa-fhtl) powers the escalation term + the
                # deferred-exclusion; project the small blob, not the full description.
                defer = _extract_defer_meta(task.get("description"))
                if defer:
                    row["defer"] = defer
            all_tasks.append(row)

        by_instance[instance_name] = instance_count

    # Sort by due date (nulls last), then priority
    def sort_key(t):
        due = t.get("due_date") or "9999-12-31"
        priority = 5 - t.get("priority", 0)  # Higher priority first
        return (due, priority)

    all_tasks.sort(key=sort_key)

    # Check if we hit the limit - but allow if using date filter (natural narrowing)
    if hit_limit and not allow_truncated and page == 0 and not filter_due:
        return {
            "error": "too_many_results",
            "count": len(all_tasks),
            "message": f"Found {len(all_tasks)}+ tasks (limit: {per_page}/instance). Please narrow your query:",
            "options": [
                "Add date filter: search_all_tasks(filter_due='today')",
                "Add date filter: search_all_tasks(filter_due='week')",
                "Add filter: search_all_tasks(filter='priority >= 3')",
                "Request page: search_all_tasks(page=1)",
                "Allow truncated: search_all_tasks(allow_truncated=true)"
            ],
            "by_instance": by_instance
        }

    result = {
        "tasks": all_tasks,
        "total": len(all_tasks),
        "filter_due": filter_due or "all",
        "filter": filter or None,
        "due_after": due_after or None,
        "due_before": due_before or None,
        "page": page if page > 0 else "all",
        "truncated": hit_limit,
        "by_instance": by_instance,
        "cached": False
    }

    # Add hint about high-priority tasks without due dates when using date filters
    if filter_due and high_priority_no_date > 0:
        result["high_priority_no_date"] = high_priority_no_date

    # Cache successful results (not errors)
    _set_cached_task_list(cache_key, result)

    return result


def _overdue_tasks_impl(instance: str = "", project_id: int = 0, include_meta: bool = False) -> dict:
    """Tasks past their due date, not completed."""
    return _list_all_tasks_impl(filter_due="overdue", instance=instance, project_id=project_id, include_meta=include_meta)


def _due_today_impl(instance: str = "", project_id: int = 0, include_meta: bool = False) -> dict:
    """Tasks due today or overdue. Focus for right now."""
    return _list_all_tasks_impl(filter_due="today", instance=instance, project_id=project_id, include_meta=include_meta)


def _due_this_week_impl(instance: str = "", project_id: int = 0) -> dict:
    """Tasks due in the next 7 days, including overdue."""
    return _list_all_tasks_impl(filter_due="week", instance=instance, project_id=project_id)


def _high_priority_tasks_impl(instance: str = "", project_id: int = 0, include_meta: bool = False) -> dict:
    """Open tasks with priority >= 3."""
    return _list_all_tasks_impl(filter="priority >= 3", instance=instance, project_id=project_id, allow_truncated=True, include_meta=include_meta)


def _urgent_tasks_impl(instance: str = "", project_id: int = 0) -> dict:
    """Open tasks with priority >= 4 (highest priority levels)."""
    return _list_all_tasks_impl(filter="priority >= 4", instance=instance, project_id=project_id, allow_truncated=True)


def _unscheduled_tasks_impl(instance: str = "", project_id: int = 0, include_meta: bool = False) -> dict:
    """Open tasks without a due date (often forgotten/floating)."""
    # Get all tasks, then filter client-side for no due date
    # Allow truncated since we're doing a focused query
    result = _list_all_tasks_impl(allow_truncated=True, instance=instance, project_id=project_id, include_meta=include_meta)
    if "error" in result:
        return result

    # Filter to only tasks without due date (None or sentinel)
    unscheduled = []
    for task in result.get("tasks", []):
        due = task.get("due_date")
        if not due or due == "0001-01-01T00:00:00Z":
            unscheduled.append(task)

    return {
        "tasks": unscheduled,
        "total": len(unscheduled),
        "by_instance": result.get("by_instance", {}),
        "filter": "unscheduled (no due date)"
    }


def _upcoming_deadlines_impl(days: int = 3, instance: str = "", project_id: int = 0) -> dict:
    """Tasks due in the next N days (default 3). Does NOT include overdue."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    end_date = (now + timedelta(days=days)).strftime("%Y-%m-%d")

    return _list_all_tasks_impl(due_after=today_str, due_before=end_date, instance=instance, project_id=project_id)


def _focus_now_impl(instance: str = "", project_id: int = 0, limit: int = 10) -> dict:
    """Tasks requiring immediate attention: priority >= 4 (high+) OR overdue."""
    from datetime import datetime, timezone

    # Get all open tasks (allow truncated for slash command use)
    result = _list_all_tasks_impl(instance=instance, project_id=project_id, allow_truncated=True)
    if "error" in result:
        return result

    now = datetime.now(timezone.utc)

    focus_tasks = []
    for task in result.get("tasks", []):
        # High priority (>= 4 = high, urgent, critical)?
        if task.get("priority", 0) >= 4:
            focus_tasks.append(task)
            continue

        # Overdue? (strictly past, not just due today)
        due_str = task.get("due_date")
        if due_str and due_str != "0001-01-01T00:00:00Z":
            try:
                due_date = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                if due_date < now:
                    focus_tasks.append(task)
            except:
                pass

    # Sort by priority (highest first), then due date
    def sort_key(t):
        priority = 5 - t.get("priority", 0)  # Higher priority first
        due = t.get("due_date") or "9999-12-31"
        return (priority, due)

    focus_tasks.sort(key=sort_key)

    total_matching = len(focus_tasks)

    # Apply limit (0 = no limit)
    if limit > 0 and len(focus_tasks) > limit:
        focus_tasks = focus_tasks[:limit]

    response = {
        "tasks": focus_tasks,
        "total": len(focus_tasks),
        "by_instance": result.get("by_instance", {}),
        "filter": "focus (priority >= 4 OR overdue)"
    }

    # Show total_matching if we truncated
    if total_matching > len(focus_tasks):
        response["total_matching"] = total_matching
        response["hint"] = f"Showing top {len(focus_tasks)} of {total_matching}. Use limit=0 for all, or task_query(query='urgent') for critical-only."

    return response


def _task_summary_impl(instance: str = "", project_id: int = 0) -> dict:
    """Lightweight task overview - counts only, no full task details.

    Returns counts for: overdue, due_today, due_this_week, high_priority,
    urgent, unscheduled, plus total and by_instance breakdown.
    """
    from datetime import datetime, timezone, timedelta

    # Fetch all tasks once
    result = _list_all_tasks_impl(allow_truncated=True, instance=instance, project_id=project_id)
    if "error" in result:
        return result

    tasks = result.get("tasks", [])
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    week_end = today_start + timedelta(days=7)

    # Initialize counters
    overdue = 0
    due_today = 0
    due_this_week = 0
    high_priority = 0  # priority >= 3
    urgent = 0  # priority >= 4
    critical = 0  # priority == 5
    unscheduled = 0

    for task in tasks:
        priority = task.get("priority", 0)
        due_str = task.get("due_date")

        # Priority counts
        if priority >= 3:
            high_priority += 1
        if priority >= 4:
            urgent += 1
        if priority == 5:
            critical += 1

        # Due date counts
        if not due_str or due_str == "0001-01-01T00:00:00Z":
            unscheduled += 1
        else:
            try:
                due_date = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                if due_date < today_start:
                    overdue += 1
                elif due_date <= today_end:
                    due_today += 1
                elif due_date <= week_end:
                    due_this_week += 1
            except:
                pass

    return {
        "total": len(tasks),
        "overdue": overdue,
        "due_today": due_today,
        "due_this_week": due_this_week,
        "high_priority": high_priority,
        "urgent": urgent,
        "critical": critical,
        "unscheduled": unscheduled,
        "by_instance": result.get("by_instance", {}),
        "note": "Counts only - use specific tools (overdue_tasks, due_today, etc.) for details"
    }


def _weight_overrides() -> dict:
    """The raw weight overrides for this actor: the user's `user_settings` row
    (fa-amnt.7), else the shared config's ``today_actions.weights``.

    Weights are stored per USER, not per account — one ranking preference per person,
    which is exactly the pre-migration semantics. The cross-account deck never compares
    scores between accounts (it interleaves round-robin, today/08 §6), so per-account
    weights would buy nothing here; they'd be a new feature, not a migration.

    Never raises: an unreadable store yields {} and the caller keeps pure defaults.
    """
    user_id, store = _settings_actor()
    if user_id:
        if store is None:
            return {}                         # defaults, not the owner's overrides
        try:
            value = store.get(user_id, store.ALL_ACCOUNTS, store.WEIGHTS)
            if value is None:                 # no row = never seeded (a successful read)
                value = _yaml_weights() if _settings_is_config_owner(user_id) else {}
                store.seed_if_absent(user_id, store.ALL_ACCOUNTS, store.WEIGHTS, value)
        except store.SettingsUnavailable:
            return {}                         # store down: defaults, never the file
        return value or {}
    return _yaml_weights()


def _yaml_weights() -> dict:
    """``today_actions.weights`` from the shared config; {} when unreadable."""
    try:
        return (_load_config() or {}).get("today_actions", {}).get("weights") or {}
    except Exception:  # noqa: BLE001
        return {}


def _today_action_weights() -> dict:
    """Resolve today-actions scoring weights: documented defaults overlaid with
    any operator/LLM overrides from the global config store.

    Overrides are PER USER (`user_settings`, fa-amnt.7) for a user actor and the
    global config for a system one — see `_weight_overrides`. Weights are account-wide,
    not per-project. A partial override is valid (missing keys keep their default); a
    malformed value is ignored. Never raises: an unreadable store yields pure defaults.
    The deterministic engine only READS weights.
    """
    weights = dict(_TODAY_DEFAULT_WEIGHTS)
    try:
        overrides = _weight_overrides()
        for key, default in _TODAY_DEFAULT_WEIGHTS.items():
            if key in overrides:
                try:
                    weights[key] = type(default)(overrides[key])
                except (ValueError, TypeError):
                    pass  # malformed override → keep the default
    except Exception:
        pass  # config unreadable → pure defaults
    return weights


def _parse_vikunja_dt(value):
    """Parse a Vikunja RFC3339 timestamp into an AWARE datetime, or None.

    Vikunja's zero sentinel ("0001-01-01T00:00:00Z") and empty/missing values
    are treated as absent (None). Never raises on a malformed string. A naive
    timestamp (no 'Z'/offset) is assumed UTC and returned aware — without this,
    a naive parse would raise TypeError the moment it's compared against the
    aware `now` in the scorer/staleness paths.
    """
    if not value or value == "0001-01-01T00:00:00Z":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _staleness_bucket(updated, now) -> int:
    """Age bucket from an `updated` timestamp: 0 (<7d), 1 (<30d), 2 (<90d),
    3 (>=90d). Absent/unparseable timestamp → 0 (no staleness nudge)."""
    dt = _parse_vikunja_dt(updated)
    if dt is None:
        return 0
    age_days = (now - dt).days
    if age_days < 7:
        return 0
    if age_days < 30:
        return 1
    if age_days < 90:
        return 2
    return 3


def _task_goal_labels(task: dict) -> list:
    """Labels on the task whose title is in the `goal:*` namespace (fa-3lri)."""
    return [l for l in (task.get("labels") or [])
            if str(l.get("title") or "").startswith("goal:")]


def _task_min_duration(task: dict):
    """Smallest POSITIVE `duration_minutes` declared across the task's labels via
    the fa-3lri metadata pattern (e.g. a `~15m` label → {"duration_minutes": 15}),
    or None if no label carries one. Never guesses from the title. Non-positive
    values (a malformed `~0m`/negative label) are ignored, so they can't masquerade
    as the strongest quick-win signal."""
    durations = []
    for label in task.get("labels") or []:
        d = _label_metadata(label).get("duration_minutes")
        if isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0:
            durations.append(d)
    return min(durations) if durations else None


def _task_door_closes(task: dict):
    """Earliest ``door_closes`` deadline declared across the task's labels via the
    fa-3lri metadata pattern (a label whose description JSON carries
    ``{"door_closes": "2026-07-19"}``), as an AWARE datetime, or None.

    ``door_closes`` is an IRREVERSIBLE deadline — a one-way door. Unlike due_date
    (overdue is recoverable, stale is recoverable) a closed door is not, so the
    scorer weights it hyperbolically as the date nears (fa-3729 §1). The EARLIEST
    door wins: the nearest irreversible deadline is the one that constrains the day.
    Absent/unparseable metadata simply doesn't fire — costs a signal, never a wrong
    answer, mirroring `_task_min_duration`."""
    doors = []
    for label in task.get("labels") or []:
        dt = _parse_vikunja_dt(_label_metadata(label).get("door_closes"))
        if dt is not None:
            doors.append(dt)
    return min(doors) if doors else None


def _is_today_candidate(task: dict, now, today_ids=()) -> bool:
    """today-actions.md §1 inclusion predicate. A task is a candidate iff it is
    open AND matches >=1 signal: due by end of today, started by end of today,
    priority >= 3, or is claimed for today (today/08 D3). (Today-kanban-bucket inclusion
    is deferred — bucket-title resolution lands with persistence in Phase 4.)

    NOTE: not yet wired into the shipped pool — `_gather_today_candidates` builds
    the pool by unioning the focused-query primitives (which already cover overdue
    / due-today / priority>=3 / unscheduled). Phase 4 uses THIS predicate to fold
    in `today`-labeled tasks the primitives miss (e.g. a future-dated task the user
    explicitly swiped into today). Until then the `today_ids` path is unused.
    """
    if task.get("done"):
        return False
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start.replace(hour=23, minute=59, second=59, microsecond=999999)

    due = _parse_vikunja_dt(task.get("due_date"))
    if due is not None and due <= today_end:
        return True
    start = _parse_vikunja_dt(task.get("start_date"))
    if start is not None and start <= today_end:
        return True
    if (task.get("priority") or 0) >= 3:
        return True
    if today_ids and task.get("id") in set(today_ids):
        return True
    return False


def _score_today_candidate(task: dict, now, weights: dict) -> tuple:
    """Score one candidate and return ``(score:int, why:list[str])``.

    Pure weighted sum of deterministic signals (today-actions.md §2). Every term
    that fires appends a short, human-readable trace to `why` — this is NOT an
    LLM explanation; it's what makes a swipe UI intelligible and a score auditable.
    A signal whose metadata is absent simply doesn't fire (costs a signal, never a
    wrong answer).
    """
    score = 0
    why = []
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start.replace(hour=23, minute=59, second=59, microsecond=999999)

    due = _parse_vikunja_dt(task.get("due_date"))
    if due is not None:
        if due < today_start:
            delta = today_start - due
            days_overdue = max(1, delta.days + (1 if (delta.seconds or delta.microseconds) else 0))
            pts = min(weights["W_OVERDUE"] * days_overdue, _W_OVERDUE_CAP)
            score += pts
            why.append(f"{days_overdue}d overdue")
        elif due <= today_end:
            score += weights["W_DUE_TODAY"]
            why.append("due today")

    # Irreversibility (fa-3729 §1): a `door_closes` deadline is a ONE-WAY door.
    # Overdue is recoverable; a closed door is not. The bonus rises hyperbolically
    # as the door nears (rectangular hyperbola in days: peak / (1 + days/halflife)),
    # peaks the day it closes, and — being a fact about the world, not the operator —
    # is immune to staleness decay. A door already past stays at peak so it surfaces
    # loudly for a kill decision instead of sinking.
    door = _task_door_closes(task)
    if door is not None:
        # `door_closes` is a floating LOCAL date ("2026-07-19"), not an absolute instant:
        # compare the calendar day AS WRITTEN against the user's local today. Do NOT
        # astimezone-shift it — parsed as UTC midnight, an astimezone into a negative
        # offset would slide the door a day early for non-UTC instances (PR #133 review).
        days_to_door = (door.date() - now.date()).days
        if days_to_door <= 0:
            score += weights["W_DOOR"]
            why.append("door closes today" if days_to_door == 0
                       else f"door closed {abs(days_to_door)}d ago")
        else:
            score += round(weights["W_DOOR"] / (1 + days_to_door / _DOOR_HALFLIFE))
            why.append(f"door closes in {days_to_door}d")

    priority = task.get("priority") or 0
    if priority > 0:
        score += weights["W_PRIORITY"] * priority
        why.append(f"priority {priority}")

    bucket = _staleness_bucket(task.get("updated"), now)
    if bucket > 0:
        score += weights["W_STALE"] * bucket
        why.append(f"stale {_STALE_WHY[bucket]}")

    # Escalation (fa-fhtl / spec 07 §3): a task that has been deferred and has now
    # RETURNED gets LOUDER, proportional to how many times it was pushed away —
    # min(W_DEFER * defer_count, cap), the same linear-with-cap idiom as W_OVERDUE.
    # Additive only, never decays downward. A still-deferred task is excluded upstream
    # (_is_deferred), so any deferred task reaching the scorer is on/after its return.
    defer_count = _defer_count(task)
    if defer_count > 0:
        score += min(weights["W_DEFER"] * defer_count, _W_DEFER_CAP)
        why.append(f"deferred {defer_count}x")

    if _task_goal_labels(task):
        score += weights["W_GOAL"]
        why.append("goal")

    start = _parse_vikunja_dt(task.get("start_date"))
    if start is not None and today_start <= start <= today_end:
        score += weights["W_TIMEBLOCK"]
        why.append("scheduled today")

    duration = _task_min_duration(task)
    if duration is not None and duration <= 15:
        score += weights["W_QUICK"]
        why.append(f"quick (≤{int(duration)}m)")

    return score, why


def _task_kind(task: dict) -> str:
    """'occasion' (carries the `anno` label) | 'event' (has a real end_date) | 'deed'."""
    for l in (task.get("labels") or []):
        if str((l or {}).get("title") or "").lower() == _ANNO_LABEL:
            return "occasion"
    end = task.get("end_date")
    if end and not str(end).startswith("0001"):
        return "event"
    return "deed"


def _event_end(task: dict):
    """When an Event is over: its end_date, else its due_date (aware datetime or None)."""
    end = _parse_vikunja_dt(task.get("end_date"))
    if end is None:
        end = _parse_vikunja_dt(task.get("due_date"))
    return end


def _event_passed(task: dict, now) -> bool:
    end = _event_end(task)
    return end is not None and end < now


def _occasions_map(instance: str = ""):
    """{task_id: 'MM-DD'} for the actor's account. **None** when the store is
    unavailable — distinct from an empty map — so the read derives month/day from due
    dates AND skips the lazy backfill instead of opening one doomed connection per
    `anno` task on the request path (auggie #3 MEDIUM)."""
    try:
        from . import occasions
        return occasions.for_account(_today_claim_actor(), _backend_key(instance, create=False))
    except Exception:  # noqa: BLE001
        return None


def _derive_lifecycle(task: dict, now, occ: dict = None, instance: str = ""):
    """Apply the kind's lifecycle to a task row for the read surfaces. Returns a COPY
    tagged with `kind`, or None when the task should not surface at all.

    - occasion: `due_date` becomes the NEXT occurrence (end of that local day) — so it
      is "due today" on its day and never overdue, regardless of how stale the stored
      due_date is (the courtesy sweep may lag; the read must not).
    - event, ended: priority 0 → None (it happened or it didn't; nothing is owed);
      priority > 0 → kind 'passed' with `ended` — one Reckoning card (D6).
    - anything else: tagged 'deed' / 'event', otherwise untouched.
    """
    store_up = occ is not None
    occ = occ or {}
    kind = _task_kind(task)
    out = dict(task)
    if kind == "occasion":
        md = occ.get(task.get("id")) or _read_anno_md("", fallback_due=task.get("due_date") or "")
        out["kind"] = "occasion"
        if md:
            try:
                nxt = _anchor_occasion_date(md, today=now.date())
                d = datetime.fromisoformat(nxt).date()
                local_eod = datetime.combine(d, datetime.max.time().replace(microsecond=0), tzinfo=now.tzinfo or timezone.utc)
                out["due_date"] = local_eod.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                out["occasion_md"] = md
            except (ValueError, TypeError):
                pass
            if store_up and task.get("id") not in occ:
                # Lazy backfill (D2): first sight of an `anno` task with no fact row.
                # Only when the store answered — a down store is probed once per read
                # (in _occasions_map), never once per task.
                try:
                    from . import occasions
                    occasions.upsert(_today_claim_actor(), _backend_key(instance), int(task.get("id")), md)
                except Exception:  # noqa: BLE001
                    pass
        return out
    if kind == "event":
        end = _event_end(task)
        if end is not None and end < now:
            if (task.get("priority") or 0) <= 0:
                return None
            out["kind"] = "passed"
            out["ended"] = end.isoformat()
            return out
        out["kind"] = "event"
        return out
    out["kind"] = "deed"
    return out


def _is_snoozed(task: dict, now) -> bool:
    """True if the task's start_date is a FUTURE local day relative to `now` — i.e.
    snoozed (swipe-left) or not yet actionable — so it must NOT surface today even
    when overdue / high-priority / unscheduled. Compares LOCAL calendar days so a
    tz-correct snooze (local tomorrow) reliably excludes it."""
    start = _parse_vikunja_dt(task.get("start_date"))
    if start is None:
        return False
    try:
        tz = now.tzinfo or timezone.utc
        return start.astimezone(tz).date() > now.date()
    except Exception:
        return False


def _deferrals_map(instance: str = ""):
    """{task_id: state} from the deferrals table for the actor's account (today/08 D4).
    None when the store is unavailable — distinct from {} — so the overlay falls back
    to the description marker AND skips lazy backfill (no per-task probing of a dead
    store)."""
    try:
        from . import deferrals
        return deferrals.for_account(_today_claim_actor(), _backend_key(instance, create=False))
    except Exception:  # noqa: BLE001
        return None


def _overlay_deferral(task: dict, dmap, instance: str = "") -> dict:
    """Return a COPY of a projected task row carrying its deferral state from the
    TABLE when a row exists (the table wins over the description marker); otherwise
    the marker's parse — and, when the store answered, file that marker as a row
    (lazy backfill, D4).

    Never mutates `task`: the rows come from the 30s task-list cache, which is shared
    across actors outside the per-user override — writing one actor's state into a
    cached row would let the next actor read (and backfill!) it as their own
    (auggie #231 MEDIUM)."""
    out = dict(task)
    tid = out.get("id")
    if dmap is not None and tid in dmap:
        out["defer"] = dict(dmap[tid])
        return out
    marker = out.get("defer") if isinstance(out.get("defer"), dict) else None
    if marker and dmap is not None:
        try:
            from . import deferrals
            deferrals.upsert(_today_claim_actor(), _backend_key(instance), int(tid), marker)
        except Exception:  # noqa: BLE001
            pass
    return out


def _is_deferred(task: dict, now) -> bool:
    """True if the task has a `deferred_until` in the FUTURE (spec 07) — a real
    deferral, so it must not surface today. Mirrors `_is_snoozed` but keys on the
    defer-meta `deferred_until`, not `start_date`. `deferred_until` is a floating
    LOCAL date ("2026-07-20"): compare the day as written against local today (no
    astimezone shift — same rule as the door scorer)."""
    dt = _parse_vikunja_dt(_task_defer_state(task).get("deferred_until"))
    if dt is None:
        return False
    return dt.date() > now.date()


def _gather_today_candidates(instance: str = "", now=None, excluded=None) -> list:
    """Deduped candidate pool for today-actions, composed from the EXISTING
    focused-query primitives rather than a from-scratch query path
    (today-actions.md §4). Unions overdue / due-today / high-priority /
    unscheduled, dedupes by (instance, id), and attaches a deterministic
    ``score`` + ``why`` to each task (via the current weight set).

    The four primitives ARE the §1 inclusion rules (overdue & due-today,
    priority>=3, and unscheduled floaters that feed *Been waiting*); the enriched
    projection means each carries the start_date / updated / labels the scorer
    needs. Plus any `today`-labeled task the user swiped in (which the primitives
    can miss if it's future-dated). Returns tasks sorted by score descending.

    `excluded` is a set of (instance, project_id) whose tasks must NOT surface in
    "today" — Vikunja-archived AND triage-parked projects both (the caller unions
    them; this fn just honors the set). Park is a sweep-aside, so parked projects
    are excluded here exactly like archived ones (fa-k374).
    """
    if now is None:
        now = _today_now(instance)
    excluded = excluded or set()
    weights = _today_action_weights()
    occ = _occasions_map(instance)   # D2 fact table; {} degrades to due-month/day
    dmap = _deferrals_map(instance)  # D4 deferrals table; None = store down (marker only)
    # today/10: handoffs. Imported HERE, inside a try, for the same reason
    # `_today_routine_cluster` imports `routines` that way — the private module does not
    # exist in the extracted public package, and its absence must degrade to "no
    # handoffs" rather than break the panel.
    actor = _today_claim_actor()
    _handoffs = None
    offered_to_me: set = set()
    try:
        # BOTH the import and the owner check live inside the try: `_OWNER_CLAIM_ACTOR`
        # is a @PRIVATE module constant that public extraction does not emit, so naming
        # it from this @PUBLIC_HELPER would NameError in the extracted package. A
        # NameError is an Exception, so it degrades here to "no handoffs" exactly like a
        # missing module — which is the intended behaviour either way (augment #240).
        if actor and actor != _OWNER_CLAIM_ACTOR:
            from . import handoffs as _handoffs  # noqa: F401
            # Vikunja ids of tasks OFFERED to this person. An offer is an explicit
            # interpersonal act, so — exactly like a claim — it overrides the snooze and
            # deferral skips below. Without this, handing over something the recipient
            # had deferred produced an offer they could never see, while the sender's
            # panel went on saying "waiting on" them.
            from . import fe_tasks as _fe
            for fe_id, row in (_handoffs.for_user(actor) or {}).items():
                if row.get("state") != _handoffs.OFFERED or row.get("to_user") != actor:
                    continue
                proj = _fe.projection_of(fe_id)
                if proj:
                    offered_to_me.add((None, int(proj["vikunja_task_id"])))
    except Exception as e:  # noqa: BLE001
        _handoffs = None
        logger.warning("today: handoffs unavailable: %s", e)

    sources = [
        _overdue_tasks_impl(instance=instance, include_meta=True),
        _due_today_impl(instance=instance, include_meta=True),
        _high_priority_tasks_impl(instance=instance, include_meta=True),
        _unscheduled_tasks_impl(instance=instance, include_meta=True),
    ]
    # Pass 1 — dedupe the RAW rows by (instance, id) and drop excluded projects. The
    # rows are cache objects: never mutated here or below (auggie #231).
    by_key = {}
    for res in sources:
        if not isinstance(res, dict) or "error" in res:
            continue
        for task in res.get("tasks", []):
            if (task.get("instance"), task.get("project_id")) in excluded:
                continue  # archived or parked project — don't surface
            by_key.setdefault((task.get("instance"), task.get("id")), task)

    # 5th source (#4): tasks the user explicitly claimed for today (today/08 D3),
    # which the focused-query primitives miss when the task is future-dated. A claim
    # overrides snooze/deferral (snooze withdraws the claim anyway), but NOT exclusion —
    # a task in an archived or parked project stays swept even if it was claimed.
    claimed = set()
    for task in _today_claimed_tasks(instance, now=now):
        if (task.get("instance"), task.get("project_id")) in excluded:
            continue
        key = (task.get("instance"), task.get("id"))
        claimed.add(key)
        by_key.setdefault(key, task)

    # Pass 2 — once per task: deferral overlay (copy), snooze/deferral gates, kind
    # lifecycle (copy), score. One overlay per task also means one lazy backfill per
    # task, not one per source it appeared in.
    candidates = []
    for key, raw in by_key.items():
        task = _overlay_deferral(raw, dmap, instance)            # D4: table wins over the marker
        # An offer overrides snooze/defer for the same reason a claim does: somebody
        # asked you for this, and a decision you cannot see is not a decision.
        if key not in claimed and (None, key[1]) not in offered_to_me:
            if _is_snoozed(task, now):
                continue  # future start_date = swiped-left / not yet actionable
            if _is_deferred(task, now):
                continue  # future deferred_until = a real deferral (spec 07)
        task = _derive_lifecycle(task, now, occ, instance)       # D2/D6: kinds at read time
        if task is None:
            continue  # a passed, priority-0 event: it happened or it didn't
        score, why = _score_today_candidate(task, now, weights)
        task["score"] = score
        task["why"] = why
        candidates.append(task)

    # ATTRIBUTION (today/10 §3.1). On a shared account both people see every task, so
    # this is the only thing that can say whose it is: a task someone else ACCEPTED drops
    # out here, and an OFFER is annotated so the clusterer can lift it into its own
    # decision cluster. Applied after scoring — scoring does not depend on it, and one
    # pass over the finished list is cheaper than a lookup inside the loop.
    if _handoffs is not None:
        try:
            candidates = _handoffs.overlay(
                candidates, actor, _backend_key(instance, create=False))
        except Exception as e:  # noqa: BLE001
            logger.warning("today: handoff overlay failed: %s", e)

    candidates.sort(key=lambda t: t["score"], reverse=True)
    return candidates


def _task_contexts(task: dict) -> list:
    """The task's `@context` label titles (the `@*` namespace, label-metadata.md),
    in label order. These come from the LLM enrichment pass; never guessed."""
    return [str(l.get("title")) for l in (task.get("labels") or [])
            if str(l.get("title") or "").startswith("@")]


def _in_must_clear(task: dict, today_end) -> bool:
    """*Must clear* membership: due by end of today OR priority >= 4."""
    due = _parse_vikunja_dt(task.get("due_date"))
    if due is not None and due <= today_end:
        return True
    return (task.get("priority") or 0) >= 4


def _cluster_candidates(candidates: list, now, project_names: dict = None, instance_urls: dict = None) -> list:
    """Partition scored candidates into the 5 named-intent clusters
    (today-actions.md §3). PURE — no fetches; `project_names` (id->title) and
    `instance_urls` (instance-name->front-end base url, for cross-instance-safe
    deep-links) are supplied by the caller for display and default to empty.

    Each cluster's items are sorted by score descending and capped at
    _N_PER_CLUSTER. Empty clusters are omitted; cluster order is fixed
    (Must clear -> Quick wins -> Move a goal -> Context batches -> Been waiting).
    A task may appear in more than one cluster (the swipe UI dedupes on action).
    """
    project_names = project_names or {}
    instance_urls = instance_urls or {}
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start.replace(hour=23, minute=59, second=59, microsecond=999999)

    def item(task, ctx=None):
        contexts = _task_contexts(task)
        out = {
            "task_id": str(task.get("id")),
            "title": task.get("title"),
            "score": task.get("score", 0),
            "why": task.get("why", []),
            # today/08 kind: 'deed' | 'event' | 'occasion' | 'passed' (the panel keys the
            # card on 'passed' — one "done, or didn't happen?" decision, D6).
            "kind": task.get("kind") or "deed",
            "ended": task.get("ended"),
            # spec 07 state (d): at 3 dread defers the card becomes a portfolio
            # decision (Do/Shrink/Park/Kill, no "Not today") — the panel keys on this.
            "defer_count": _defer_count(task),
            "project": project_names.get((task.get("instance"), task.get("project_id")), ""),
            "context": ctx if ctx is not None else (contexts[0] if contexts else None),
            "instance": task.get("instance"),
            # cross-instance-safe "open in Vikunja" deep-link (fa-bglr.1): the task's
            # OWN instance host, never the viewed one. '' when unresolved.
            "url": _vikunja_task_url(instance_urls.get(task.get("instance"), ""), task.get("id")),
        }
        # today/10: who sent this and what they said, so the card can render
        # "Ivan → before the bins go out" without a second round trip. Added ONLY when
        # there is a live handoff — which is almost never — so the panel payload for
        # everyone not sharing an account is byte-for-byte what it was.
        if task.get("handoff"):
            out["handoff"] = task["handoff"]
        return out

    def take(tasks, ctx=None):
        ranked = sorted(tasks, key=lambda t: t.get("score", 0), reverse=True)[:_N_PER_CLUSTER]
        return [item(t, ctx) for t in ranked]

    clusters = []

    # 0a. Offers (today/10 D2) are a DECISION — accept or decline — not a to-do, so like
    #     passed events they get their own cluster and stay out of every other one. Only
    #     offers TO this person: one they SENT is still their own work until answered.
    offered = [t for t in candidates if (t.get("handoff") or {}).get("stance") == "offered"]
    candidates = [t for t in candidates
                  if (t.get("handoff") or {}).get("stance") != "offered"]

    # 0. Passed events that carried a priority (D6) are a decision, not a to-do: they
    #    get ONE card in their own cluster and stay out of every other one.
    passed = [t for t in candidates if t.get("kind") == "passed"]
    candidates = [t for t in candidates if t.get("kind") != "passed"]

    handoff_cluster = _today_handoff_cluster(take(offered))
    if handoff_cluster:
        clusters.append(handoff_cluster)

    # 1. Must clear — due by today OR priority >= 4
    must_clear = [t for t in candidates if _in_must_clear(t, today_end)]
    must_clear_ids = {(t.get("instance"), t.get("id")) for t in must_clear}
    if must_clear:
        clusters.append({"intent": "must_clear", "label": "Must clear", "items": take(must_clear)})
    if passed:
        # Chronological, not by score: the question is "what's the oldest loose end?"
        chrono = sorted(passed, key=lambda t: t.get("ended") or "")[:_N_PER_CLUSTER]
        clusters.append({"intent": "passed", "label": "Did it happen?", "items": [item(t) for t in chrono]})

    # 2. Quick wins — duration_minutes <= 15 from a ~Nm label (no heuristic guess)
    quick = []
    for t in candidates:
        dur = _task_min_duration(t)
        if dur is not None and dur <= 15:
            quick.append(t)
    if quick:
        clusters.append({"intent": "quick_wins", "label": "Quick wins", "items": take(quick)})

    # 3. Move a goal — has goal:* label AND not already in Must clear
    goal = [t for t in candidates
            if _task_goal_labels(t) and (t.get("instance"), t.get("id")) not in must_clear_ids]
    if goal:
        clusters.append({"intent": "move_a_goal", "label": "Move a goal", "items": take(goal)})

    # 4. Context batches — >= 2 candidates sharing one @context; one cluster each.
    #    Deterministic order: larger batch first, then context title.
    ctx_groups = {}
    for t in candidates:
        for ctx in _task_contexts(t):
            ctx_groups.setdefault(ctx, []).append(t)
    qualifying = sorted(
        ((ctx, members) for ctx, members in ctx_groups.items() if len(members) >= 2),
        key=lambda cm: (-len(cm[1]), cm[0]),
    )
    for ctx, members in qualifying:
        clusters.append({
            "intent": "context_batch",
            "label": f"Context: {ctx}",
            "context": ctx,
            "items": take(members, ctx),
        })

    # 5. Been waiting — no due_date AND no start_date AND staleness_bucket >= 2
    waiting = [t for t in candidates
               if _parse_vikunja_dt(t.get("due_date")) is None
               and _parse_vikunja_dt(t.get("start_date")) is None
               and _staleness_bucket(t.get("updated"), now) >= 2]
    if waiting:
        clusters.append({"intent": "been_waiting", "label": "Been waiting", "items": take(waiting)})

    return clusters


def _today_projects(instance: str = "") -> dict:
    """Best-effort ``(instance, project_id) -> {"title", "archived"}`` map, one
    fetch per relevant instance. Powers cluster-item display (title) AND the
    archived-project exclusion (archived).

    Keyed by (instance, project_id), NOT project_id alone: project IDs are only
    unique within an instance, so a single-instance map would mislabel/mis-archive
    a task against another instance's same-id project. Never raises: any
    per-instance failure is skipped.
    """
    out = {}
    try:
        instances = _get_instances()
        if instance:
            targets = [instance]
        elif instances:
            targets = list(instances.keys())
        else:
            targets = [None]  # single-instance / default mode
        for inst in targets:
            try:
                projects = _list_projects_impl(inst)
            except Exception:
                continue  # one instance down shouldn't blank the rest
            key_inst = inst if inst is not None else "default"
            for p in projects:
                pid = p.get("id")
                if pid is not None:
                    out[(key_inst, pid)] = {
                        "title": p.get("title", ""),
                        "archived": bool(p.get("is_archived")),
                    }
    except Exception:
        return {}
    return out


def _vikunja_task_url(base_url: str, task_id) -> str:
    """A task's Vikunja front-end URL ({base}/tasks/{id}), or '' if either piece is
    missing. One shape shared by the today deck (fa-bglr.1) and the assign queue
    (fa-bglr.9) so their "open in Vikunja" links stay identical."""
    return f"{base_url}/tasks/{task_id}" if (base_url and task_id is not None) else ""


def _instance_url_resolver():
    """A memoized name->front-end-base-URL resolver for cross-instance-safe deep-links.
    Resolving PER instance is the whole point: task ids are per-instance, so one shared
    base would aim a cross-instance link at the wrong Vikunja and 404 (fa-bglr.1 trap /
    fa-bglr.9 augment review, PR #55). Unknown/erroring instance -> ''."""
    cache: dict = {}
    def base(name) -> str:
        if name not in cache:
            try:
                cache[name], _ = _get_instance_config(name or None)
            except Exception:
                cache[name] = ""
        return cache[name]
    return base


def _today_actions_impl(user_id: str = "", instance: str = "", now=None) -> dict:
    """Deterministic "what should I do today?" — scored, clustered candidate
    actions (today-actions.md). PURE function of Vikunja state: no LLM, no side
    effects. `now` is injectable for tests.

    `user_id` is accepted for the public contract (and Phase 5's MCP/multi-user
    surface); data is currently scoped via `instance` and the ambient session
    token, exactly like the sibling task_query primitives.

    `now` defaults to the user's LOCAL time (per the instance timezone) so the
    "today" boundaries follow the user's day, not UTC.
    """
    if now is None:
        now = _today_now(instance)
    projects = _today_projects(instance)
    # Exclude BOTH Vikunja-archived AND triage-PARKED projects from today candidates.
    # Park is meant to sweep a project out of the working surface (fa-k374), not merely
    # tidy the Sort forest — so a parked project's tasks must stop surfacing here too.
    # `projects` keys are (key_inst, pid) with the same "default"/instance normalization
    # as _triage_parked's "inst:pid" strings, so the reconstructed key matches exactly.
    parked_keys = _triage_parked(instance)
    excluded = {key for key, info in projects.items()
                if info.get("archived") or f"{key[0]}:{key[1]}" in parked_keys}
    project_names = {key: info.get("title", "") for key, info in projects.items()}
    candidates = _gather_today_candidates(instance=instance, now=now, excluded=excluded)
    # Per-instance deep-link bases so each card's "open in Vikunja" link routes to the
    # task's OWN instance, never the viewed one (fa-bglr.1 cross-instance trap).
    _base = _instance_url_resolver()
    instance_urls = {c.get("instance"): _base(c.get("instance")) for c in candidates}
    clusters = _cluster_candidates(candidates, now, project_names, instance_urls)
    clustered = {(it.get("instance"), it["task_id"]) for c in clusters for it in c["items"]}
    # Routine goals (fa-kx7l): unmet routines for the period, most urgent first, as
    # their own cluster right after Must clear. They are NOT tasks: kind='routine',
    # task_id='routine:<id>' (never collides with a Vikunja id), do = check in.
    if user_id:
        routine_cluster = _today_routine_cluster(user_id, now)
        if routine_cluster:
            pos = 1 if clusters and clusters[0].get("intent") == "must_clear" else 0
            clusters.insert(pos, routine_cluster)
    # today/10 (fa-odrk): who each account on screen can be handed to. Computed ONCE per
    # account rather than once per card — every card from an account shares its
    # visibility — and memoized, so the panel can decide whether to offer a "Send to…"
    # at all instead of rendering a button that dead-ends on most tasks.
    return {
        "generated_at": now.isoformat(),
        "clusters": clusters,
        "counts": {"candidates": len(candidates), "clustered": len(clustered)},
    }


def _today_handoff_cluster(items: list) -> Optional[dict]:
    """The "Sent to you" cluster (today/10 D2) — pending offers, most recent first.

    Composition, not re-scoring: an offer is not urgent, it is unanswered, so it carries a
    fixed weight rather than competing on the scorer's terms. Hidden entirely when empty,
    like `routine` — a "nothing was sent to you" row would be noise for everyone who is
    not in a household."""
    if not items:
        return None
    return {"intent": "handoff", "label": "Sent to you", "items": items}


def _today_routine_cluster(user_id: str, now) -> Optional[dict]:
    """The 'Routine' cluster for the today panel (composition, no re-scoring):
    routines.today_cluster ranks unmet routines by urgency; this shapes them like
    panel items. Never raises — the panel must render without routines.

    Tagged PUBLIC_HELPER because _today_actions_impl (public) calls it; in the public
    vikunja-mcp package the private `routines` module doesn't exist, so the import
    fails inside the try and the cluster is simply absent — the intended degradation."""
    try:
        from .routines import today_cluster
        today = now.date() if hasattr(now, "date") else now
        items = today_cluster(user_id, today)
    except Exception as e:
        logger.warning(f"today: routine cluster unavailable for {user_id}: {e}")
        return None
    if not items:
        return None
    score_for = {"at_risk": 90, "due": 70, "ok": 45}
    out = []
    for r in items:
        title = f"{r.get('emoji') or ''} {r['name']}".strip()
        out.append({
            "kind": "routine",
            "routine_id": r["routine_id"],
            "task_id": f"routine:{r['routine_id']}",
            "title": title,
            "score": score_for.get(r.get("urgency"), 45),
            "why": r.get("why") or [],
            "defer_count": 0,
            "project": "Routine",
            "context": None,
            "instance": "",
            "url": "",
        })
    return {"intent": "routine", "label": "Routine", "items": out}


def _today_tz(instance: str = "") -> str:
    """The user/instance IANA timezone for 'today' boundaries; 'UTC' if unset."""
    return _get_instance_timezone(instance or None) or "UTC"


def _today_now(instance: str = ""):
    """Current time as an AWARE datetime in the user's LOCAL timezone, so 'today'
    boundaries (midnight) and 'tomorrow' (snooze) follow the user's day, not UTC.
    Falls back to UTC if the timezone is unset or invalid."""
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(_today_tz(instance)))
    except Exception:
        return datetime.now(timezone.utc)


def _today_claim_actor() -> str:
    """Who a today-claim belongs to. MCP/kal/Slack/Matrix set `_current_user_id`; the
    vikunja-native bot path carries the requester's username; a context-less owner or
    CLI session claims as `__owner__` (one shared pool, exactly the old single-owner
    label semantics — never another user's pool)."""
    user_id = _current_user_id.get() or ""
    if not user_id and _bot_mode.get():
        requester = _requesting_user.get() or ""
        if requester:
            user_id = f"vikunja:{requester}"
    return user_id or _OWNER_CLAIM_ACTOR


def _account_key(instance: str = "") -> str:
    """The account key chariot rows are filed under — the instance name as the actor
    knows it ('default', 'household', …). ONE normalizer for every reader and writer
    of every per-account table (today_claims, sweep_marks, special_label_cache), so
    two code paths can never disagree on the key (today/08 defects B and C).

    Under the per-user override an EMPTY instance means the user's own active account
    (token_broker), never the owner's current instance — `_get_current_instance()`
    reads the shared config and would file a user's rows under the owner's name
    (auggie #2 LOW)."""
    if instance:
        return instance
    if _per_user_override():
        uid = _current_user_id.get() or ""
        if uid:
            try:
                from .token_broker import get_user_active_instance
                return get_user_active_instance(uid) or "default"
            except Exception:  # noqa: BLE001
                return "default"
        return "default"
    return _get_current_instance() or "default"


def _today_claim_inst(instance: str = "") -> str:
    """today_claims' account key — see `_account_key`."""
    return _account_key(instance)


def _backend_key(instance: str = "", *, create: bool = True) -> str:
    """The BACKEND a chariot row is filed under — the Vikunja server, not the account
    (today/09, fa-u0ci). ONE resolver, the way `_account_key` is one resolver for the
    alias, so no two code paths can disagree.

    `_account_key` normalizes the alias; this turns that alias into the server it points
    at and mints an `fe_backends` row on first sight. The alias stays what it always was —
    how THIS user selects a credential — but it is no longer part of any identity, which
    is what stops a row filed under `default` from being invisible to a read scoped
    `business` (today/09 §2.3).

    `create` follows the store's read/write split: WRITE paths mint the backend on first
    sight; READ and DELETE paths pass `create=False` so they neither write on a read nor
    raise when the database is down. A backend that does not exist yet can have no rows
    pointing at it, so "" is the correct answer for a read either way.

    Returns "" when the account resolves to no usable URL. Every caller treats that as
    "no chariot state available" and degrades, rather than filing rows under a key that
    means nothing.

    The same "" is returned when the chariot store itself is absent. `fe_tasks` is
    server-side and is not published, so in the extracted single-user package this
    import fails — and "no chariot state available" is exactly the right answer there,
    not an error to propagate up through today_actions (fa-sxac).
    """
    try:
        from . import fe_tasks
    except ImportError:
        return ""
    try:
        url, _token = _get_instance_config(_account_key(instance) or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("_backend_key: cannot resolve instance %r: %s", instance, e)
        return ""
    return fe_tasks.backend_id_for(url, create=create) or ""


def _today_claim_ids(instance: str = "", now=None) -> set:
    """Task ids the actor has claimed for their LOCAL today on `instance`. Empty set
    when the store is unavailable (the read path degrades, never breaks)."""
    try:
        from . import today_claims
    except Exception:
        return set()
    day = (now or _today_now(instance)).date()
    try:
        return today_claims.claimed_ids(_today_claim_actor(), _backend_key(instance, create=False), day)
    except Exception as e:  # noqa: BLE001 — belt and braces: the panel must render
        logger.warning(f"[today] claim read failed for {instance!r}: {e}")
        return set()


def _today_claimed_tasks(instance: str = "", now=None) -> list:
    """Tasks the actor claimed for today (today/08 §4.4): the claim set from the
    store, materialised against the instance's task list. Used to fold swiped-in
    tasks into the candidate pool (#4) — a future-dated task the user explicitly
    said "today" to, which the focused-query primitives miss. Empty when nothing is
    claimed, so the (expensive) task fetch is skipped entirely.

    Scoped to the given/ambient instance, like the engine (cross-instance today is
    today/08 §6)."""
    ids = _today_claim_ids(instance, now=now)
    if not ids:
        return []
    inst = _today_claim_inst(instance)
    result = _list_all_tasks_impl(include_meta=True, instance=inst, allow_truncated=True)
    if not isinstance(result, dict) or "error" in result:
        return []
    return [t for t in result.get("tasks", []) if t.get("id") in ids]


def _today_apply_impl(task_id: int, instance: str = "", source: str = "kal") -> dict:
    """Swipe-right "do today": file a claim on the task for the actor's LOCAL today
    (today/08 D3). Idempotent — claiming twice is the success state. Writes raise
    (an apply that did not persist must not report success). No Vikunja write.

    The claim store is server-side and unpublished, so in the extracted single-user
    package this returns an explicit error rather than an ImportError traceback. It must
    NOT return a success shape — the docstring's rule holds precisely here: a claim that
    could not be filed has not been filed (fa-sxac)."""
    try:
        from . import today_claims
    except ImportError:
        return {"error": "claiming_unavailable",
                "message": "Claiming a task for today needs the server-side claim store, "
                           "which this build does not include."}
    inst = _today_claim_inst(instance)
    day = _today_now(instance).date()
    today_claims.claim(_today_claim_actor(), _backend_key(instance), int(task_id), day, source=source)
    return {"task_id": int(task_id), "instance": inst, "day": day.isoformat(), "today": True}


def _today_snooze_impl(task_id: int, reason: str = "", until: str = "",
                       wake_trigger: str = "", instance: str = "") -> dict:
    """Not today — the reason-aware deferral (spec 07 §Behavior).

    A ``reason`` is REQUIRED. The legacy reasonless snooze-to-tomorrow (the
    canonical lie) has been retired now that both panels send a taxonomy reason —
    a bare call returns ``{"error": "reason_required"}`` rather than silently
    mutating ``start_date``. The taxonomy:

    - Validates ``reason`` against `_DEFER_REASONS`.
    - ``dread`` is the only true deferral: it REQUIRES a date and increments
      `defer_count` (the escalation datum). A dateless dread is rejected — "a deferral
      without a date is a lie."
    - Hard cap at the door: a deferral on/after `door_closes` is rejected with the
      door date (irreversible deadlines can't be snoozed past).
    - `blocked` / `wrong_context` do NOT increment the count (they are misfiled bug
      reports about the task, not deferrals); they may set a `wake_trigger`.
    - State is written to the `<!-- defer-meta -->` description marker surgically, and
      `defer_history` (the eval corpus) is appended, never overwritten. NO `start_date`
      mutation — that conflation was the original defect.
    - On the 3rd `dread` deferral the result carries `rule_of_three` so the card drops
      "Not today" and offers Do / Shrink / Park / Kill (a portfolio decision).
    """
    if not reason:
        return {"error": "reason_required",
                "valid": list(_DEFER_REASONS),
                "message": "Deferral needs a reason (spec 07 taxonomy) — the legacy "
                           "snooze-to-tomorrow was retired. Pick one of `valid`; only "
                           "`dread` defers (and it needs a date)."}

    inst = instance or _get_current_instance()
    if reason not in _DEFER_REASONS:
        return {"error": "invalid_reason", "reason": reason, "valid": list(_DEFER_REASONS)}

    now = _today_now(instance)
    until_dt = _parse_vikunja_dt(until) if until else None

    # dread must carry a date
    if reason in _DEFER_COUNTING_REASONS and until_dt is None:
        return {"error": "date_required", "reason": reason,
                "message": "A `dread` deferral needs an explicit date — "
                           "a deferral without a date is a lie."}

    task = _request("GET", f"/api/v1/tasks/{task_id}", instance=instance or None)

    # hard cap at the door (compare floating local dates, like the door scorer)
    door = _task_door_closes(task)
    if until_dt is not None and door is not None and until_dt.date() >= door.date():
        return {"error": "past_door", "task_id": task_id,
                "door_closes": door.date().isoformat(),
                "deferred_until": until_dt.date().isoformat(),
                "message": f"Deferring to {until_dt.date().isoformat()} is on/after the "
                           f"door closes ({door.date().isoformat()}). A one-way door "
                           f"can't be snoozed past."}

    # read existing state; count/history are additive and survive completion.
    # D4: the deferrals row is the fact; the description marker is the fallback (and
    # stays written as a courtesy until the marker is retired).
    row_state = None
    try:
        from . import deferrals
        row_state = deferrals.get(_today_claim_actor(), _backend_key(instance or "", create=False), int(task_id))
    except Exception:  # noqa: BLE001
        row_state = None
    marker_state = _extract_defer_meta(task.get("description"))
    # Reconcile rather than trust the row blindly: a best-effort row write can fail
    # after the marker landed, leaving the marker one step ahead (auggie #231 LOW).
    # The richer record (higher count, then longer history) is the truth.
    def _richness(st):
        c = st.get("defer_count"); h = st.get("defer_history")
        return (c if isinstance(c, int) and not isinstance(c, bool) else 0,
                len(h) if isinstance(h, list) else 0)
    if row_state is None:
        state = marker_state
    elif not marker_state:
        state = row_state
    else:
        state = max((row_state, marker_state), key=_richness)
    count = state.get("defer_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0
    history = state.get("defer_history")
    history = list(history) if isinstance(history, list) else []

    try:
        score_at_defer, _ = _score_today_candidate(task, now, _today_action_weights())
    except Exception:
        score_at_defer = None

    if reason in _DEFER_COUNTING_REASONS:
        count += 1

    entry = {"ts": now.isoformat(), "reason": reason}
    if until_dt is not None:
        entry["until"] = until_dt.date().isoformat()
    if score_at_defer is not None:
        entry["score_at_defer"] = score_at_defer
    history.append(entry)

    new_state = {"defer_count": count, "defer_reason": reason, "defer_history": history}
    if until_dt is not None:
        new_state["deferred_until"] = until_dt.date().isoformat()
    if wake_trigger:
        new_state["wake_trigger"] = wake_trigger

    new_desc = _write_defer_meta(task.get("description") or "", new_state)
    # _update_task_impl treats a non-HTML description as markdown and would escape our
    # comment; real Vikunja descriptions are HTML (so pass through), but guard the
    # empty/plaintext edge so the marker survives.
    if new_desc and not _is_html(new_desc):
        new_desc = "<p></p>\n" + new_desc
    _update_task_impl(task_id, description=new_desc, instance=instance or None)
    # D4: the row is the fact from here on. Best-effort — the marker write above
    # already persisted the same state, so a store blip loses nothing.
    try:
        from . import deferrals
        deferrals.upsert(_today_claim_actor(), _backend_key(instance or ""), int(task_id), new_state)
    except Exception:  # noqa: BLE001
        pass

    # `_is_deferred` keys on deferred_until, so a task only actually LEAVES today when a
    # date was set. A dateless blocked/wrong_context defer records the reason + wake
    # trigger but keeps surfacing until context-wake ships (D2) — report that honestly
    # rather than claiming today=False (Augment #135).
    removed_from_today = until_dt is not None
    result = {"task_id": task_id, "instance": inst, "reason": reason,
              "defer_count": count, "today": not removed_from_today}
    if until_dt is not None:
        result["deferred_until"] = until_dt.date().isoformat()
    if wake_trigger:
        result["wake_trigger"] = wake_trigger
    if reason in _DEFER_COUNTING_REASONS and count >= _RULE_OF_THREE:
        result["rule_of_three"] = True
        result["portfolio_options"] = ["do_now", "shrink", "park", "kill"]
    if reason == "too_big":
        result["hint"] = "split"          # offer to split; the parent is NOT deferred
    elif reason == "not_mine":
        result["hint"] = "park_or_kill"    # delegated or dead — no deferral
    # "Not today" after "today": withdraw today's claim so the fold-in (#4) stops
    # overriding the snooze. Best-effort — the deferral itself already persisted.
    try:
        from . import today_claims
        today_claims.unclaim(_today_claim_actor(), _backend_key(instance, create=False),
                             int(task_id), _today_now(instance).date())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[today] unclaim after snooze failed for {task_id}: {e}")
    return result


def _today_delete_impl(task_id: int, instance: str = "") -> dict:
    """The "never" gesture — permanently delete the task on its own instance. Vikunja
    has no trash, so this is irreversible server-side; the panel guards it with a
    deferred-delete + undo window client-side (the call only fires if the user
    doesn't undo). Invalidates BOTH the task-list and ICS caches (matching
    _delete_task_impl) so the deleted task stops rendering on the calendar grid
    immediately, not after the ICS TTL (auggie M1)."""
    inst = instance or _get_current_instance()
    _request("DELETE", f"/api/v1/tasks/{task_id}", instance=instance or None)
    _invalidate_ics_cache(inst)
    _invalidate_task_list_cache()
    # Chariot rows keyed on this task are now orphans — drop them (best-effort).
    try:
        from . import deferrals, occasions
        deferrals.forget(_today_claim_actor(), _backend_key(instance or "", create=False), int(task_id))
        occasions.forget(_today_claim_actor(), _backend_key(instance or "", create=False), int(task_id))
    except Exception:  # noqa: BLE001
        pass
    return {"task_id": task_id, "instance": inst, "deleted": True}


def _today_reckoning_impl(instance: str = "", threshold: int = _RULE_OF_THREE) -> dict:
    """Weekly reckoning (spec 07): surface ONLY the chronic deferrals — tasks pushed
    away `defer_count >= threshold` times — **not to do them, to KILL them.** A sibling
    of `triage_parked`: the hundreds of hidden tasks get audited here or nowhere.
    Read-only; returns the offenders sorted by defer_count (loudest first) with their
    reason, next return date, and history depth. `today_snooze` won't even show a
    "Not today" button on these (rule of three) — the reckoning is where they die."""
    # A reckoning is about DEFERRED tasks; threshold < 1 would sweep in every open task
    # (undeferred tasks have defer_count 0), so floor it at 1 (Augment #135).
    threshold = max(1, int(threshold))
    res = _list_all_tasks_impl(include_meta=True, instance=instance, allow_truncated=True)
    tasks = res.get("tasks", []) if isinstance(res, dict) else []
    dmap = _deferrals_map(instance)
    rows = []
    for t in tasks:
        t = _overlay_deferral(t, dmap, instance)
        n = _defer_count(t)
        if n >= threshold:
            state = _task_defer_state(t)
            rows.append({
                "id": t.get("id"),
                "instance": t.get("instance"),
                "title": t.get("title"),
                "project_id": t.get("project_id"),
                "defer_count": n,
                "defer_reason": state.get("defer_reason"),
                "deferred_until": state.get("deferred_until"),
                "history_len": len(state.get("defer_history") or []),
            })
    rows.sort(key=lambda r: r["defer_count"], reverse=True)
    return {
        "threshold": threshold,
        "count": len(rows),
        "tasks": rows,
        "prompt": "Deferred repeatedly. This is a portfolio decision, not a scheduling "
                  "one: do it, shrink it, park it, or kill it — but do not defer again.",
    }


def _today_reset_impl(instance: str = "") -> dict:
    """Manual "clear my today": withdraw every claim the actor filed for their LOCAL
    today on `instance`. There is no scheduled reset any more — yesterday's claims
    stop matching on their own (today/08 §4.4). Returns {instance, cleared}.

    With no server-side claim store there is nothing to clear, and saying so beats an
    ImportError. Reports zero rather than an error: clearing an empty set genuinely
    succeeded (fa-sxac)."""
    try:
        from . import today_claims
    except ImportError:
        return {"instance": _today_claim_inst(instance), "cleared": 0}
    inst = _today_claim_inst(instance)
    day = _today_now(instance).date()
    try:
        cleared = today_claims.clear_day(_today_claim_actor(), _backend_key(instance, create=False), day)
    except Exception as e:  # noqa: BLE001
        return {"instance": inst, "cleared": 0, "error": str(e)}
    return {"instance": inst, "cleared": cleared}


def _set_today_action_weights(weights: dict) -> dict:
    """Persist weight overrides for THIS ACTOR: the user's own `user_settings` row
    (fa-amnt.7), or the global config for a system actor. Only known weight keys are
    accepted; values are coerced to int; unknown keys are rejected (not silently
    stored). Returns ``{set, rejected, weights}`` where `weights` is the fully resolved
    set.

    Validation happens BEFORE anything is WRITTEN, so a call carrying one bad value
    can't leave half of it applied and an all-rejected call writes nothing."""
    user_id, settings = _settings_actor()
    per_user = bool(user_id)
    if per_user and settings is None:
        raise RuntimeError("user_settings unavailable — refusing to write weights into "
                           "the shared config on behalf of a user")

    # Validate FIRST: nothing is WRITTEN until every key has been checked, so a call
    # carrying one bad value can't leave half of it applied, and a call carrying only
    # bad keys writes nothing at all (augment review of #234).
    #
    # It can still SEED, and that is not a loophole: the response reports the resolved
    # weights, which means a read, and any read performs the one-shot migration — the
    # very next `today_get_weights` would do the same. The guarantee is "no partial
    # write", not "no row may come into existence".
    set_keys, rejected = {}, []
    accepted = {}
    for key, value in (weights or {}).items():
        if key not in _TODAY_DEFAULT_WEIGHTS:
            rejected.append(key)
            continue
        try:
            accepted[key] = int(value)
        except (ValueError, TypeError):
            rejected.append(key)
    if not accepted:
        return {"set": {}, "rejected": rejected, "weights": _today_action_weights()}

    if per_user:
        # Seed (idempotent) so this partial update lands on top of the migrated values
        # rather than replacing them.
        _weight_overrides()
        # Atomic shallow merge (`||`), not read-modify-write: two concurrent weight
        # updates must not lose one another (augment review of #234).
        settings.merge_object(user_id, settings.ALL_ACCOUNTS, settings.WEIGHTS, accepted)
        set_keys = dict(accepted)
    else:
        cfg = _load_config() or {}
        store = cfg.setdefault("today_actions", {}).setdefault("weights", {})
        store.update(accepted)
        set_keys = dict(accepted)
        _save_config(cfg)
    return {"set": set_keys, "rejected": rejected, "weights": _today_action_weights()}


def _triage_instances(instance: str = "") -> list:
    """Instance list to walk for the forest. Mirrors _today_projects: one named
    instance, else every configured instance, else single/default mode ([None])."""
    if instance:
        return [instance]
    instances = _get_instances()
    return list(instances.keys()) if instances else [None]


def _settings_log():
    """A logger resolved inline rather than via the module-level `logger`.

    `_settings_module` is deliberately self-contained — it is the function that decides
    whether this process even HAS the private store, so it must not lean on module
    globals that the extracted public package doesn't carry (`logger` is already an
    undefined name there 65 times over; see fa-ew4k). Keeping these two ERROR paths
    self-sufficient costs nothing on a branch that only runs when an import failed.
    """
    return logging.getLogger(__name__)


def _settings_module():
    """The `user_settings` store, or None when it is unavailable.

    The module being ABSENT is the expected case in the extracted public package: no
    multi-user story there, so every per-user branch is dead code. Any OTHER import
    failure is a broken private deploy and is logged at ERROR.

    Either way callers get None — but None must NEVER be read as "use config.yaml" for
    an authenticated user. For a real user that would silently reinstate the shared,
    cross-user parked/weight state this table exists to remove (augment review of #234).
    They degrade to the documented DEFAULTS instead: wrong-but-private beats
    right-but-leaked, and writes raise rather than land in the shared file.

    Resolved dynamically (like the contextvar in `_settings_actor`) so a static
    reference can't become an undefined name in the public extract.
    """
    try:
        from . import user_settings
        return user_settings
    except ModuleNotFoundError as e:
        if (getattr(e, "name", "") or "").endswith("user_settings"):
            return None          # the module itself is absent — the public package
        _settings_log().error(
            "user_settings unavailable (missing %s): per-user settings degrade to "
            "DEFAULTS, never to the shared config", e.name, exc_info=True)
        return None
    except Exception:  # noqa: BLE001
        _settings_log().error(
            "user_settings failed to import: per-user settings degrade to DEFAULTS, "
            "never to the shared config", exc_info=True)
        return None


def _settings_actor():
    """``(user_id, store)`` — the one gate every reader and writer below goes through.

    `user_id` alone decides WHERE state lives, and it is the tenancy boundary:
      - empty  → a system actor (a sweep, the CLI, the legacy owner cookie). These read
        and write `config.yaml`, so they keep seeing what they always saw. (Deliberately
        NOT `_today_claim_actor`, which maps a context-less session onto the shared
        `__owner__` pool: right for claims, wrong here.)
      - set    → per-user rows, and `config.yaml` is now OFF LIMITS for this call no
        matter what — including when `store` comes back None. See `_settings_module`.
    """
    holder = globals().get("_current_user_id")
    user_id = (holder.get() if holder is not None else "") or ""
    if not user_id:
        return ("", None)
    return (user_id, _settings_module())


def _settings_is_config_owner(user_id: str) -> bool:
    """True when `user_id` owns the config.yaml this process reads — the ONLY user
    whose settings may be seeded from it (the file's parked keys are their projects).

    The owner lookup is resolved through `globals()` rather than referenced directly:
    it is @PRIVATE (per-user account management) and so is absent from the extracted
    public package, where a static call would be an undefined name. Absent → False,
    which is the right answer there anyway: no multi-user story, nothing to seed.
    """
    if not user_id:
        return False
    find_owner = globals().get("_find_calendar_owner_user_id")
    if find_owner is None:
        return False
    try:
        return find_owner() == user_id
    except Exception:  # noqa: BLE001
        return False


def _yaml_parked_accounts() -> set:
    """Account names appearing in config.yaml's parked keys. An empty-instance read
    walks EVERY configured account, so the seed has to cover all of them at once —
    seeding only the active one leaves a legacy `household:512` unparked until that
    account happens to be read by name (augment review of #234)."""
    try:
        raw = (_load_config().get("triage") or {}).get("parked") or []
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for entry in raw:
        inst, sep, pid = str(entry).rpartition(":")
        if sep and inst and pid.isdigit():
            out.add(inst)
    return out


def _yaml_parked_ids(account: str) -> list:
    """Project ids parked on `account` per config.yaml — the "inst:pid" strings filtered
    to one account. Malformed entries are skipped, never guessed at."""
    try:
        raw = (_load_config().get("triage") or {}).get("parked") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for entry in raw:
        text = str(entry)
        inst, _, pid = text.rpartition(":")
        if inst == account and pid.isdigit():
            out.append(int(pid))
    return out


def _settings_parked_ids(user_id: str, account: str) -> list:
    """This user's parked project ids on `account`, seeding once from config.yaml.

    A MISSING row (None) means "never seeded" and triggers the seed; a row holding an
    empty list is a real answer ("I unparked everything") and must survive — which is
    why the seed writes even when it has nothing to write.
    """
    _uid, store = _settings_actor()
    if store is None:
        # Only ever reached WITH a user (the system path returns before this), so the
        # owner's file is not an option here — empty is the tenancy-safe degradation.
        return []
    try:
        value = store.get(user_id, account, store.PARKED)
        if value is None:
            # No row = never seeded. Reached ONLY on a successful read, so a database
            # outage can never be mistaken for a fresh user and re-serve config.yaml
            # (augment review of #234).
            value = _yaml_parked_ids(account) if _settings_is_config_owner(user_id) else []
            store.seed_if_absent(user_id, account, store.PARKED, value)
    except store.SettingsUnavailable:
        return []      # store down: defaults, never the shared file
    return [int(x) for x in (value or []) if str(x).isdigit() or isinstance(x, int)]


def _triage_parked(instance: str = "") -> set:
    """Set of parked project keys ("inst:pid"). Park is a durable, reversible flag —
    not Vikunja archival — so revive is a clean toggle.

    Per-user rows for a user actor (fa-amnt.7), the shared config for a system one.
    The "inst:pid" RETURN shape is unchanged so every caller's key arithmetic
    (`f"{key_inst}:{pid}" in parked`) keeps working untouched.
    """
    user_id, store = _settings_actor()
    if not user_id:
        cfg = _load_config()
        raw = (cfg.get("triage") or {}).get("parked") or []
        return set(str(x) for x in raw)
    account = _account_key(instance)
    if instance:
        return {f"{account}:{pid}" for pid in _settings_parked_ids(user_id, account)}
    # No account named, so the caller may walk SEVERAL (the owner-shaped forest, and
    # the `triage_parked` tool's default). Seed every account this read could touch
    # BEFORE unioning: `get_all_accounts` only returns rows that already exist, so
    # seeding just the active one would omit legacy entries on the others until each
    # was read by name (augment review of #234).
    if store is None:
        return set()                          # store down: nothing parked, nothing leaked
    accounts = {account}
    if _settings_is_config_owner(user_id):
        accounts |= _yaml_parked_accounts()   # only the owner has legacy rows to bring over
    for acct in accounts:
        _settings_parked_ids(user_id, acct)
    keys = set()
    try:
        rows = store.get_all_accounts(user_id, store.PARKED)
    except store.SettingsUnavailable:
        return set()   # store down: nothing parked, never the shared file
    for other, value in rows.items():
        if other:
            keys |= {f"{other}:{pid}" for pid in (value or [])}
    return keys


def _triage_set_parked(project_id: int, instance: str = "", parked: bool = True) -> dict:
    """Add/remove a project from the parked set. `instance` empty → 'default', matching
    the forest's empty-instance key so park never silently mismatches (auggie #9).

    A user actor writes their own `user_settings` row (fa-amnt.7); a system actor keeps
    writing config.yaml under the process lock. The returned `instance` is the key the
    row was actually filed under, so the caller reports the same account the forest
    will read it back on."""
    user_id, store = _settings_actor()
    if user_id:
        if store is None:
            raise RuntimeError("user_settings unavailable — refusing to park into the "
                               "shared config on behalf of a user")
        account = _account_key(instance)
        # Seed FIRST (idempotent): a park on an account never read would otherwise
        # create the row from this one id and lose the legacy config.yaml entries.
        _settings_parked_ids(user_id, account)
        # Then mutate in ONE statement. A read-modify-write here would let two rapid
        # taps lose each other's update — the very race `_config_lock` was added for
        # (auggie #8); a per-user row makes it rarer, not impossible, and a process
        # lock wouldn't cover two web workers anyway (augment review of #234).
        if parked:
            store.list_add(user_id, account, store.PARKED, int(project_id))
        else:
            store.list_remove(user_id, account, store.PARKED, int(project_id))
        return {"project_id": project_id, "instance": account, "parked": parked}
    inst_key = instance or "default"
    key = f"{inst_key}:{project_id}"
    with _config_lock:
        cfg = _load_config()
        store = [str(x) for x in (cfg.setdefault("triage", {}).get("parked") or [])]
        if parked and key not in store:
            store.append(key)
        elif not parked and key in store:
            store.remove(key)
        cfg["triage"]["parked"] = store
        _save_config(cfg)
    return {"project_id": project_id, "instance": inst_key, "parked": parked}


def _triage_due_state(task: dict, now_utc) -> str:
    """'overdue' | 'future' | 'undated' for a task's due_date. Missing/zero/unparseable
    → 'undated'; a naive timestamp is assumed UTC rather than silently swallowed (#6)."""
    due = task.get("due_date")
    if not due or str(due).startswith("0001"):
        return "undated"
    try:
        d = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "undated"
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return "overdue" if d < now_utc else "future"


def _triage_task_state(task: dict, now, now_utc, today_ids=frozenset()):
    """Triage load class of a task: 'overdue' | 'undated', or None when it already has a
    home — done, claimed for today (committed), snoozed (future start_date), or
    future-dated. This is what makes someday/today actually CLEAR the pile (auggie #3):
    a deferred or committed task stops counting as load and leaves the assignment queue.
    `today_ids` is the actor's claim set for the task's instance (today/08 D3)."""
    if task.get("done"):
        return None
    if today_ids and task.get("id") in today_ids:
        return None
    if _is_snoozed(task, now):
        return None
    # today/08 §2: an Occasion is never load (it rolls), and a passed Event is a
    # decision card (D6) or nothing — neither belongs in the overdue pile.
    kind = _task_kind(task)
    if kind == "occasion" or (kind == "event" and _event_passed(task, now)):
        return None
    st = _triage_due_state(task, now_utc)
    return st if st in ("overdue", "undated") else None


def _triage_counts(inst_list: list, now) -> tuple:
    """Per-(instance, project_id) DIRECT overdue + undated counts of un-homed tasks
    (excludes done/today/snoozed/future via _triage_task_state). Rides the 30s task-list
    cache. Returns (counts, truncated) — truncated flags an undercounted load (#7)."""
    now_utc = now.astimezone(timezone.utc)
    counts: dict = {}
    truncated = False
    for inst in inst_list:
        try:
            res = _list_all_tasks_impl(include_meta=True, instance=inst, allow_truncated=True)
        except Exception:
            continue
        if not isinstance(res, dict):
            continue
        truncated = truncated or bool(res.get("truncated"))
        key_inst = inst if inst is not None else "default"
        # No `now=` here on purpose: the forest spans instances, and a claim is stamped
        # with ITS instance's local day by the writer — so look it up the same way
        # (auggie #228 LOW: one ambient `now` misreads a claim near midnight across tz).
        today_ids = _today_claim_ids(inst or "")
        for t in res.get("tasks", []):
            pid = t.get("project_id")
            if pid is None:
                continue
            st = _triage_task_state(t, now, now_utc, today_ids)
            if st is None:
                continue
            counts.setdefault((key_inst, pid), {"overdue": 0, "undated": 0})[st] += 1
    return counts, truncated


def _list_projects_complete(instance=None, per_page: int = 50, max_pages: int = 20) -> tuple:
    """``(projects, complete)`` — the instance's projects, and whether that list is
    provably ALL of them (fa-tpoo, augment review of #235).

    `_list_projects_impl` is fine for rendering but must never be used to conclude a
    project does not EXIST: `_fetch_all_pages` defaults to `raise_on_error=False`, so a
    mid-pagination failure silently returns a short list, and it stops at `max_pages`.
    Either would make a live project look deleted and get its park key reaped. The
    docstring on `raise_on_error` already says it: "use for completeness-critical
    callers" — the orphan reap is the first caller that is one.

    Incomplete is NOT an error: the caller still renders from whatever came back, it
    just refuses to draw conclusions from an absence.
    """
    try:
        rows = _fetch_all_pages("GET", "/api/v1/projects", per_page=per_page,
                                max_pages=max_pages, instance=instance,
                                raise_on_error=True)
    except Exception:
        # The raise loses whatever it had, so re-run leniently purely so the desk still
        # RENDERS — flagged incomplete, which is what stops the caller concluding
        # anything from an absence.
        return ([_format_project(p) for p in _fetch_all_pages(
            "GET", "/api/v1/projects", per_page=per_page, max_pages=max_pages,
            instance=instance)], False)
    return ([_format_project(p) for p in rows], len(rows) < per_page * max_pages)


def _triage_reconcile_parked(parked: set, fetched: dict) -> tuple:
    """Split stored parked keys against the projects the forest actually fetched
    (fa-tpoo). PURE. Returns ``(archived_parked, orphans)``:

      archived_parked  [{id, title, instance}] — the project exists but is archived, so
                       it has no node and `prune_parked` can't list it. Without this it
                       is swept aside with no Revive button: invisible AND stuck.
      orphans          [(instance, project_id)] — no such project at all. The park key
                       outlived the project and should be reaped.

    An instance is only judged for ORPHANS when its entry is marked ``complete`` — a
    provably exhaustive project list. Two ways to fail that, and both would destroy real
    state: the fetch raised (instance absent from `fetched` entirely), or it came back
    silently truncated (`_list_projects_complete`, augment #235). Reading "I couldn't
    see it" as "it does not exist" is the same shape of mistake as treating a store
    outage as a fresh user (review of #234).

    Archived surfacing needs no such gate: it is driven by a project being PRESENT, and
    presence is trustworthy even in a partial list.
    """
    archived_parked, orphans = [], []
    for entry in parked:
        inst, sep, pid_text = str(entry).rpartition(":")
        if not sep or not pid_text.isdigit():
            continue                        # malformed key: not ours to interpret
        pid = int(pid_text)
        seen = fetched.get(inst)
        if seen is None:
            continue                        # this instance never answered — say nothing
        if pid in seen["archived"]:
            archived_parked.append({"id": pid, "title": seen["archived"][pid],
                                    "instance": inst, "archived": True})
        elif pid not in seen["ids"] and seen.get("complete"):
            # ABSENCE only means "deleted" against a provably complete list. A truncated
            # or partially-failed page walk omits live projects, and reaping on that
            # would destroy real parked state (augment #235). Presence is still
            # trustworthy either way, which is why archived surfacing needs no such gate.
            orphans.append((inst, pid))
    return archived_parked, orphans


def _triage_reap_orphan_parks(orphans: list) -> None:
    """Drop park keys whose project no longer exists, for a per-user actor (fa-tpoo).

    Lazy on read, like the other chariot backfills. Best-effort and silent on failure —
    reaping a tombstone is housekeeping, and it must never break the forest that just
    rendered correctly without it.

    Only the per-user store is reaped. The system actor's `config.yaml` is left alone on
    purpose: writing the shared file from a READ path is what made the original
    per-user-state mess hard to reason about, and that path is being retired anyway.
    """
    if not orphans:
        return
    user_id, store = _settings_actor()
    if not user_id or store is None:
        return
    for inst, pid in orphans:
        try:
            store.list_remove(user_id, inst, store.PARKED, pid)
        except Exception as e:  # noqa: BLE001
            # Same self-contained logger as the settings gate: this runs on the read
            # path of a function the public extract carries, where `logger` is undefined.
            _settings_log().warning(
                "triage: could not reap orphan park %s:%s — %s", inst, pid, e)


def _triage_forest_impl(instance: str = "", now=None) -> dict:
    """The project-triage forest: the project tree (per instance), each node carrying
    its DIRECT overdue + undated counts; the client rolls the subtree up. Archived
    projects are dropped; parked roots are sectioned into `parked`. Nodes are sorted
    by subtree load (hottest first). Read-only."""
    if now is None:
        now = _today_now(instance)
    inst_list = _triage_instances(instance)
    parked = _triage_parked(instance)
    counts, truncated = _triage_counts(inst_list, now)

    nodes: dict = {}
    # What each instance's projects fetch actually returned, for reconciling the stored
    # parked set below (fa-tpoo). An instance is recorded ONLY on a successful fetch —
    # the `except: continue` below is a transient outage, and treating "I couldn't ask"
    # as "the project is gone" would reap a user's whole parked set on one bad request.
    fetched: dict = {}
    for inst in inst_list:
        try:
            projects, complete = _list_projects_complete(inst)
        except Exception:
            continue
        key_inst = inst if inst is not None else "default"
        seen = fetched.setdefault(key_inst, {"ids": set(), "archived": {}, "complete": complete})
        seen["complete"] = seen["complete"] and complete
        for p in projects:
            pid = p.get("id")
            if pid is None:
                continue
            seen["ids"].add(pid)
            if p.get("is_archived"):
                # Archived projects get no node — but a parked one still has to be
                # REACHABLE, or it is swept aside with no Revive button anywhere
                # (fa-tpoo). Keep the title so it can be listed under `parked`.
                seen["archived"][pid] = p.get("title", "")
                continue
            c = counts.get((key_inst, pid), {})
            nodes[(key_inst, pid)] = {
                "id": pid, "title": p.get("title", ""), "hex": p.get("hex_color", "") or "",
                "instance": key_inst, "_parent": p.get("parent_project_id") or 0,
                "position": p.get("position", 0) or 0,
                "overdue": c.get("overdue", 0), "undated": c.get("undated", 0),
                "parked": f"{key_inst}:{pid}" in parked, "children": [],
            }

    # Build an ACYCLIC forest (auggie #4). Index children by parent, then attach via DFS
    # from the roots with a visited guard: a back-edge to an already-visited node is NOT
    # linked, so a self-parent or cycle is broken in the OUTPUT (not just in traversal —
    # a cyclic child structure would crash JSON serialization). Pure-cycle islands
    # (no real root) are promoted so no project is ever silently lost.
    kids_by_parent: dict = {}
    for node in nodes.values():
        par = node["_parent"]
        if par and par != node["id"]:
            kids_by_parent.setdefault((node["instance"], par), []).append(node)

    visited = set()
    def attach(node):
        visited.add((node["instance"], node["id"]))
        for child in kids_by_parent.get((node["instance"], node["id"]), []):
            ck = (child["instance"], child["id"])
            if ck in visited:
                continue  # back-edge → break the cycle
            node["children"].append(child)
            attach(child)

    roots = []
    for key, node in nodes.items():
        par = node["_parent"]
        is_root = not (par and par != node["id"] and (node["instance"], par) in nodes)
        if is_root:
            roots.append(node)
            attach(node)
    for key, node in nodes.items():     # promote any unreached cycle island
        if key not in visited:
            roots.append(node)
            attach(node)

    # Section OFF parked subtrees at ANY depth — not just roots. Parking a project sweeps
    # it and its whole subtree aside, so a parked node must be pruned from the tree
    # wherever it sits (root OR child) and surfaced in `parked`. The old root-only filter
    # silently no-op'd parking a CHILD project (most business projects live under the
    # venture hierarchy): the node kept parked=True but stayed in its parent's children,
    # and its load stayed in the roll-up. Prune BEFORE roll-up so totals shed swept load.
    parked_list: list = []
    def prune_parked(children: list) -> list:
        kept = []
        for c in children:
            if c["parked"]:
                # whole subtree goes with it — not re-listed per descendant
                parked_list.append({"id": c["id"], "title": c["title"], "instance": c["instance"]})
            else:
                c["children"] = prune_parked(c["children"])
                kept.append(c)
        return kept
    tree = prune_parked(roots)
    # Reconcile the STORED parked set against what the fetch actually saw (fa-tpoo).
    # Archived-but-parked projects have no node, so `prune_parked` above can never list
    # them — add them here or they are invisible AND unrevivable. Orphans (no project at
    # all) are reaped from the store.
    archived_parked, orphans = _triage_reconcile_parked(parked, fetched)
    parked_list.extend(archived_parked)
    _triage_reap_orphan_parks(orphans)
    # Deterministic `parked` order (grouped by instance, then title) — prune harvests in
    # traversal order, which isn't otherwise sorted; a stable order keeps the "swept aside"
    # list from reshuffling between fetches (augment #131).
    parked_list.sort(key=lambda p: (str(p["instance"]), p["title"].lower(), p["id"]))

    def rollup(n):
        od, un = n["overdue"], n["undated"]
        for c in n["children"]:
            r = rollup(c); od += r[0]; un += r[1]
        # Native Vikunja order within a parent (position), not load-shuffle — the counts
        # surface load; the ORDER stays predictable / matches the instance (UAT).
        n["children"].sort(key=lambda c: (c["position"], c["id"]))
        n["_roll"] = (od, un)
        return od, un
    for r in tree:
        rollup(r)
    # Roots grouped BY INSTANCE, then native position within each instance.
    tree.sort(key=lambda n: (str(n["instance"]), n["position"], n["id"]))

    totals = {"overdue": sum(n["_roll"][0] for n in tree),
              "undated": sum(n["_roll"][1] for n in tree)}

    def strip(n):
        n.pop("_roll", None); n.pop("_parent", None)
        for k in n["children"]:
            strip(k)
    for n in tree:
        strip(n)

    return {"instance": instance, "roots": tree, "parked": parked_list, "totals": totals,
            "truncated": truncated, "generated_at": now.isoformat()}


def _triage_parked_list_impl(instance: str = "") -> dict:
    """The parked ("swept aside") projects — the Sort-tab state that's otherwise
    invisible outside the calendar UI (fa-hfg6). Reuses the forest's own `parked`
    sectioning so it stays consistent with what Sort shows. Lets the conversation
    (Claude Desktop / in-app chat) see what's set aside to reason about or revive it.
    Returns {parked: [{id, title, instance}], count}."""
    forest = _triage_forest_impl(instance=instance)
    parked = forest.get("parked", [])
    return {"parked": parked, "count": len(parked)}


def _triage_park_impl(project_id: int, parked: bool = True, instance: str = "",
                      verify: bool = True) -> dict:
    """Park/revive one project, resolving the instance key the way the FOREST keys it.

    The hazard this exists to close: `_triage_set_parked` maps an empty instance to
    the literal key "default", but `_triage_forest_impl` keys every node by its
    concrete instance name whenever instances are configured (`_triage_instances`
    returns [None] — and thus "default" — only in single-instance mode). So a park
    written with instance="" against a multi-instance config lands on a key no forest
    node can ever carry: it reports success and is invisible forever. Resolving the
    empty case to the CURRENT instance keeps the key concrete in multi-instance mode
    and still yields "default" in single-instance mode, matching the forest both ways.

    `verify` (default True) also confirms the project exists in that instance, so a bad id
    parks a phantom loudly instead of silently. Callers that just rendered the project from
    the forest — i.e. the calendar UI — pass `verify=False`: the id is known-good and the
    check would only buy a redundant API round-trip on an interactive path. The key
    resolution above is the part that must be shared; existence checking is caller policy.

    Verification fetches the ONE project rather than listing all of them. `_list_projects_impl`
    goes through `_fetch_all_pages`, whose `raise_on_error` defaults to False — an outage or a
    failed later page comes back as an empty/partial list, which is indistinguishable from
    "no such project". That would report `project_not_found` for a project that exists and
    refuse a legitimate park during an outage (augment review, PR #196). A single GET raises
    instead, so the two cases stay separable.
    """
    inst = instance or _get_current_instance() or ""
    title = ""

    if verify:
        try:
            project = _get_project_impl(int(project_id), inst or None)
        except Exception as e:
            # _request raises ValueError("Resource not found: ...") for a 404 and a
            # differently-worded ValueError for 401/403/5xx. Only the 404 means the id is
            # wrong; everything else means we could not find out, which is not the same
            # answer and must not be reported as one.
            detail = str(e)
            if detail.startswith("Resource not found"):
                return {"error": "project_not_found", "project_id": int(project_id),
                        "instance": inst or "default"}
            return {"error": "instance_unreachable", "instance": inst or "default",
                    "detail": detail}
        title = (project or {}).get("title", "")

    result = _triage_set_parked(int(project_id), instance=inst, parked=parked)
    if verify:
        result["title"] = title
    return result


def _assign_preview_text(raw: str, cap: int = 500) -> str:
    """Plain-text snippet of a task description for the inspect-before-act preview
    (fa-bglr.9) — so the user can EXAMINE a task (often a 'task' that's really a note)
    before assigning or deleting it. Strips HTML (script/style content too), unescapes
    entities, collapses whitespace, caps length."""
    if not raw:
        return ""
    import html as _htmlmod
    # Unescape FIRST so encoded markup (&lt;b&gt;…) becomes real tags and gets stripped
    # too — otherwise it would resurface as visible "<b>" text in the plain-text snippet
    # (augment review, PR #55). Order: decode → drop script/style w/ content → tags → ws.
    s = _htmlmod.unescape(raw)
    s = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'<\s*br\s*/?>', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:cap]


def _assign_queue_impl(project_id: int, instance: str = "", now=None) -> dict:
    """The actionable tasks in one project — its OVERDUE + UNDATED tasks (the ones that
    need a home). Where you go when you tap a project's "N to assign" in the forest.
    Future-dated tasks are excluded (already scheduled). Overdue first, then priority."""
    if now is None:
        now = _today_now(instance)
    now_utc = now.astimezone(timezone.utc)
    res = _list_all_tasks_impl(include_meta=True, instance=instance or None, allow_truncated=True)
    # Deep-link base (fa-bglr.9 / fa-bglr.1): resolve PER ITEM-INSTANCE, not once — the
    # queue can span instances when `instance` is empty (two instances may share a
    # project id), so a single base_url would point some items at the wrong Vikunja
    # frontend (augment review, PR #55). Shared memoized resolver. {url}/tasks/{id}.
    _inst_base = _instance_url_resolver()
    items = []
    truncated = False
    if isinstance(res, dict):
        truncated = bool(res.get("truncated"))
        today_ids = _today_claim_ids(instance, now=now)
        for t in res.get("tasks", []):
            if t.get("project_id") != int(project_id):
                continue
            st = _triage_task_state(t, now, now_utc, today_ids)
            if st is None:
                continue  # done / today / snoozed / future-dated = already has a home
            tid = t.get("id")
            item_inst = t.get("instance") or instance or _get_current_instance()
            base_url = _inst_base(item_inst)
            items.append({
                "task_id": tid, "title": t.get("title", ""),
                "priority": t.get("priority", 0) or 0,
                "instance": item_inst,
                "overdue": st == "overdue", "undated": st == "undated",
                # inspect-before-act (fa-bglr.9): deep-link + a preview of what this is
                "url": _vikunja_task_url(base_url, tid),
                "description": _assign_preview_text(t.get("description", "")),
                "labels": [l.get("title", "") for l in (t.get("labels") or []) if l.get("title")],
            })
    items.sort(key=lambda i: (i["overdue"], i["priority"]), reverse=True)
    return {"project_id": int(project_id), "items": items, "count": len(items), "truncated": truncated}


def _assign_verify_deletable(task_id: int, instance: str = "", now=None):
    """Confirm task_id is a currently-actionable (not-done, un-homed overdue/undated)
    task before an irreversible delete — the assignment-queue analog of
    _today_find_candidate. Returns the task (carrying its real instance) or None, so the
    delete binds to a genuinely-surfaced task and ITS instance, never a body id (#1)."""
    if now is None:
        now = _today_now(instance)
    now_utc = now.astimezone(timezone.utc)
    res = _list_all_tasks_impl(include_meta=True, instance=instance or None, allow_truncated=True)
    tid = str(task_id)
    today_ids = _today_claim_ids(instance, now=now)
    for t in (res.get("tasks", []) if isinstance(res, dict) else []):
        if str(t.get("id")) == tid and _triage_task_state(t, now, now_utc, today_ids) is not None:
            return t
    return None


def _assign_apply_impl(task_id: int, disposition: str, instance: str = "", now=None) -> dict:
    """Give an undated/overdue task a home. Dispositions reuse the today-panel writes:
      done    → mark complete (keeps the task + its notes; reversible — for a "task"
                that's really a note with no action left)
      today   → the `today` label (do it today)
      week    → due_date = +7 days (a deadline; leaves the undated pile)
      someday → start_date = +90 days (defer far; resurfaces in a season)
      delete  → permanent delete
    """
    if now is None:
        now = _today_now(instance)
    if disposition == "today":
        return _today_apply_impl(task_id, instance=instance, source="assign")
    if disposition == "delete":
        return _today_delete_impl(task_id, instance=instance)
    if disposition == "done":
        # Instance-aware complete (read-modify-write; _complete_task_impl is ambient-only).
        cur = _request("GET", f"/api/v1/tasks/{task_id}", instance=instance or None)
        cur["done"] = True
        _request("POST", f"/api/v1/tasks/{task_id}", json=cur, instance=instance or None)
        _invalidate_ics_cache(instance or _get_current_instance())
        _invalidate_task_list_cache()
        return {"task_id": task_id, "disposition": "done", "done": True}
    if disposition == "week":
        due = (now + timedelta(days=7)).replace(hour=23, minute=59, second=0, microsecond=0)
        due_utc = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _update_task_impl(task_id, due_date=due_utc, instance=instance or None)
        return {"task_id": task_id, "disposition": "week", "due_date": due_utc}
    if disposition == "someday":
        start = (now + timedelta(days=90)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _update_task_impl(task_id, start_date=start_utc, instance=instance or None)
        return {"task_id": task_id, "disposition": "someday", "start_date": start_utc}
    raise ValueError(f"unknown disposition: {disposition}")


def _assign_apply_guarded(task_id, disposition: str, instance: str = "", now=None) -> dict:
    """THE one place an assignment disposition is validated and applied.

    Both `/calendar-api/assign` (POST) and the `assign_apply` MCP tool route through here,
    so the irreversible-delete hardening cannot drift between the two surfaces. That drift
    is not hypothetical: it is why park and assign were UI-only in the first place
    (`fa-bg5x`, `fa-yj5n`) — the guards lived inline in the HTTP handler with nothing
    shared, so an MCP layer could only be written by copying them or by going without.

    Division of labor. **Transport auth stays with the caller** — session validation,
    instance scoping, the owner-mode fail-closed, and the MCP tool's `confirm_delete` are
    properties of *how you got here*. **This guard owns the domain rules**, which hold
    regardless of surface:
      - the disposition must be one of `ASSIGN_DISPOSITIONS`;
      - `delete` is irreversible, so it must bind to a genuinely-surfaced actionable task
        (`_assign_verify_deletable`) and take its instance from **that verified task**,
        never from a caller-supplied value (auggie #1).

    Raises `AssignRefused`; returns the disposition result otherwise.
    """
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise AssignRefused("task_id + valid disposition required", 400)
    if disposition not in ASSIGN_DISPOSITIONS:
        raise AssignRefused("task_id + valid disposition required", 400)

    if disposition == "delete":
        verified = _assign_verify_deletable(tid, instance=instance, now=now)
        if verified is None:
            raise AssignRefused("not a current assignable task", 403)
        # Bind to the verified task's own instance — never the caller's claim.
        instance = verified.get("instance") or instance

    return _assign_apply_impl(tid, disposition, instance=instance, now=now)


@mcp.tool()
@mcp_tool_with_fallback
def today_actions(
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = all instances."),
    user_id: str = Field(default="", description="Optional user id for the public contract; data is scoped by instance + session token.")
) -> dict:
    """What should I do today? — deterministic, scored, clustered candidate
    actions (no LLM). Returns named-intent clusters (Must clear / Quick wins /
    Move a goal / Context batches / Been waiting), each item carrying a `score`
    and a `why` trace. Times/boundaries follow the user's local timezone.
    """
    return _today_actions_impl(user_id=user_id, instance=instance)


@mcp.tool()
@mcp_tool_with_fallback
def today_snooze(
    task_id: int = Field(description="ID of the task to defer out of today"),
    reason: str = Field(description="Deferral reason (spec 07 taxonomy), REQUIRED (no default): 'dread' (the only true deferral — needs a date), 'blocked', 'too_big', 'wrong_context', 'not_mine'. A deferral must name why — the legacy snooze-to-tomorrow was retired."),
    until: str = Field(default="", description="Date to defer until, e.g. '2026-07-22'. MANDATORY for reason='dread'. Rejected if on/after the task's door_closes."),
    wake_trigger: str = Field(default="", description="Context to wake on for 'blocked'/'wrong_context' defers, e.g. '@laptop'."),
    instance: str = Field(default="", description="Vikunja instance name. Empty = current instance.")
) -> dict:
    """"Not today" — reason-aware deferral (spec 07). With a `reason` it records the
    taxonomy, increments defer_count only for `dread` (which requires a date), rejects
    deferrals past a `door_closes`, appends to defer_history, and flags `rule_of_three`
    on the 3rd dread defer. Deferred tasks ESCALATE on return, never sink. A `reason`
    is required — a bare call returns `reason_required` (the legacy snooze-to-tomorrow
    was retired)."""
    return _today_snooze_impl(task_id, reason=reason, until=until,
                              wake_trigger=wake_trigger, instance=instance)


@mcp.tool()
@mcp_tool_with_fallback
def today_set_weights(
    weights: dict = Field(description="Map of weight overrides, e.g. {\"W_OVERDUE\": 15, \"W_GOAL\": 12}. Keys: W_OVERDUE, W_DUE_TODAY, W_PRIORITY, W_STALE, W_GOAL, W_TIMEBLOCK, W_QUICK, W_DOOR, W_DEFER. Unknown keys are rejected.")
) -> dict:
    """Re-tune today-actions scoring at runtime (no code change). Persists the
    overrides for YOU — a connected user's weights are their own (`user_settings`,
    today/08 §5.2); only a context-less system/CLI call writes the shared config.
    Every later deterministic run for that same actor honors them. Returns the keys
    set, any rejected, and the fully resolved weight set."""
    return _set_today_action_weights(weights)


@mcp.tool()
@mcp_tool_with_fallback
def today_get_weights() -> dict:
    """Show the current today-actions scoring weights: the documented defaults overlaid
    with YOUR saved overrides (per-user; a system/CLI call sees the shared config's)."""
    return {"weights": _today_action_weights(), "defaults": dict(_TODAY_DEFAULT_WEIGHTS)}


@mcp.tool()
@mcp_tool_with_fallback
def today_reckoning(
    instance: str = Field(default="", description="Vikunja instance name. Empty = all instances."),
    threshold: int = Field(default=3, description="Minimum defer_count to surface (default 3 — the rule of three).")
) -> dict:
    """The weekly reckoning — surface ONLY tasks deferred `threshold`+ times (default
    3), the chronic avoiders. Not a to-do list: it exists to KILL. Sibling of
    triage_parked. Each row carries defer_count, reason, next return date, and history
    depth, loudest first. The answer is do / shrink / park / kill — never defer again."""
    return _today_reckoning_impl(instance=instance, threshold=threshold)


@mcp.tool()
@mcp_tool_with_fallback
def triage_parked(
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'personal', 'business'). Empty = all configured instances.")
) -> dict:
    """List the projects you've PARKED ("swept aside") in the triage forest — the
    Sort-tab state that's otherwise invisible outside the calendar UI. Lets the
    conversation see what's set aside so it can reason about or revive it. Returns
    {parked: [{id, title, instance}], count}."""
    return _triage_parked_list_impl(instance=instance)


@mcp.tool()
@mcp_tool_with_fallback
def triage_park(
    project_id: int = Field(description="ID of the project to park (sweep aside) or revive."),
    parked: bool = Field(default=True, description="True = park (sweep aside). False = revive (un-park)."),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'business'). Empty = the CURRENT instance — never 'all', because a park is always written against exactly one instance.")
) -> dict:
    """PARK a whole project — "read the desk, don't organize it": sweep a speculative
    tree aside in one gesture so the work that matters can speak. The counterpart to
    triage_parked (which only lists). Park is a durable, REVERSIBLE flag stored for
    YOU (`user_settings`, today/08 §5.2; a system/CLI call writes the shared config) —
    it never touches Vikunja, never deletes a task, and never marks anything done;
    pass parked=False to revive. Use it on inert trees (all-undated, never-touched
    batches), not on individual tasks. Returns {project_id, instance, parked, title}."""
    return _triage_park_impl(project_id, parked=parked, instance=instance)


@mcp.tool()
@mcp_tool_with_fallback
def assign_queue(
    project_id: int = Field(description="ID of the project whose un-homed tasks you want."),
    instance: str = Field(default="", description="Vikunja instance name (e.g., 'business'). Empty = the CURRENT instance — not 'all', because project ids can collide across instances.")
) -> dict:
    """The tasks in one project that need a HOME — its overdue + undated set, the work
    standard due-date tooling never surfaces. Future-dated and already-committed tasks are
    excluded (they have a home). Overdue first, then priority. Each item carries a `url`
    and a description preview so you can INSPECT before acting — much of an undated pile
    is really notes, not actions. Feed the results to assign_apply."""
    return _assign_queue_impl(project_id, instance=instance or _get_current_instance() or "")


@mcp.tool()
@mcp_tool_with_fallback
def assign_apply(
    task_id: int = Field(description="ID of the task to give a home. Should be one surfaced by assign_queue."),
    disposition: str = Field(description="done = complete it, keeping the task and its notes (the honest verb for a 'task' that was always a note). today = the `today` label. week = due in 7 days. someday = defer 90 days. delete = PERMANENT, requires confirm_delete=True."),
    instance: str = Field(default="", description="Vikunja instance name. Empty = the CURRENT instance."),
    confirm_delete: bool = Field(default=False, description="Required True for disposition='delete'. No other disposition reads this.")
) -> dict:
    """Give one undated/overdue task a home — the verb that actually drains the pile.
    Dispositions: done · today · week · someday · delete.

    `delete` is PERMANENT and is deliberately harder to reach here than in the UI: it
    needs confirm_delete=True, and it still must bind to a task the assignment queue is
    currently surfacing. Prefer `someday` (reversible) when unsure, and prefer parking the
    whole project (triage_park) over deleting its tasks one by one."""
    if disposition == "delete" and not confirm_delete:
        return {"error": "confirm_delete_required", "task_id": task_id,
                "detail": "delete is permanent; pass confirm_delete=True, or use 'someday' to defer reversibly."}
    try:
        return _assign_apply_guarded(task_id, disposition,
                                     instance=instance or _get_current_instance() or "")
    except AssignRefused as refused:
        return {"error": refused.code, "task_id": task_id, "disposition": disposition,
                "valid_dispositions": list(ASSIGN_DISPOSITIONS)}


def _task_query_impl(query: str = "", instance: str = "", days: int = 3, limit: int = 10, **kwargs) -> dict:
    """Dispatch to the appropriate query impl based on query preset name."""
    dispatch = {
        "overdue": lambda: _overdue_tasks_impl(instance=instance),
        "today": lambda: _due_today_impl(instance=instance),
        "week": lambda: _due_this_week_impl(instance=instance),
        "high_priority": lambda: _high_priority_tasks_impl(instance=instance),
        "urgent": lambda: _urgent_tasks_impl(instance=instance),
        "unscheduled": lambda: _unscheduled_tasks_impl(instance=instance),
        "upcoming": lambda: _upcoming_deadlines_impl(days=days, instance=instance),
        "focus": lambda: _focus_now_impl(instance=instance, limit=limit),
        "summary": lambda: _task_summary_impl(instance=instance),
    }
    handler = dispatch.get(query)
    if not handler:
        return {"error": f"Unknown query '{query}'. Valid: {', '.join(sorted(dispatch.keys()))}"}
    return handler()


@mcp.tool()
@mcp_tool_with_fallback
def task_query(
    query: str = Field(description="Query preset: 'overdue' (past due), 'today' (due today + overdue), 'week' (due in 7 days + overdue), 'high_priority' (priority >= 3), 'urgent' (priority >= 4), 'unscheduled' (no due date), 'upcoming' (due in N days, no overdue), 'focus' (priority >= 4 OR overdue — a flat high-priority list; for the open-ended 'what should I do today?' prefer today_actions, which clusters and scores), 'summary' (counts only, no task details)"),
    instance: str = Field(default="", description="Filter to specific instance (e.g., 'personal', 'business'). Empty = all instances."),
    days: int = Field(default=3, description="Days to look ahead (only used with 'upcoming' query, default 3)"),
    limit: int = Field(default=10, description="Max tasks to return (only used with 'focus' query, default 10, 0 = all)")
) -> dict:
    """
    FAST task queries — replaces scanning all tasks. Use this for:
    - "What's overdue?" → query='overdue'
    - "What's due today?" → query='today'
    - "Weekly overview" → query='week'
    - "Important tasks" → query='high_priority'
    - "What's urgent?" → query='urgent'
    - "Backlog / no due date" → query='unscheduled'
    - "What's coming up?" → query='upcoming' (with days=N)
    - "What should I work on?" → query='focus' (BEST default)
    - "Quick summary / how many?" → query='summary' (counts only)

    Priority scale: 0=none, 1-2=low, 3=medium, 4=high, 5=urgent.
    TIP: If user has multiple instances, ask which one first.
    """
    dispatch = {
        "overdue": lambda: _overdue_tasks_impl(instance=instance),
        "today": lambda: _due_today_impl(instance=instance),
        "week": lambda: _due_this_week_impl(instance=instance),
        "high_priority": lambda: _high_priority_tasks_impl(instance=instance),
        "urgent": lambda: _urgent_tasks_impl(instance=instance),
        "unscheduled": lambda: _unscheduled_tasks_impl(instance=instance),
        "upcoming": lambda: _upcoming_deadlines_impl(days=days, instance=instance),
        "focus": lambda: _focus_now_impl(instance=instance, limit=limit),
        "summary": lambda: _task_summary_impl(instance=instance),
    }
    handler = dispatch.get(query)
    if not handler:
        return {"error": f"Unknown query '{query}'. Valid: {', '.join(sorted(dispatch.keys()))}"}
    return handler()


@mcp.tool()
@mcp_tool_with_fallback
def instance_connect(
    name: str = Field(description="Name for this instance (e.g., 'personal', 'work')"),
    url: str = Field(description="Vikunja instance URL (e.g., 'https://vikunja.example.com')"),
    token: str = Field(description="API token from Vikunja Settings > API Tokens")
) -> dict:
    """
    Connect a new Vikunja instance. Use when user says:
    - "Connect to my Vikunja at..."
    - "Add my personal instance..."
    - "Connect to vikunja.example.com with token..."

    Validates the token before storing. Auto-switches to this instance if it's the first one.
    """
    return _connect_instance_impl(name, url, token)


@mcp.tool()
@mcp_tool_with_fallback
def ctx_set(
    instance: str = Field(default="", description="Default instance name (e.g., 'personal'). Empty to clear."),
    project_id: int = Field(default=0, description="Default project ID. 0 to clear.")
) -> dict:
    """
    Set default instance and/or project for subsequent queries.
    Persists across tool calls until cleared or changed.

    Use when user says:
    - "Focus on my personal instance"
    - "Switch to the Kitchen project"
    - "Only show me work tasks from now on"

    Clear with empty instance and project_id=0.
    """
    config = _load_config()
    if "mcp_context" not in config:
        config["mcp_context"] = {}

    if instance:
        # Validate instance exists
        instances = _get_instances()
        if instance not in instances:
            return {"error": f"Instance '{instance}' not found. Available: {list(instances.keys())}"}
        config["mcp_context"]["instance"] = instance
    elif "instance" in config["mcp_context"]:
        del config["mcp_context"]["instance"]

    if project_id:
        config["mcp_context"]["project_id"] = project_id
    elif "project_id" in config["mcp_context"]:
        del config["mcp_context"]["project_id"]

    _save_config(config)

    # Return current context (inline to avoid calling decorated function)
    mcp_context = config.get("mcp_context", {})
    instances = _get_instances()
    return {
        "instance": mcp_context.get("instance"),
        "project_id": mcp_context.get("project_id"),
        "available_instances": list(instances.keys()),
        "hint": "Use set_active_context to change defaults, or pass instance= to individual tools."
    }


def _build_share_button_blocks(response_text: str) -> list:
    """Build Slack blocks for ephemeral response (private to user).

    Used for channel @mentions to protect privacy - response shown only to user.
    Share button disabled until Slack Interactivity is configured (solutions-pm0v).
    """
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": response_text
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":lock: _Only visible to you_"
                }
            ]
        }
    ]
    return blocks


def _format_tasks_for_slack(result: dict, title: str) -> str:
    """Format task list result as Slack mrkdwn."""
    if "error" in result:
        return f":warning: Error: {result['error']}"

    tasks = result.get("tasks", [])
    if not tasks:
        return f":white_check_mark: {title}: No tasks found"

    lines = [f"*{title}* ({len(tasks)} tasks):\n"]
    for task in tasks[:20]:  # Limit to 20 for readability
        priority = task.get("priority", 0)
        priority_emoji = {5: ":rotating_light:", 4: ":red_circle:", 3: ":large_orange_circle:", 2: ":large_yellow_circle:"}.get(priority, "")
        instance = task.get("instance", "")
        instance_tag = f" [{instance}]" if instance and len(_get_instances()) > 1 else ""
        due = task.get("due_date", "")[:10] if task.get("due_date") else ""
        due_str = f" (due {due})" if due else ""
        lines.append(f"{priority_emoji} {task.get('title', 'Untitled')}{instance_tag}{due_str}")

    if len(tasks) > 20:
        lines.append(f"\n_...and {len(tasks) - 20} more_")

    # Add instance summary if multi-instance
    by_instance = result.get("by_instance", {})
    if len(by_instance) > 1:
        instance_summary = ", ".join(f"{name}: {count}" for name, count in by_instance.items())
        lines.append(f"\n_Instances: {instance_summary}_")

    return "\n".join(lines)


def _format_summary_for_slack(result: dict) -> str:
    """Format task summary as Slack mrkdwn."""
    if "error" in result:
        return f":warning: Error: {result['error']}"

    total = result.get("total", 0)
    overdue = result.get("overdue", 0)
    due_today = result.get("due_today", 0)
    due_this_week = result.get("due_this_week", 0)
    critical = result.get("critical", 0)
    urgent = result.get("urgent", 0)
    high_priority = result.get("high_priority", 0)
    unscheduled = result.get("unscheduled", 0)

    lines = [f"*Task Summary* ({total} total)\n"]

    # Time-based
    if overdue:
        lines.append(f":warning: Overdue: {overdue}")
    if due_today:
        lines.append(f":calendar: Due today: {due_today}")
    if due_this_week:
        lines.append(f":date: Due this week: {due_this_week}")

    # Priority-based
    if critical:
        lines.append(f":rotating_light: Critical (P5): {critical}")
    if urgent:
        lines.append(f":red_circle: Urgent (P4+): {urgent}")
    if high_priority:
        lines.append(f":large_orange_circle: High priority (P3+): {high_priority}")

    # Other
    if unscheduled:
        lines.append(f":grey_question: Unscheduled: {unscheduled}")

    # Instance breakdown
    by_instance = result.get("by_instance", {})
    if len(by_instance) > 1:
        instance_summary = ", ".join(f"{name}: {count}" for name, count in by_instance.items())
        lines.append(f"\n_Instances: {instance_summary}_")

    return "\n".join(lines)


def _format_instances_for_slack() -> str:
    """Format instance list for /instances slash command."""
    instances = _get_instances()
    current = _get_current_instance()

    if not instances:
        return ":x: No Vikunja instances configured."

    lines = [f"*Configured Instances* ({len(instances)}):\n"]
    for name, config in instances.items():
        url = config.get("url", "")
        is_current = " ← current" if name == current else ""
        lines.append(f"• *{name}*: {url}{is_current}")

    return "\n".join(lines)


def _format_help_for_slack(topic: str = "") -> str:
    """Format help message for /help slash command."""
    topic = topic.strip().lower()

    # Detailed help for specific commands
    command_help = {
        "overdue": (
            "*`/overdue`* - Tasks past their due date\n\n"
            "Shows all incomplete tasks where due date < now.\n"
            "Sorted by due date (oldest first), then priority."
        ),
        "today": (
            "*`/today`* - Tasks due today + overdue\n\n"
            "Shows tasks due today AND any overdue tasks.\n"
            "Best for daily planning - what needs attention NOW."
        ),
        "week": (
            "*`/week`* - Tasks due this week\n\n"
            "Shows tasks due in the next 7 days + overdue.\n"
            "Good for weekly planning and sprint reviews."
        ),
        "priority": (
            "*`/priority`* - High priority tasks (3+)\n\n"
            "Shows tasks with priority 3, 4, or 5.\n"
            "Vikunja priority scale: 0=none, 1-2=low, 3=medium, 4=high, 5=urgent"
        ),
        "urgent": (
            "*`/urgent`* - Urgent tasks (priority 4+)\n\n"
            "Shows only priority 4 and 5 tasks.\n"
            "For critical items that need immediate attention."
        ),
        "unscheduled": (
            "*`/unscheduled`* - Tasks without due date\n\n"
            "Shows tasks with no due date set.\n"
            "Useful for backlog review and scheduling floating tasks."
        ),
        "focus": (
            "*`/focus`* - What to work on now\n\n"
            "Shows: high priority (3+) OR due today/overdue.\n"
            "Combines urgency and importance for actionable view."
        ),
        "summary": (
            "*`/summary`* - Quick task counts\n\n"
            "Shows counts: overdue, due today, due this week, priority levels.\n"
            "Fastest overview - no task details, just numbers."
        ),
        "connections": (
            "*`/connections`* - Show connected Vikunja instances\n\n"
            "Lists all your Vikunja connections with URLs.\n"
            "All task commands query ALL connections in parallel."
        ),
        "project": (
            "*`/project`* - Set active project context\n\n"
            "*Usage:*\n"
            "• `/project` - Show current active project\n"
            "• `/project Kitchen` - Set active project (fuzzy match)\n"
            "• `/project Kitchen 2` - Select 2nd match if ambiguous\n"
            "• `/clear` - Clear active project\n\n"
            "_When set, slash commands show only tasks from that project._"
        ),
        "connect": (
            "*`/connect`* - Connect a Vikunja instance\n\n"
            "*Usage:* `/connect <name> <url> <token>`\n\n"
            "*Example:*\n"
            "`/connect personal vikunja.example.com abc123...`\n\n"
            "*Get your token:*\n"
            "1. Log into your Vikunja instance\n"
            "2. Go to Settings > API Tokens\n"
            "3. Create a new token and copy it here\n\n"
            "_Response is always private - your token is never shown._"
        ),
        "disconnect": (
            "*`/disconnect`* - Remove a Vikunja instance\n\n"
            "*Usage:* `/disconnect <name>`\n\n"
            "*Example:* `/disconnect personal`\n\n"
            "_Use `/connections` to see available instances._"
        ),
        "usage": (
            "*`/usage`* - Toggle usage/ECO footer\n\n"
            "*Usage:*\n"
            "• `/usage` - Toggle footer on/off\n"
            "• `/usage on` - Show footer\n"
            "• `/usage off` - Hide footer\n\n"
            "_The footer shows your ECO streak (consecutive slash commands)\n"
            "and estimated token savings vs LLM queries._"
        ),
    }

    if topic and topic in command_help:
        return command_help[topic]

    if topic:
        return f":warning: Unknown command: `{topic}`\n\nType `/help` for all commands."

    # General help
    instances = _get_instances()
    instance_note = f" across {len(instances)} instances" if len(instances) > 1 else ""

    return f"""*Factum Erit Commands*{instance_note}

*Task Filters* (no LLM cost, instant):
• `/overdue` - Tasks past due date
• `/today` - Due today + overdue
• `/week` - Due within 7 days
• `/priority` - Priority 3+ tasks
• `/urgent` - Priority 4+ (critical only)
• `/unscheduled` - No due date set
• `/focus` - High priority OR due today
• `/summary` - Quick counts only (fastest)

*Context*:
• `/project [name]` - Set active project for filtering
• `/clear` - Clear active project
• `/connections` - Show connected Vikunja instances

*Setup* (always private):
• `/connect <name> <url> <token>` - Add Vikunja instance
• `/disconnect <name>` - Remove instance
• `/usage [on|off]` - Toggle ECO footer

• `/help [command]` - Help for specific command

*Chat*: Message me naturally for complex queries!
_"What's overdue in the Kitchen project?"_
_"Create a task to buy groceries, due tomorrow"_"""


@mcp.tool()
@mcp_tool_with_fallback
def search_all_tasks(
    filter_due: str = Field(default="", description="Filter: 'today' (overdue+today), 'week' (overdue+week), 'overdue', or empty for all"),
    include_done: bool = Field(default=False, description="Include completed tasks"),
    filter: str = Field(default="", description="Additional Vikunja filter (e.g., 'priority >= 3')"),
    page: int = Field(default=0, description="Page number (0=all pages, 1+=specific page)"),
    allow_truncated: bool = Field(default=False, description="Allow truncated results without error"),
    due_after: str = Field(default="", description="Only tasks due on or after this date (ISO format: YYYY-MM-DD)"),
    due_before: str = Field(default="", description="Only tasks due before this date (ISO format: YYYY-MM-DD)")
) -> dict:
    """
    HEAVY tool for complex task queries. Use QUICK TOOLS FIRST for common queries:

    PREFER task_query() (faster, single tool for common queries):
    - task_query(query='overdue') → "What's overdue?"
    - task_query(query='today') → "What's due today?"
    - task_query(query='week') → "What's due this week?"
    - task_query(query='focus') → "What needs attention?"
    - task_query(query='high_priority') → "What's high priority?"
    - task_query(query='urgent') → "What's urgent/critical?"
    - task_query(query='unscheduled') → "What has no due date?"

    USE THIS TOOL ONLY FOR:
    - Custom date ranges: due_after="2025-12-19", due_before="2025-12-26"
    - Custom filters: filter="priority >= 3"
    - Including completed tasks: include_done=True
    - Pagination: page=1
    - Queries that need all tasks at once

    Filter meanings:
    - 'today': Overdue + due today
    - 'week': Overdue + due within 7 days
    - 'overdue': Strictly past due only

    Returns: {tasks: [{id, title, instance, project, due_date, priority, ...}], by_instance: {...}}
    """
    return _list_all_tasks_impl(
        filter_due=filter_due,
        include_done=include_done,
        filter=filter,
        page=page,
        allow_truncated=allow_truncated,
        due_after=due_after,
        due_before=due_before
    )


@mcp.tool()
@mcp_tool_with_fallback
def search_all(
    query: str = Field(description="Search term to find in task/project titles and descriptions"),
    filter: str = Field(default="", description="Additional Vikunja filter (e.g., '!done', 'priority >= 3')"),
    page: int = Field(default=0, description="Page number (0=all pages, 1+=specific page)"),
    allow_truncated: bool = Field(default=False, description="Allow truncated results without error")
) -> dict:
    """
    Search for tasks and projects across ALL configured Vikunja instances.

    Uses Vikunja's server-side filtering for efficient search.

    If results exceed limit (100), returns error with options:
    - Add filter to narrow results
    - Request specific page
    - Set allow_truncated=true to accept partial results

    Returns: {results: [{type, id, title, instance, ...}, ...], by_instance: {name: count}}
    """
    instances = _get_instances()
    all_results = []
    by_instance = {}
    query_lower = query.lower()
    per_page = 50  # Vikunja's actual page limit

    # Build server-side filter for tasks
    task_filter = f"title ~ \"{query}\" || description ~ \"{query}\""
    if filter:
        task_filter = f"({task_filter}) && ({filter})"

    # Fetch all pages unless specific page requested
    if page > 0:
        params = {"filter": task_filter, "per_page": per_page, "page": page}
        task_results = _fetch_from_all_instances("GET", "/api/v1/tasks", params=params)
    else:
        # Fetch ALL pages from ALL instances
        task_results = _fetch_all_pages_from_all_instances(
            "GET", "/api/v1/tasks",
            per_page=per_page,
            max_pages=100,
            params={"filter": task_filter}
        )

    # Projects - also paginated (fetch all pages)
    if page > 0:
        project_results = _fetch_from_all_instances("GET", "/api/v1/projects", params={"page": page})
    else:
        project_results = _fetch_all_pages_from_all_instances(
            "GET", "/api/v1/projects",
            per_page=per_page,
            max_pages=20  # Projects usually fewer than tasks
        )

    hit_limit = False
    for instance_name in instances.keys():
        instance_count = 0

        # Search projects (client-side filter)
        projects = project_results.get(instance_name, [])
        if not isinstance(projects, dict) or "error" not in projects:
            for project in projects:
                title = project.get("title", "")
                desc = project.get("description", "")
                if query_lower in title.lower() or query_lower in desc.lower():
                    instance_count += 1
                    all_results.append({
                        "type": "project",
                        "id": project.get("id"),
                        "title": title,
                        "instance": instance_name,
                        "match_in": "title" if query_lower in title.lower() else "description",
                    })

        # Tasks already filtered server-side
        tasks = task_results.get(instance_name, [])
        if not isinstance(tasks, dict) or "error" not in tasks:
            if len(tasks) >= per_page:
                hit_limit = True
            for task in tasks:
                instance_count += 1
                all_results.append({
                    "type": "task",
                    "id": task.get("id"),
                    "title": task.get("title"),
                    "instance": instance_name,
                    "project_id": task.get("project_id"),
                    "done": task.get("done", False),
                })

        by_instance[instance_name] = instance_count

    # Check if we hit the limit and user didn't explicitly allow truncation
    if hit_limit and not allow_truncated and page == 0:
        return {
            "error": "too_many_results",
            "count": len(all_results),
            "query": query,
            "message": f"Found {len(all_results)}+ results (limit: {per_page}/instance). Please narrow your search:",
            "options": [
                f"Add filter: search_all(query='{query}', filter='!done')",
                f"Request page: search_all(query='{query}', page=1)",
                f"Allow truncated: search_all(query='{query}', allow_truncated=true)"
            ],
            "by_instance": by_instance
        }

    return {
        "query": query,
        "filter": filter or None,
        "results": all_results,
        "total": len(all_results),
        "page": page if page > 0 else "all",
        "truncated": hit_limit,
        "by_instance": by_instance
    }


def _require_admin(instance: str) -> Optional[dict]:
    """Check if instance has admin privileges.

    Returns None if admin, or error dict if not.
    """
    config = _load_config()
    inst_config = config.get("instances", {}).get(instance, {})
    if not inst_config.get("admin", False):
        return {
            "error": "admin_required",
            "message": f"Instance '{instance}' does not have admin privileges",
            "hint": "Set 'admin: true' in config for this instance"
        }
    return None


def _invalidate_ics_cache(instance: Optional[str] = None):
    """Invalidate ICS cache for an instance or all instances.

    Called when tasks are created/updated/deleted to ensure
    calendar feeds reflect latest data.
    """
    global _ics_feed_cache
    if instance:
        # Clear only entries for this instance
        keys_to_remove = [k for k in _ics_feed_cache if k.startswith(f"{instance}:")]
        for k in keys_to_remove:
            del _ics_feed_cache[k]
        keys_to_remove = [k for k in _omnibus_cache if k[0] == instance]
        for k in keys_to_remove:
            del _omnibus_cache[k]
    else:
        # Clear all cached feeds
        _ics_feed_cache = {}
        _omnibus_cache.clear()


def _extract_event_location(description: str) -> str:
    """Extract a venue/address from an event description for a maps link, via the
    location-line convention: a line like ``Location: <address>`` (also Venue /
    Where / Address / 📍). HTML is stripped to text first (block/break tags become
    newlines so the line survives). Returns '' when no such line is present.

    Vikunja tasks have no native location field, so this lets an organizer opt a
    Vikunja event into the same Google-Maps link the external (Google/Outlook)
    events already render from their real location field.
    """
    if not description:
        return ""
    import re
    import html as _html
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", description, flags=re.I)
    text = re.sub(r"</\s*(?:p|div|li|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    for line in text.splitlines():
        m = re.match(r"\s*(?:(?:location|venue|where|address)\s*[:\-]|📍[:\-]?)\s*(.+\S)",
                     line, flags=re.I)
        if m:
            return m.group(1).strip()
    return ""


def _invalidate_project_colors_cache():
    """Clear the project-colors cache (call on project create/update/delete)."""
    global _project_colors_cache
    _project_colors_cache = {}


@mcp.custom_route("/move/{task_id}/{project_id}/{token}", methods=["GET"])
async def move_task_to_project(request: Request):
    """Move a task to a different project and redirect to Vikunja.

    URL: /move/{task_id}/{project_id}/{token}

    - task_id: The task ID to move
    - project_id: The target project ID
    - token: Security token derived from task_id + project_id + bot_token

    On success: Moves the task and redirects to the task in Vikunja.
    On error: Returns JSON error message.
    """
    from starlette.responses import RedirectResponse

    task_id_str = request.path_params.get("task_id", "")
    project_id_str = request.path_params.get("project_id", "")
    url_token = request.path_params.get("token", "")

    # Validate task_id and project_id
    try:
        task_id = int(task_id_str)
        project_id = int(project_id_str)
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "invalid_ids", "message": "Task ID and Project ID must be numbers"},
            status_code=400
        )

    # Validate token
    bot_token = os.environ.get("VIKUNJA_BOT_TOKEN", "")
    expected_token = hashlib.sha256(f"{task_id}:{project_id}:{bot_token}".encode()).hexdigest()[:12]
    # Constant-time compare — move token rides in the URL path (sea-ywwu).
    if not url_token or not hmac.compare_digest(url_token, expected_token):
        return JSONResponse(
            {"error": "invalid_token", "message": "Invalid or expired move token"},
            status_code=401
        )

    # Move the task
    from .vikunja_client import BotVikunjaClient, VikunjaAPIError

    try:
        client = BotVikunjaClient()

        # Get current task
        task = client.get_task(task_id)
        if not task:
            return JSONResponse(
                {"error": "task_not_found", "message": f"Task #{task_id} not found"},
                status_code=404
            )

        # Clean description: remove "Move to:" links after successful move
        description = task.get("description", "")
        if description and "📁" in description:
            import re
            # Remove the move links section (📁 Move to: ...)
            # Pattern: ---\n📁 **Move to:** ... to end of that line
            description = re.sub(r'\n*---\n*📁 \*?\*?Move to:\*?\*?[^\n]*\n*', '', description)
            # Also remove standalone move links without ---
            description = re.sub(r'\n*📁 \*?\*?Move to:\*?\*?[^\n]*\n*', '', description)
            description = description.rstrip()

        # Update task with new project_id and cleaned description
        task["project_id"] = project_id
        client.update_task(task_id, project_id=project_id, description=description)

        logger.info(f"Moved task #{task_id} to project #{project_id}")

    except VikunjaAPIError as e:
        return JSONResponse(
            {"error": "move_failed", "message": f"Could not move task: {e}"},
            status_code=500
        )

    # Redirect to the task in Vikunja
    vikunja_url = os.environ.get("VIKUNJA_URL", "https://vikunja.factumerit.app")
    redirect_url = f"{vikunja_url}/tasks/{task_id}"

    return RedirectResponse(url=redirect_url, status_code=302)


# ---------------------------------------------------------------------------
# Public entry point.
#
# Appended verbatim to the generated package by scripts/extract_public.py. It is
# NOT extracted from server.py, because the private main() is not a stdio entry
# point that happens to carry extras — it is a Factumerit server boot: it grants
# roles from ADMIN_USER_IDS, applies Alembic revisions against DATABASE_URL,
# re-seals Google refresh tokens, mounts OAuth middleware and the @eis poller.
# None of that belongs in a package someone installs to talk to their own
# Vikunja, and every piece of it reaches for a name the extraction does not
# publish.
#
# So the public package gets its own main: parse a transport, run the server.
# ---------------------------------------------------------------------------


def main():
    """Run the Vikunja MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Vikunja MCP Server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "http"],
                        help="Transport protocol (default: stdio)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host for sse/http transport (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000,
                        help="Port for sse/http transport (default: 8000)")
    args = parser.parse_args()

    if args.transport == "stdio":
        # stdio is the Claude Desktop path: the client owns the process and the
        # protocol owns stdout, so nothing may be printed to it.
        mcp.run(show_banner=False)
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port,
                show_banner=False)


if __name__ == "__main__":
    main()
