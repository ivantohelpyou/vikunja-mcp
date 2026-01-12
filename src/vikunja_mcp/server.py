"""
Vikunja MCP Server

MCP server that gives Claude full access to your Vikunja task management.
Supports multiple instances, X-Q handoff queues, and power query tools.

Configuration (in order of priority):
1. Config file: ~/.vikunja-mcp/config.yaml
2. Environment: VIKUNJA_INSTANCES (JSON) or VIKUNJA_URL + VIKUNJA_TOKEN

Multi-instance config example (env var):
    VIKUNJA_INSTANCES='{"personal":{"url":"https://vikunja.example.com","token":"tk_xxx"}}'

Source: https://github.com/ivantohelpyou/vikunja-mcp
PyPI: https://pypi.org/project/vikunja-mcp/
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from fastmcp import FastMCP
from pydantic import Field

# ============================================================================
# CONFIGURATION
# ============================================================================

CONFIG_DIR = Path.home() / ".vikunja-mcp"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

logger = logging.getLogger("vikunja-mcp")
logger.setLevel(logging.DEBUG if os.environ.get("VIKUNJA_DEBUG") else logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)

# ============================================================================
# MCP SERVER
# ============================================================================

mcp = FastMCP(
    "Vikunja MCP",
    instructions="""Manage tasks, projects, labels, and kanban boards in Vikunja.

Multi-instance support: Use list_instances() to see configured instances,
switch_instance() to change, or pass instance= to individual tools.

Quick tools for common queries:
- focus_now() - Tasks needing immediate attention
- due_today() - Today's tasks + overdue
- task_summary() - Counts only, very fast

X-Q (Exchange Queue) for agent handoffs:
- check_xq() - See pending handoff items
- claim_xq_task() / complete_xq_task() - Process handoffs
"""
)

# ============================================================================
# CONFIG FILE MANAGEMENT
# ============================================================================

def _load_config() -> dict:
    """Load config from YAML file."""
    if not CONFIG_FILE.exists():
        return {"instances": {}, "current_instance": None, "xq": {}, "mcp_context": {}}
    try:
        with open(CONFIG_FILE, "r") as f:
            config = yaml.safe_load(f) or {}
            for key in ["instances", "xq", "mcp_context"]:
                if key not in config:
                    config[key] = {}
            return config
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed config file: {e}")


def _save_config(config: dict) -> None:
    """Save config to YAML file (atomic write)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, CONFIG_FILE)
    except Exception:
        os.unlink(temp_path)
        raise


# ============================================================================
# INSTANCE MANAGEMENT
# ============================================================================

def _get_instances() -> dict:
    """Get all configured Vikunja instances.

    Priority:
    1. Config file (~/.vikunja-mcp/config.yaml)
    2. VIKUNJA_INSTANCES env var (JSON object: {"name": {"url": "...", "token": "..."}})
    3. VIKUNJA_URL/VIKUNJA_TOKEN env vars as 'default'
    """
    config = _load_config()
    instances = dict(config.get("instances", {}))

    # Parse VIKUNJA_INSTANCES env var
    # Supports both formats:
    #   Array: [{"name": "foo", "url": "...", "token": "..."}]
    #   Object: {"foo": {"url": "...", "token": "..."}}
    instances_json = os.environ.get("VIKUNJA_INSTANCES", "")
    if instances_json:
        try:
            env_instances = json.loads(instances_json)
            if isinstance(env_instances, list):
                # Array format: [{"name": "...", "url": "...", "token": "..."}]
                for inst in env_instances:
                    name = inst.get("name", "")
                    if name and name not in instances:
                        instances[name] = {
                            "url": inst.get("url", "").rstrip("/"),
                            "token": inst.get("token", "")
                        }
            elif isinstance(env_instances, dict):
                # Object format: {"name": {"url": "...", "token": "..."}}
                for name, inst in env_instances.items():
                    if name not in instances:  # Config file takes precedence
                        instances[name] = {
                            "url": inst.get("url", "").rstrip("/"),
                            "token": inst.get("token", "")
                        }
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    # Fallback to VIKUNJA_URL/VIKUNJA_TOKEN as 'default'
    env_url = os.environ.get("VIKUNJA_URL")
    env_token = os.environ.get("VIKUNJA_TOKEN")
    if env_url and env_token and "default" not in instances:
        instances["default"] = {
            "url": env_url.rstrip("/"),
            "token": env_token
        }

    return instances


def _get_current_instance() -> Optional[str]:
    """Get name of currently active instance."""
    config = _load_config()

    # Check mcp_context first (set by set_active_context)
    mcp_instance = (config.get("mcp_context") or {}).get("instance")
    if mcp_instance:
        return mcp_instance

    # Then current_instance
    current = config.get("current_instance")
    if current:
        return current

    # Fall back to first available
    instances = _get_instances()
    if instances:
        if "default" in instances:
            return "default"
        return next(iter(instances.keys()))

    return None


def _set_current_instance(name: str) -> None:
    """Set the currently active instance."""
    instances = _get_instances()
    if name not in instances:
        available = ", ".join(instances.keys()) if instances else "none"
        raise ValueError(f"Instance '{name}' not found. Available: {available}")

    config = _load_config()
    config["current_instance"] = name
    if "mcp_context" not in config:
        config["mcp_context"] = {}
    config["mcp_context"]["instance"] = name
    _save_config(config)


def _get_instance_config(name: Optional[str] = None) -> tuple[str, str]:
    """Get URL and token for an instance."""
    if name is None:
        name = _get_current_instance()

    if name is None:
        url = os.environ.get("VIKUNJA_URL")
        token = os.environ.get("VIKUNJA_TOKEN")
        if url and token:
            return url.rstrip("/"), token
        raise ValueError("No instance configured. Set VIKUNJA_URL/VIKUNJA_TOKEN or configure instances.")

    instances = _get_instances()
    if name not in instances:
        raise ValueError(f"Instance '{name}' not found")

    instance = instances[name]
    url = instance.get("url")
    token = instance.get("token")

    # Support env var references: ${VAR_NAME}
    if token and token.startswith("${") and token.endswith("}"):
        env_var = token[2:-1]
        token = os.environ.get(env_var)
        if not token:
            raise ValueError(f"Environment variable {env_var} not set for instance '{name}'")

    if not url or not token:
        raise ValueError(f"Instance '{name}' missing url or token")

    return url.rstrip("/"), token


# ============================================================================
# VIKUNJA API CLIENT
# ============================================================================

def _request(method: str, endpoint: str, instance: Optional[str] = None, **kwargs) -> dict:
    """Make authenticated request to Vikunja API."""
    url, token = _get_instance_config(instance)

    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"

    full_url = f"{url}/api/v1{endpoint}" if endpoint.startswith("/") else f"{url}/api/v1/{endpoint}"

    response = requests.request(method, full_url, headers=headers, **kwargs)

    if response.status_code >= 400:
        try:
            error = response.json()
            msg = error.get("message", response.text)
        except Exception:
            msg = response.text
        raise Exception(f"Vikunja API error ({response.status_code}): {msg}")

    if response.status_code == 204:
        return {}

    return response.json()


def _request_all_instances(method: str, endpoint: str, filter_instance: str = "", **kwargs) -> list:
    """Make request to all instances (or one if filter specified)."""
    instances = _get_instances()
    if filter_instance:
        instances = {k: v for k, v in instances.items() if k == filter_instance}

    results = []
    for name in instances:
        try:
            data = _request(method, endpoint, instance=name, **kwargs)
            if isinstance(data, list):
                for item in data:
                    item["_instance"] = name
                results.extend(data)
            else:
                data["_instance"] = name
                results.append(data)
        except Exception as e:
            logger.warning(f"Request to {name} failed: {e}")

    return results


# ============================================================================
# INSTANCE MANAGEMENT TOOLS
# ============================================================================

@mcp.tool()
def list_instances() -> dict:
    """List all configured Vikunja instances.

    Returns instances with URLs and current selection.
    """
    instances = _get_instances()
    current = _get_current_instance()

    return {
        "instances": [
            {"name": name, "url": inst["url"], "is_current": name == current}
            for name, inst in instances.items()
        ],
        "current": current
    }


@mcp.tool()
def switch_instance(
    name: str = Field(description="Instance name to switch to")
) -> dict:
    """Switch to a different Vikunja instance.

    All subsequent operations will use this instance.
    """
    _set_current_instance(name)
    instances = _get_instances()
    return {
        "switched_to": name,
        "url": instances[name]["url"]
    }


@mcp.tool()
def connect_instance(
    name: str = Field(description="Name for this instance (e.g., 'personal', 'work')"),
    url: str = Field(description="Vikunja instance URL"),
    token: str = Field(description="API token from Vikunja Settings > API Tokens")
) -> dict:
    """Connect a new Vikunja instance.

    Validates the token before storing.
    """
    # Validate by making a test request
    test_url = f"{url.rstrip('/')}/api/v1/user"
    try:
        resp = requests.get(test_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if resp.status_code != 200:
            return {"error": f"Token validation failed: {resp.status_code}"}
        user_info = resp.json()
    except Exception as e:
        return {"error": f"Connection failed: {e}"}

    # Save to config
    config = _load_config()
    config["instances"][name] = {"url": url.rstrip("/"), "token": token}

    # Auto-switch if first instance
    if len(config["instances"]) == 1:
        config["current_instance"] = name

    _save_config(config)

    return {
        "connected": name,
        "url": url,
        "user": user_info.get("username"),
        "is_current": config.get("current_instance") == name
    }


@mcp.tool()
def get_context() -> dict:
    """Get the current Vikunja instance context."""
    current = _get_current_instance()
    instances = _get_instances()
    url = instances.get(current, {}).get("url", "") if current else ""
    return {"instance": current, "url": url}


@mcp.tool()
def set_active_context(
    instance: str = Field(default="", description="Default instance. Empty to clear."),
    project_id: int = Field(default=0, description="Default project ID. 0 to clear.")
) -> dict:
    """Set default instance and/or project for queries."""
    config = _load_config()
    if "mcp_context" not in config:
        config["mcp_context"] = {}

    if instance:
        instances = _get_instances()
        if instance not in instances:
            return {"error": f"Instance '{instance}' not found"}
        config["mcp_context"]["instance"] = instance
    elif "instance" in config["mcp_context"]:
        del config["mcp_context"]["instance"]

    if project_id:
        config["mcp_context"]["project_id"] = project_id
    elif "project_id" in config["mcp_context"]:
        del config["mcp_context"]["project_id"]

    _save_config(config)

    return {
        "instance": config["mcp_context"].get("instance"),
        "project_id": config["mcp_context"].get("project_id"),
        "available_instances": list(_get_instances().keys())
    }


@mcp.tool()
def get_active_context() -> dict:
    """Get current default instance and project context."""
    config = _load_config()
    mcp_context = config.get("mcp_context", {})
    return {
        "instance": mcp_context.get("instance"),
        "project_id": mcp_context.get("project_id"),
        "available_instances": list(_get_instances().keys())
    }


# ============================================================================
# PROJECT TOOLS
# ============================================================================

@mcp.tool()
def list_projects() -> dict:
    """List all Vikunja projects."""
    projects = _request("GET", "/projects")
    return {"result": [
        {
            "id": p["id"],
            "title": p.get("title", ""),
            "description": p.get("description", ""),
            "parent_project_id": p.get("parent_project_id", 0),
            "hex_color": p.get("hex_color", ""),
            "is_favorite": p.get("is_favorite", False),
            "position": p.get("position", 0)
        }
        for p in projects
    ]}


@mcp.tool()
def get_project(project_id: int = Field(description="Project ID")) -> dict:
    """Get details of a specific project."""
    p = _request("GET", f"/projects/{project_id}")
    return {
        "id": p["id"],
        "title": p.get("title", ""),
        "description": p.get("description", ""),
        "hex_color": p.get("hex_color", ""),
        "parent_project_id": p.get("parent_project_id", 0)
    }


@mcp.tool()
def create_project(
    title: str = Field(description="Project title"),
    description: str = Field(default="", description="Project description"),
    hex_color: str = Field(default="", description="Color in hex format"),
    parent_project_id: int = Field(default=0, description="Parent project ID for nesting")
) -> dict:
    """Create a new project."""
    data = {"title": title}
    if description:
        data["description"] = description
    if hex_color:
        data["hex_color"] = hex_color
    if parent_project_id:
        data["parent_project_id"] = parent_project_id

    project = _request("PUT", "/projects", json=data)
    return {
        "id": project["id"],
        "title": project.get("title"),
        "message": f"Created project '{title}'"
    }


@mcp.tool()
def update_project(
    project_id: int = Field(description="Project ID"),
    title: str = Field(default="", description="New title"),
    description: str = Field(default="", description="New description"),
    hex_color: str = Field(default="", description="New color"),
    parent_project_id: int = Field(default=-1, description="New parent (-1=keep, 0=root, >0=reparent)"),
    position: float = Field(default=-1, description="Position for ordering")
) -> dict:
    """Update a project's properties."""
    # Get current project first
    current = _request("GET", f"/projects/{project_id}")

    data = {"id": project_id}
    data["title"] = title if title else current.get("title", "")
    if description:
        data["description"] = description
    if hex_color:
        data["hex_color"] = hex_color
    if parent_project_id >= 0:
        data["parent_project_id"] = parent_project_id
    if position >= 0:
        data["position"] = position

    project = _request("POST", f"/projects/{project_id}", json=data)
    return {"id": project["id"], "title": project.get("title"), "updated": True}


@mcp.tool()
def delete_project(project_id: int = Field(description="Project ID")) -> dict:
    """Delete a project and all its tasks. WARNING: Permanent!"""
    _request("DELETE", f"/projects/{project_id}")
    return {"deleted": project_id}


# ============================================================================
# TASK TOOLS
# ============================================================================

@mcp.tool()
def list_tasks(
    project_id: int = Field(description="Project ID"),
    include_completed: bool = Field(default=False, description="Include completed tasks"),
    label_filter: str = Field(default="", description="Filter by label name")
) -> dict:
    """List tasks in a project."""
    tasks = _request("GET", f"/projects/{project_id}/tasks")

    if not include_completed:
        tasks = [t for t in tasks if not t.get("done")]

    if label_filter:
        label_lower = label_filter.lower()
        tasks = [t for t in tasks if any(
            label_lower in (l.get("title") or "").lower()
            for l in t.get("labels") or []
        )]

    return {"tasks": [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "description": t.get("description", "")[:200] if t.get("description") else "",
            "done": t.get("done", False),
            "priority": t.get("priority", 0),
            "due_date": t.get("due_date"),
            "labels": [l.get("title") for l in t.get("labels") or []],
            "project_id": t.get("project_id")
        }
        for t in tasks
    ]}


@mcp.tool()
def get_task(task_id: int = Field(description="Task ID")) -> dict:
    """Get details of a specific task."""
    t = _request("GET", f"/tasks/{task_id}")
    return {
        "id": t["id"],
        "title": t.get("title", ""),
        "description": t.get("description", ""),
        "done": t.get("done", False),
        "priority": t.get("priority", 0),
        "due_date": t.get("due_date"),
        "start_date": t.get("start_date"),
        "end_date": t.get("end_date"),
        "labels": [{"id": l["id"], "title": l.get("title")} for l in t.get("labels") or []],
        "project_id": t.get("project_id"),
        "bucket_id": t.get("bucket_id")
    }


@mcp.tool()
def create_task(
    project_id: int = Field(description="Project ID"),
    title: str = Field(description="Task title"),
    description: str = Field(default="", description="Task description"),
    due_date: str = Field(default="", description="Due date (ISO format)"),
    start_date: str = Field(default="", description="Start date for GANTT"),
    end_date: str = Field(default="", description="End date for GANTT"),
    priority: int = Field(default=0, description="Priority 0-5"),
    repeat_after: int = Field(default=0, description="Repeat interval in seconds"),
    repeat_mode: int = Field(default=0, description="0=from due date, 1=from completion")
) -> dict:
    """Create a new task."""
    data = {"title": title}
    if description:
        data["description"] = description
    if due_date:
        data["due_date"] = due_date if "T" in due_date else f"{due_date}T00:00:00Z"
    if start_date:
        data["start_date"] = start_date if "T" in start_date else f"{start_date}T00:00:00Z"
    if end_date:
        data["end_date"] = end_date if "T" in end_date else f"{end_date}T23:59:00Z"
    if priority:
        data["priority"] = priority
    if repeat_after:
        data["repeat_after"] = repeat_after
        data["repeat_mode"] = repeat_mode

    task = _request("PUT", f"/projects/{project_id}/tasks", json=data)
    return {"id": task["id"], "title": task.get("title"), "project_id": project_id}


@mcp.tool()
def update_task(
    task_id: int = Field(description="Task ID"),
    title: str = Field(default="", description="New title"),
    description: str = Field(default="", description="New description"),
    due_date: str = Field(default="", description="New due date"),
    priority: int = Field(default=-1, description="New priority (-1=keep)"),
    start_date: str = Field(default="", description="New start date"),
    end_date: str = Field(default="", description="New end date"),
    repeat_after: int = Field(default=-1, description="Repeat interval (-1=keep, 0=disable)"),
    repeat_mode: int = Field(default=-1, description="Repeat mode")
) -> dict:
    """Update an existing task."""
    data = {}
    if title:
        data["title"] = title
    if description:
        data["description"] = description
    if due_date:
        data["due_date"] = due_date if "T" in due_date else f"{due_date}T00:00:00Z"
    if start_date:
        data["start_date"] = start_date if "T" in start_date else f"{start_date}T00:00:00Z"
    if end_date:
        data["end_date"] = end_date if "T" in end_date else f"{end_date}T23:59:00Z"
    if priority >= 0:
        data["priority"] = priority
    if repeat_after >= 0:
        data["repeat_after"] = repeat_after
    if repeat_mode >= 0:
        data["repeat_mode"] = repeat_mode

    if not data:
        return {"error": "No changes specified"}

    task = _request("POST", f"/tasks/{task_id}", json=data)
    return {"id": task["id"], "title": task.get("title"), "updated": True}


@mcp.tool()
def complete_task(task_id: int = Field(description="Task ID")) -> dict:
    """Mark a task as complete."""
    task = _request("POST", f"/tasks/{task_id}", json={"done": True})
    return {"id": task_id, "title": task.get("title"), "done": True}


@mcp.tool()
def delete_task(task_id: int = Field(description="Task ID")) -> dict:
    """Delete a task. Permanent!"""
    _request("DELETE", f"/tasks/{task_id}")
    return {"deleted": task_id}


# ============================================================================
# LABEL TOOLS
# ============================================================================

@mcp.tool()
def list_labels() -> dict:
    """List all labels."""
    labels = _request("GET", "/labels")
    return {"labels": [
        {"id": l["id"], "title": l.get("title", ""), "hex_color": l.get("hex_color", "")}
        for l in labels
    ]}


@mcp.tool()
def create_label(
    title: str = Field(description="Label title"),
    hex_color: str = Field(description="Color in hex format (e.g., '#FF0000')")
) -> dict:
    """Create a new label."""
    label = _request("PUT", "/labels", json={
        "title": title,
        "hex_color": hex_color.lstrip("#") if hex_color else ""
    })
    return {"id": label["id"], "title": label.get("title")}


@mcp.tool()
def delete_label(label_id: int = Field(description="Label ID")) -> dict:
    """Delete a label."""
    _request("DELETE", f"/labels/{label_id}")
    return {"deleted": label_id}


@mcp.tool()
def add_label_to_task(
    task_id: int = Field(description="Task ID"),
    label_id: int = Field(description="Label ID")
) -> dict:
    """Add a label to a task."""
    _request("PUT", f"/tasks/{task_id}/labels", json={"label_id": label_id})
    return {"task_id": task_id, "label_id": label_id, "added": True}


# ============================================================================
# KANBAN / VIEW TOOLS
# ============================================================================

@mcp.tool()
def list_views(project_id: int = Field(description="Project ID")) -> dict:
    """List all views for a project."""
    views = _request("GET", f"/projects/{project_id}/views")
    return {"views": [
        {"id": v["id"], "title": v.get("title", ""), "view_kind": v.get("view_kind", "")}
        for v in views
    ]}


@mcp.tool()
def get_kanban_view(project_id: int = Field(description="Project ID")) -> dict:
    """Get the kanban view for a project."""
    views = _request("GET", f"/projects/{project_id}/views")
    for v in views:
        if v.get("view_kind") == "kanban":
            return {"view_id": v["id"], "title": v.get("title", "Kanban")}
    return {"error": "No kanban view found"}


@mcp.tool()
def list_buckets(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="Kanban view ID")
) -> dict:
    """List kanban buckets in a view."""
    buckets = _request("GET", f"/projects/{project_id}/views/{view_id}/buckets")
    return {"buckets": [
        {
            "id": b["id"],
            "title": b.get("title", ""),
            "position": b.get("position", 0),
            "limit": b.get("limit", 0),
            "task_count": len(b.get("tasks") or [])
        }
        for b in buckets
    ]}


@mcp.tool()
def create_bucket(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="Kanban view ID"),
    title: str = Field(description="Bucket title"),
    limit: int = Field(default=0, description="WIP limit (0=no limit)"),
    position: int = Field(default=0, description="Position")
) -> dict:
    """Create a kanban bucket."""
    data = {"title": title}
    if limit:
        data["limit"] = limit
    if position:
        data["position"] = position

    bucket = _request("PUT", f"/projects/{project_id}/views/{view_id}/buckets", json=data)
    return {"id": bucket["id"], "title": bucket.get("title")}


@mcp.tool()
def set_task_position(
    task_id: int = Field(description="Task ID"),
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="Kanban view ID"),
    bucket_id: int = Field(description="Target bucket ID"),
    apply_sort: bool = Field(default=False, description="Auto-position by sort strategy")
) -> dict:
    """Move a task to a kanban bucket."""
    data = {
        "task_id": task_id,
        "bucket_id": bucket_id,
        "project_view_id": view_id,
        "project_id": project_id
    }
    _request("POST", f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks", json=data)
    return {"task_id": task_id, "bucket_id": bucket_id, "moved": True}


# ============================================================================
# TASK RELATIONS
# ============================================================================

@mcp.tool()
def create_task_relation(
    task_id: int = Field(description="Source task ID"),
    relation_kind: str = Field(description="Relation type: subtask, parenttask, related, blocking, blocked, etc."),
    other_task_id: int = Field(description="Target task ID")
) -> dict:
    """Create a relation between tasks."""
    _request("PUT", f"/tasks/{task_id}/relations", json={
        "other_task_id": other_task_id,
        "relation_kind": relation_kind
    })
    return {"task_id": task_id, "relation_kind": relation_kind, "other_task_id": other_task_id}


@mcp.tool()
def list_task_relations(task_id: int = Field(description="Task ID")) -> dict:
    """List relations for a task."""
    task = _request("GET", f"/tasks/{task_id}")
    relations = task.get("related_tasks", {})
    return {"task_id": task_id, "relations": relations}


# ============================================================================
# POWER QUERY TOOLS - Fast task queries across instances
# ============================================================================

def _get_all_tasks(instance: str = "") -> list:
    """Get all incomplete tasks from all projects."""
    instances = _get_instances()
    if instance:
        instances = {k: v for k, v in instances.items() if k == instance}

    all_tasks = []
    for inst_name in instances:
        try:
            projects = _request("GET", "/projects", instance=inst_name)
            for proj in projects:
                try:
                    tasks = _request("GET", f"/projects/{proj['id']}/tasks", instance=inst_name)
                    for t in tasks:
                        if not t.get("done"):
                            t["_instance"] = inst_name
                            t["_project_title"] = proj.get("title", "")
                            all_tasks.append(t)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to get tasks from {inst_name}: {e}")

    return all_tasks


def _parse_due_date(task: dict) -> Optional[datetime]:
    """Parse due_date from task."""
    due = task.get("due_date")
    if not due or due == "0001-01-01T00:00:00Z":
        return None
    try:
        return datetime.fromisoformat(due.replace("Z", "+00:00"))
    except Exception:
        return None


@mcp.tool()
def overdue_tasks(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Get tasks past their due date. FAST."""
    now = datetime.now(timezone.utc)
    tasks = _get_all_tasks(instance)

    overdue = []
    for t in tasks:
        due = _parse_due_date(t)
        if due and due < now:
            overdue.append({
                "id": t["id"],
                "title": t.get("title", ""),
                "due_date": t.get("due_date"),
                "priority": t.get("priority", 0),
                "project": t.get("_project_title", ""),
                "instance": t.get("_instance", "")
            })

    overdue.sort(key=lambda x: x.get("due_date", ""))
    return {"tasks": overdue, "count": len(overdue)}


@mcp.tool()
def due_today(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Get tasks due today + overdue. FAST."""
    now = datetime.now(timezone.utc)
    today_end = now.replace(hour=23, minute=59, second=59)
    tasks = _get_all_tasks(instance)

    due = []
    for t in tasks:
        d = _parse_due_date(t)
        if d and d <= today_end:
            due.append({
                "id": t["id"],
                "title": t.get("title", ""),
                "due_date": t.get("due_date"),
                "priority": t.get("priority", 0),
                "project": t.get("_project_title", ""),
                "instance": t.get("_instance", ""),
                "overdue": d < now
            })

    due.sort(key=lambda x: (-x.get("priority", 0), x.get("due_date", "")))
    return {"tasks": due, "count": len(due)}


@mcp.tool()
def due_this_week(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Get tasks due in next 7 days + overdue. FAST."""
    now = datetime.now(timezone.utc)
    week_end = now + timedelta(days=7)
    tasks = _get_all_tasks(instance)

    due = []
    for t in tasks:
        d = _parse_due_date(t)
        if d and d <= week_end:
            due.append({
                "id": t["id"],
                "title": t.get("title", ""),
                "due_date": t.get("due_date"),
                "priority": t.get("priority", 0),
                "project": t.get("_project_title", ""),
                "instance": t.get("_instance", "")
            })

    due.sort(key=lambda x: x.get("due_date", ""))
    return {"tasks": due, "count": len(due)}


@mcp.tool()
def high_priority_tasks(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Get tasks with priority >= 3. FAST."""
    tasks = _get_all_tasks(instance)

    high = [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "priority": t.get("priority", 0),
            "due_date": t.get("due_date"),
            "project": t.get("_project_title", ""),
            "instance": t.get("_instance", "")
        }
        for t in tasks if t.get("priority", 0) >= 3
    ]

    high.sort(key=lambda x: -x.get("priority", 0))
    return {"tasks": high, "count": len(high)}


@mcp.tool()
def urgent_tasks(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Get tasks with priority >= 4 (critical). FAST."""
    tasks = _get_all_tasks(instance)

    urgent = [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "priority": t.get("priority", 0),
            "due_date": t.get("due_date"),
            "project": t.get("_project_title", ""),
            "instance": t.get("_instance", "")
        }
        for t in tasks if t.get("priority", 0) >= 4
    ]

    urgent.sort(key=lambda x: -x.get("priority", 0))
    return {"tasks": urgent, "count": len(urgent)}


@mcp.tool()
def focus_now(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all."),
    limit: int = Field(default=10, description="Max tasks (0=all)")
) -> dict:
    """Get tasks needing attention: priority >= 4 OR overdue. THE BEST for 'what should I work on?'"""
    now = datetime.now(timezone.utc)
    tasks = _get_all_tasks(instance)

    focus = []
    for t in tasks:
        due = _parse_due_date(t)
        is_overdue = due and due < now
        is_high_priority = t.get("priority", 0) >= 4

        if is_overdue or is_high_priority:
            focus.append({
                "id": t["id"],
                "title": t.get("title", ""),
                "priority": t.get("priority", 0),
                "due_date": t.get("due_date"),
                "overdue": is_overdue,
                "project": t.get("_project_title", ""),
                "instance": t.get("_instance", "")
            })

    focus.sort(key=lambda x: (-x.get("priority", 0), x.get("due_date") or "9999"))

    total = len(focus)
    if limit > 0:
        focus = focus[:limit]

    return {"tasks": focus, "count": len(focus), "total_matching": total}


@mcp.tool()
def task_summary(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Lightweight task overview - COUNTS ONLY. FASTEST."""
    now = datetime.now(timezone.utc)
    today_end = now.replace(hour=23, minute=59, second=59)
    week_end = now + timedelta(days=7)

    tasks = _get_all_tasks(instance)

    counts = {
        "total": len(tasks),
        "overdue": 0,
        "due_today": 0,
        "due_this_week": 0,
        "high_priority": 0,
        "urgent": 0,
        "unscheduled": 0
    }

    for t in tasks:
        due = _parse_due_date(t)
        priority = t.get("priority", 0)

        if due:
            if due < now:
                counts["overdue"] += 1
            if due <= today_end:
                counts["due_today"] += 1
            if due <= week_end:
                counts["due_this_week"] += 1
        else:
            counts["unscheduled"] += 1

        if priority >= 3:
            counts["high_priority"] += 1
        if priority >= 4:
            counts["urgent"] += 1

    return counts


@mcp.tool()
def unscheduled_tasks(
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Get tasks without a due date."""
    tasks = _get_all_tasks(instance)

    unscheduled = [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "priority": t.get("priority", 0),
            "project": t.get("_project_title", ""),
            "instance": t.get("_instance", "")
        }
        for t in tasks if not _parse_due_date(t)
    ]

    return {"tasks": unscheduled, "count": len(unscheduled)}


@mcp.tool()
def upcoming_deadlines(
    days: int = Field(default=3, description="Days to look ahead"),
    instance: str = Field(default="", description="Filter to specific instance. Empty = all.")
) -> dict:
    """Get tasks due in next N days (not overdue)."""
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=days)
    tasks = _get_all_tasks(instance)

    upcoming = []
    for t in tasks:
        due = _parse_due_date(t)
        if due and now <= due <= future:
            upcoming.append({
                "id": t["id"],
                "title": t.get("title", ""),
                "due_date": t.get("due_date"),
                "priority": t.get("priority", 0),
                "project": t.get("_project_title", ""),
                "instance": t.get("_instance", "")
            })

    upcoming.sort(key=lambda x: x.get("due_date", ""))
    return {"tasks": upcoming, "count": len(upcoming)}


# ============================================================================
# X-Q (EXCHANGE QUEUE) TOOLS
# ============================================================================

def _get_xq_config() -> dict:
    """Get X-Q project IDs from config."""
    config = _load_config()
    return config.get("xq", {})


def _get_xq_kanban_view(instance: str, project_id: int) -> dict:
    """Get kanban view and buckets for X-Q project."""
    views = _request("GET", f"/projects/{project_id}/views", instance=instance)

    kanban_view = None
    for v in views:
        if v.get("view_kind") == "kanban":
            kanban_view = v
            break

    if not kanban_view:
        return {"error": "No kanban view found"}

    buckets = _request("GET", f"/projects/{project_id}/views/{kanban_view['id']}/buckets", instance=instance)
    bucket_map = {b.get("title", ""): b["id"] for b in buckets}

    return {
        "view_id": kanban_view["id"],
        "buckets": bucket_map
    }


@mcp.tool()
def check_xq(
    instance: str = Field(default="", description="Instance name (empty=all)")
) -> dict:
    """Check X-Q for pending handoff items."""
    xq_config = _get_xq_config()

    if instance:
        if instance not in xq_config:
            return {"error": f"X-Q not configured for '{instance}'"}
        xq_config = {instance: xq_config[instance]}

    if not xq_config:
        return {"error": "No X-Q configured. Add 'xq' section to ~/.vikunja-mcp/config.yaml"}

    results = []
    for inst_name, project_id in xq_config.items():
        try:
            tasks = _request("GET", f"/projects/{project_id}/tasks", instance=inst_name)
            pending = [t for t in tasks if not t.get("done")]
            for t in pending:
                results.append({
                    "id": t["id"],
                    "title": t.get("title", ""),
                    "description": (t.get("description") or "")[:200],
                    "instance": inst_name,
                    "project_id": project_id
                })
        except Exception as e:
            results.append({"instance": inst_name, "error": str(e)})

    return {"pending": results, "count": len([r for r in results if "id" in r])}


@mcp.tool()
def setup_xq(
    instance: str = Field(description="Instance name to setup X-Q buckets")
) -> dict:
    """Setup X-Q project with standard buckets."""
    xq_config = _get_xq_config()
    if instance not in xq_config:
        return {"error": f"X-Q not configured for '{instance}'. Add to config.yaml first."}

    project_id = xq_config[instance]
    XQ_BUCKETS = ["📬 Handoff", "🔍 Review", "✅ Filed"]

    kanban_info = _get_xq_kanban_view(instance, project_id)
    if "error" in kanban_info:
        return kanban_info

    view_id = kanban_info["view_id"]
    existing = kanban_info["buckets"]

    created = []
    for bucket_name in XQ_BUCKETS:
        if bucket_name not in existing:
            _request("PUT", f"/projects/{project_id}/views/{view_id}/buckets",
                    instance=instance, json={"title": bucket_name})
            created.append(bucket_name)

    return {
        "instance": instance,
        "project_id": project_id,
        "created": created,
        "existing": [b for b in XQ_BUCKETS if b in existing]
    }


@mcp.tool()
def claim_xq_task(
    instance: str = Field(description="Instance name"),
    task_id: int = Field(description="Task ID to claim")
) -> dict:
    """Claim an X-Q task - moves to Review bucket."""
    xq_config = _get_xq_config()
    if instance not in xq_config:
        return {"error": f"X-Q not configured for '{instance}'"}

    project_id = xq_config[instance]
    kanban_info = _get_xq_kanban_view(instance, project_id)
    if "error" in kanban_info:
        return {"error": kanban_info["error"], "hint": "Run setup_xq first"}

    review_bucket = kanban_info["buckets"].get("🔍 Review")
    if not review_bucket:
        return {"error": "No Review bucket. Run setup_xq first."}

    task = _request("GET", f"/tasks/{task_id}", instance=instance)

    _request("POST", f"/projects/{project_id}/views/{kanban_info['view_id']}/buckets/{review_bucket}/tasks",
            instance=instance, json={
                "task_id": task_id,
                "bucket_id": review_bucket,
                "project_view_id": kanban_info["view_id"],
                "project_id": project_id
            })

    return {
        "claimed": task_id,
        "title": task.get("title"),
        "description": task.get("description"),
        "moved_to": "🔍 Review",
        "instance": instance
    }


@mcp.tool()
def complete_xq_task(
    instance: str = Field(description="Instance name"),
    task_id: int = Field(description="Task ID"),
    destination: str = Field(description="Where the file was placed"),
    notes: str = Field(default="", description="Optional notes")
) -> dict:
    """Complete an X-Q task - marks done and moves to Filed."""
    xq_config = _get_xq_config()
    if instance not in xq_config:
        return {"error": f"X-Q not configured for '{instance}'"}

    project_id = xq_config[instance]
    kanban_info = _get_xq_kanban_view(instance, project_id)
    if "error" in kanban_info:
        return kanban_info

    filed_bucket = kanban_info["buckets"].get("✅ Filed")
    if not filed_bucket:
        return {"error": "No Filed bucket. Run setup_xq first."}

    task = _request("GET", f"/tasks/{task_id}", instance=instance)

    # Update description with filing info
    desc = task.get("description", "") or ""
    filing = f"\n\n---\n**Filed to:** `{destination}`\n**Filed at:** {datetime.now().isoformat()}"
    if notes:
        filing += f"\n**Notes:** {notes}"

    _request("POST", f"/tasks/{task_id}", instance=instance, json={
        "description": desc + filing,
        "done": True
    })

    _request("POST", f"/projects/{project_id}/views/{kanban_info['view_id']}/buckets/{filed_bucket}/tasks",
            instance=instance, json={
                "task_id": task_id,
                "bucket_id": filed_bucket,
                "project_view_id": kanban_info["view_id"],
                "project_id": project_id
            })

    return {
        "filed": task_id,
        "title": task.get("title"),
        "destination": destination,
        "instance": instance
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
