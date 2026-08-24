"""Pure deferral-state logic (spec 07 / fa-fhtl), extracted from ``server.py`` so it can
be mutation-tested in isolation.

No I/O, no Vikunja calls — string/dict in, string/dict out. The storage is a
``<!-- defer-meta: {...} -->`` marker embedded in a task description (it survives task
completion, which the eval-corpus requirement demands); this module reads and writes
that marker **surgically**, leaving every other byte — visible content, the smart-task
``eis-meta`` marker — untouched, so the notification poller and the defer writer never
clobber each other.
"""
import json
import re

# The description-embedded marker holding a task's deferral state. Independent of the
# smart-task eis-meta marker — a task may carry both; this pattern only matches its own.
_DEFER_META_PATTERN = re.compile(r"<!--\s*defer-meta:\s*(\{.*?\})\s*-->", re.DOTALL)


# @PUBLIC_HELPER
def _extract_defer_meta(description) -> dict:
    """Parse the deferral-state blob from a task description's
    ``<!-- defer-meta: {...} -->`` marker (fa-fhtl / spec 07), or {} if absent or
    malformed. Independent of the smart-task eis-meta machinery — a task may carry
    both markers; this reader only ever matches its own. Never raises."""
    if not description:
        return {}
    m = _DEFER_META_PATTERN.search(description)
    if not m:
        return {}
    try:
        val = json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


# @PUBLIC_HELPER
def _write_defer_meta(description, meta: dict) -> str:
    """Return ``description`` with its defer-meta marker replaced (or appended when
    absent). SURGICAL: every other byte — visible content, smart-task frontmatter,
    eis-meta — is left untouched, so the notification poller and the defer writer
    never clobber each other (spec 07 storage hazard #1). A falsy ``meta`` removes
    the marker entirely."""
    description = description or ""
    if not meta:
        # Surgical clear: a description with no marker is returned UNTOUCHED (don't
        # reformat what we didn't write). When a marker is present, remove it and the
        # single trailing newline the writer put before it — preserving the body's own
        # (possibly leading/indented) whitespace, not str.strip()'ing the whole thing.
        if not _DEFER_META_PATTERN.search(description):
            return description
        return _DEFER_META_PATTERN.sub("", description).rstrip()
    marker = "<!-- defer-meta: " + json.dumps(meta, separators=(",", ":"), sort_keys=True) + " -->"
    if _DEFER_META_PATTERN.search(description):
        # function replacement: JSON payload must not be read as regex backrefs
        return _DEFER_META_PATTERN.sub(lambda _m: marker, description, count=1)
    if description.strip():
        return description.rstrip() + "\n" + marker
    return marker


# @PUBLIC_HELPER
def _task_defer_state(task: dict) -> dict:
    """The task's deferral state (``deferred_until`` / ``defer_reason`` /
    ``defer_count`` / ``defer_history`` / ``wake_trigger``), or {} if none. Reads the
    projected ``defer`` dict (from include_meta) when present, else parses the raw
    description — so both a scored candidate and a directly-fetched task work."""
    if isinstance(task.get("defer"), dict):
        return task["defer"]
    return _extract_defer_meta(task.get("description"))


# @PUBLIC_HELPER
def _defer_count(task: dict) -> int:
    """Non-negative integer ``defer_count`` from the task's defer state; 0 when
    absent or malformed. A JSON bool never counts as a number."""
    c = _task_defer_state(task).get("defer_count")
    return c if isinstance(c, int) and not isinstance(c, bool) and c > 0 else 0
