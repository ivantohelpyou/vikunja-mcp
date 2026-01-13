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
def create_view(
    project_id: int = Field(description="Project ID"),
    title: str = Field(description="View title"),
    view_kind: str = Field(description="View type: list, kanban, gantt, or table"),
    filter_query: str = Field(default="", description="Optional filter (e.g., 'done = false')")
) -> dict:
    """Create a new view for a project."""
    data = {
        "title": title,
        "view_kind": view_kind,
        "project_id": project_id,
    }
    if filter_query:
        data["filter"] = filter_query
    view = _request("PUT", f"/projects/{project_id}/views", json=data)
    return {
        "id": view["id"],
        "title": view.get("title", ""),
        "view_kind": view.get("view_kind", ""),
        "project_id": project_id
    }


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
    # Call 1: Add task to bucket
    bucket_data = {
        "task_id": task_id,
        "bucket_id": bucket_id,
        "project_view_id": view_id,
        "project_id": project_id
    }
    _request("POST", f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks", json=bucket_data)

    # Call 2: CRITICAL - Commit the bucket assignment (bucket_id required!)
    position_data = {
        "project_view_id": view_id,
        "task_id": task_id,
        "bucket_id": bucket_id
    }
    _request("POST", f"/tasks/{task_id}/position", json=position_data)

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
    # CRITICAL: Commit the bucket assignment (bucket_id required!)
    _request("POST", f"/tasks/{task_id}/position", instance=instance, json={
        "project_view_id": kanban_info["view_id"],
        "task_id": task_id,
        "bucket_id": review_bucket
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
    # CRITICAL: Commit the bucket assignment (bucket_id required!)
    _request("POST", f"/tasks/{task_id}/position", instance=instance, json={
        "project_view_id": kanban_info["view_id"],
        "task_id": task_id,
        "bucket_id": filed_bucket
    })

    return {
        "filed": task_id,
        "title": task.get("title"),
        "destination": destination,
        "instance": instance
    }


# ============================================================================
# ADDITIONAL TASK TOOLS
# ============================================================================

@mcp.tool()
def assign_user(
    task_id: int = Field(description="ID of the task"),
    user_id: int = Field(description="ID of the user to assign")
) -> dict:
    """Assign a user to a task."""
    _request("PUT", f"/tasks/{task_id}/assignees", json={"user_id": user_id})
    return {"task_id": task_id, "user_id": user_id, "assigned": True}


@mcp.tool()
def unassign_user(
    task_id: int = Field(description="ID of the task"),
    user_id: int = Field(description="ID of the user to unassign")
) -> dict:
    """Remove a user from a task."""
    _request("DELETE", f"/tasks/{task_id}/assignees/{user_id}")
    return {"task_id": task_id, "user_id": user_id, "unassigned": True}


@mcp.tool()
def set_reminders(
    task_id: int = Field(description="ID of the task"),
    reminders: list = Field(description="List of reminder datetimes in ISO format. Empty list clears all.")
) -> dict:
    """Set reminders on a task. Replaces all existing reminders."""
    current = _request("GET", f"/tasks/{task_id}")
    current["reminders"] = [{"reminder": r, "relative_period": 0, "relative_to": ""} for r in reminders]
    response = _request("POST", f"/tasks/{task_id}", json=current)
    return {
        "task_id": task_id,
        "reminders_set": len(reminders),
        "title": response.get("title", "")
    }


@mcp.tool()
def add_to_calendar(
    project_id: int = Field(description="Project ID"),
    title: str = Field(description="Event title"),
    due_date: str = Field(description="Due date/time in ISO format"),
    description: str = Field(default="", description="Optional description"),
    start_date: str = Field(default="", description="Start date for GANTT (optional)"),
    end_date: str = Field(default="", description="End date for GANTT (optional)"),
    label_name: str = Field(default="calendar", description="Label name to add (default: 'calendar')")
) -> dict:
    """Add an event to the calendar by creating a task with a label.

    Tasks with the 'calendar' label appear in the ICS calendar feed.
    """
    # Create the task
    data = {"title": title, "project_id": project_id, "due_date": due_date}
    if description:
        data["description"] = description
    if start_date:
        data["start_date"] = start_date
    if end_date:
        data["end_date"] = end_date

    task = _request("PUT", f"/projects/{project_id}/tasks", json=data)
    task_id = task["id"]

    # Find or create the label
    labels = _request("GET", "/labels")
    label_id = None
    for label in labels:
        if label.get("title", "").lower() == label_name.lower():
            label_id = label["id"]
            break

    if not label_id:
        new_label = _request("PUT", "/labels", json={"title": label_name, "hex_color": "3498db"})
        label_id = new_label["id"]

    # Add label to task
    _request("PUT", f"/tasks/{task_id}/labels", json={"label_id": label_id})

    return {
        "task_id": task_id,
        "title": title,
        "due_date": due_date,
        "label": label_name
    }


# ============================================================================
# ADDITIONAL VIEW & BUCKET TOOLS
# ============================================================================

@mcp.tool()
def delete_view(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="View ID to delete")
) -> dict:
    """Delete a view from a project."""
    _request("DELETE", f"/projects/{project_id}/views/{view_id}")
    return {"success": True, "deleted_view": view_id, "project_id": project_id}


@mcp.tool()
def update_view(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="View ID to update"),
    title: str = Field(default="", description="New title (empty = keep current)"),
    filter_query: str = Field(default="", description="New filter (empty = keep current)")
) -> dict:
    """Update a view's title and/or filter."""
    data = {}
    if title:
        data["title"] = title
    if filter_query:
        data["filter"] = filter_query
    if not data:
        return {"error": "At least one of title or filter_query must be provided"}
    response = _request("POST", f"/projects/{project_id}/views/{view_id}", json=data)
    return {"id": response["id"], "title": response.get("title", ""), "view_kind": response.get("view_kind", "")}


@mcp.tool()
def delete_bucket(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="View ID"),
    bucket_id: int = Field(description="Bucket ID to delete")
) -> dict:
    """Delete a kanban bucket. Tasks may be moved to another bucket."""
    _request("DELETE", f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}")
    return {"deleted": True, "bucket_id": bucket_id}


@mcp.tool()
def get_view_tasks(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="View ID")
) -> dict:
    """Get tasks via a specific view. For kanban, includes bucket info."""
    response = _request("GET", f"/projects/{project_id}/views/{view_id}/tasks")
    tasks = []
    for item in response:
        if "tasks" in item:
            # Kanban bucket with nested tasks
            bucket_id = item["id"]
            bucket_title = item.get("title", "")
            for task in (item.get("tasks") or []):
                task["bucket_id"] = bucket_id
                task["bucket_title"] = bucket_title
                tasks.append({
                    "id": task["id"],
                    "title": task.get("title", ""),
                    "done": task.get("done", False),
                    "priority": task.get("priority", 0),
                    "due_date": task.get("due_date"),
                    "bucket_id": bucket_id,
                    "bucket_title": bucket_title
                })
        else:
            # Non-kanban view - flat task list
            tasks.append({
                "id": item["id"],
                "title": item.get("title", ""),
                "done": item.get("done", False),
                "priority": item.get("priority", 0),
                "due_date": item.get("due_date")
            })
    return {"tasks": tasks, "count": len(tasks)}


@mcp.tool()
def list_tasks_by_bucket(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="Kanban view ID")
) -> dict:
    """Get tasks grouped by kanban bucket. Returns dict with bucket names as keys."""
    response = _request("GET", f"/projects/{project_id}/views/{view_id}/tasks")
    buckets = {}
    for item in response:
        if "tasks" in item:
            bucket_name = item.get("title", "Unnamed")
            buckets[bucket_name] = {
                "bucket_id": item["id"],
                "tasks": [{
                    "id": t["id"],
                    "title": t.get("title", ""),
                    "done": t.get("done", False),
                    "priority": t.get("priority", 0),
                    "due_date": t.get("due_date")
                } for t in (item.get("tasks") or [])]
            }
    return buckets


@mcp.tool()
def set_view_position(
    task_id: int = Field(description="Task ID"),
    view_id: int = Field(description="View ID"),
    position: float = Field(description="Position value (lower = earlier)")
) -> dict:
    """Set a task's position within a view. For Gantt, List, or Table views."""
    _request("POST", f"/tasks/{task_id}/position", json={
        "position": position,
        "view_id": view_id
    })
    return {"task_id": task_id, "view_id": view_id, "position": position}


@mcp.tool()
def create_filtered_view(
    project_id: int = Field(description="Project ID"),
    title: str = Field(description="View title"),
    view_kind: str = Field(description="View type: list, kanban, gantt, or table"),
    filter_query: str = Field(description="Filter query (e.g., 'priority >= 3', 'dueDate < now')"),
    bucket_config_mode: str = Field(default="manual", description="Bucket mode: 'manual' or 'none'")
) -> dict:
    """Create a filtered view showing only tasks matching criteria."""
    data = {
        "title": title,
        "view_kind": view_kind,
        "project_id": project_id,
        "filter": filter_query,
        "bucket_configuration_mode": 1 if bucket_config_mode == "manual" else 0
    }
    view = _request("PUT", f"/projects/{project_id}/views", json=data)
    return {"id": view["id"], "title": view.get("title", ""), "view_kind": view.get("view_kind", ""), "filter": filter_query}


# ============================================================================
# BATCH OPERATIONS
# ============================================================================

@mcp.tool()
def batch_create_tasks(
    project_id: int = Field(description="Project ID"),
    tasks: list = Field(description="List of task objects: [{title, description?, due_date?, priority?, labels?, bucket?, ref?, blocked_by?, blocks?}]"),
    create_missing_labels: bool = Field(default=True, description="Auto-create labels that don't exist")
) -> dict:
    """Create multiple tasks at once with labels and relations.

    Use 'ref' field to create relations between tasks in the same batch.
    """
    result = {"created": 0, "tasks": [], "labels_created": [], "relations_created": 0, "errors": []}
    ref_to_id = {}

    # Get existing labels
    existing_labels = {l["title"].lower(): l["id"] for l in _request("GET", "/labels")}

    # Get kanban view and buckets if any task specifies a bucket
    bucket_map = {}
    if any(t.get("bucket") for t in tasks):
        views = _request("GET", f"/projects/{project_id}/views")
        for v in views:
            if v.get("view_kind") == "kanban":
                buckets = _request("GET", f"/projects/{project_id}/views/{v['id']}/buckets")
                bucket_map = {b["title"]: {"id": b["id"], "view_id": v["id"]} for b in buckets}
                break

    # Create tasks
    for task_input in tasks:
        try:
            data = {"title": task_input["title"], "project_id": project_id}
            if task_input.get("description"):
                data["description"] = task_input["description"]
            if task_input.get("due_date"):
                data["due_date"] = task_input["due_date"]
            if task_input.get("start_date"):
                data["start_date"] = task_input["start_date"]
            if task_input.get("end_date"):
                data["end_date"] = task_input["end_date"]
            if task_input.get("priority"):
                data["priority"] = task_input["priority"]

            task = _request("PUT", f"/projects/{project_id}/tasks", json=data)
            task_id = task["id"]
            ref = task_input.get("ref", "")
            if ref:
                ref_to_id[ref] = task_id

            # Add labels
            for label_name in task_input.get("labels", []):
                label_key = label_name.lower()
                if label_key not in existing_labels:
                    if create_missing_labels:
                        new_label = _request("PUT", "/labels", json={"title": label_name, "hex_color": "808080"})
                        existing_labels[label_key] = new_label["id"]
                        result["labels_created"].append(label_name)
                    else:
                        continue
                _request("PUT", f"/tasks/{task_id}/labels", json={"label_id": existing_labels[label_key]})

            # Move to bucket (requires two API calls)
            bucket_name = task_input.get("bucket")
            if bucket_name and bucket_name in bucket_map:
                bucket_info = bucket_map[bucket_name]
                # Call 1: Add task to bucket
                bucket_data = {
                    "task_id": task_id,
                    "bucket_id": bucket_info["id"],
                    "project_view_id": bucket_info["view_id"],
                    "project_id": project_id
                }
                _request("POST", f"/projects/{project_id}/views/{bucket_info['view_id']}/buckets/{bucket_info['id']}/tasks",
                        json=bucket_data)
                # Call 2: CRITICAL - Commit the bucket assignment (bucket_id required!)
                position_data = {
                    "project_view_id": bucket_info["view_id"],
                    "task_id": task_id,
                    "bucket_id": bucket_info["id"]
                }
                _request("POST", f"/tasks/{task_id}/position", json=position_data)

            result["created"] += 1
            result["tasks"].append({"ref": ref, "id": task_id, "title": task_input["title"]})
        except Exception as e:
            result["errors"].append(f"Failed to create '{task_input.get('title', '?')}': {e}")

    # Create relations
    for task_input in tasks:
        ref = task_input.get("ref", "")
        task_id = ref_to_id.get(ref)
        if not task_id:
            continue

        for blocked_by_ref in task_input.get("blocked_by", []):
            other_id = ref_to_id.get(blocked_by_ref)
            if other_id:
                try:
                    _request("PUT", f"/tasks/{task_id}/relations", json={"other_task_id": other_id, "relation_kind": "blocked"})
                    result["relations_created"] += 1
                except:
                    pass

        for blocks_ref in task_input.get("blocks", []):
            other_id = ref_to_id.get(blocks_ref)
            if other_id:
                try:
                    _request("PUT", f"/tasks/{task_id}/relations", json={"other_task_id": other_id, "relation_kind": "blocking"})
                    result["relations_created"] += 1
                except:
                    pass

    return result


@mcp.tool()
def batch_update_tasks(
    updates: list = Field(description="List of updates: [{task_id, title?, description?, due_date?, priority?, reminders?}]")
) -> dict:
    """Update multiple tasks at once."""
    result = {"updated": 0, "tasks": [], "errors": []}

    for update in updates:
        task_id = update.get("task_id")
        if not task_id:
            result["errors"].append("Update missing task_id")
            continue

        try:
            current = _request("GET", f"/tasks/{task_id}")

            if "title" in update:
                current["title"] = update["title"]
            if "description" in update:
                current["description"] = update["description"]
            if "due_date" in update:
                current["due_date"] = update["due_date"]
            if "start_date" in update:
                current["start_date"] = update["start_date"]
            if "end_date" in update:
                current["end_date"] = update["end_date"]
            if "priority" in update:
                current["priority"] = update["priority"]
            if "reminders" in update:
                current["reminders"] = [{"reminder": r, "relative_period": 0, "relative_to": ""} for r in update["reminders"]]

            response = _request("POST", f"/tasks/{task_id}", json=current)
            result["updated"] += 1
            result["tasks"].append({"id": task_id, "title": response.get("title", "")})
        except Exception as e:
            result["errors"].append(f"Failed to update task {task_id}: {e}")

    return result


@mcp.tool()
def batch_set_positions(
    view_id: int = Field(description="View ID"),
    positions: list = Field(description="List of {task_id, position}")
) -> dict:
    """Set positions for multiple tasks in a view."""
    result = {"updated": 0, "tasks": [], "errors": []}

    for pos in positions:
        task_id = pos.get("task_id")
        position = pos.get("position")
        if not task_id or position is None:
            result["errors"].append(f"Invalid position entry: {pos}")
            continue

        try:
            _request("POST", f"/tasks/{task_id}/position", json={"position": position, "view_id": view_id})
            result["updated"] += 1
            result["tasks"].append({"task_id": task_id, "position": position})
        except Exception as e:
            result["errors"].append(f"Failed to set position for task {task_id}: {e}")

    return result


@mcp.tool()
def bulk_create_labels(
    labels: list = Field(description="List of {title, hex_color?}")
) -> dict:
    """Create multiple labels. Skips labels that already exist."""
    result = {"created_count": 0, "labels": [], "skipped": [], "errors": []}

    existing = {l["title"].lower() for l in _request("GET", "/labels")}

    for label_spec in labels:
        title = label_spec.get("title", "")
        if not title:
            continue

        if title.lower() in existing:
            result["skipped"].append(title)
            continue

        try:
            data = {"title": title}
            if label_spec.get("hex_color"):
                data["hex_color"] = label_spec["hex_color"].lstrip("#")

            label = _request("PUT", "/labels", json=data)
            result["created_count"] += 1
            result["labels"].append({"id": label["id"], "title": label.get("title", "")})
            existing.add(title.lower())
        except Exception as e:
            result["errors"].append(f"Failed to create label '{title}': {e}")

    return result


@mcp.tool()
def bulk_relabel_tasks(
    project_id: int = Field(description="Project ID"),
    task_ids: list = Field(description="List of task IDs"),
    add_labels: list = Field(default=None, description="Labels to add"),
    remove_labels: list = Field(default=None, description="Labels to remove"),
    set_labels: list = Field(default=None, description="Replace all labels with this list")
) -> dict:
    """Bulk update labels on multiple tasks."""
    result = {"updated": 0, "errors": []}

    # Get label ID mapping
    all_labels = {l["title"].lower(): l["id"] for l in _request("GET", "/labels")}

    for task_id in task_ids:
        try:
            if set_labels is not None:
                # Remove all existing labels
                task = _request("GET", f"/tasks/{task_id}")
                for existing_label in task.get("labels", []):
                    _request("DELETE", f"/tasks/{task_id}/labels/{existing_label['id']}")
                # Add new labels
                for label_name in set_labels:
                    label_id = all_labels.get(label_name.lower())
                    if label_id:
                        _request("PUT", f"/tasks/{task_id}/labels", json={"label_id": label_id})
            else:
                if remove_labels:
                    for label_name in remove_labels:
                        label_id = all_labels.get(label_name.lower())
                        if label_id:
                            try:
                                _request("DELETE", f"/tasks/{task_id}/labels/{label_id}")
                            except:
                                pass
                if add_labels:
                    for label_name in add_labels:
                        label_id = all_labels.get(label_name.lower())
                        if label_id:
                            _request("PUT", f"/tasks/{task_id}/labels", json={"label_id": label_id})

            result["updated"] += 1
        except Exception as e:
            result["errors"].append(f"Failed to update task {task_id}: {e}")

    return result


@mcp.tool()
def bulk_set_task_positions(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="Kanban view ID"),
    assignments: list = Field(description="List of {task_id, bucket_id, position?}")
) -> dict:
    """Bulk assign tasks to kanban buckets."""
    result = {"moved_count": 0, "tasks": [], "errors": []}

    for assignment in assignments:
        task_id = assignment.get("task_id")
        bucket_id = assignment.get("bucket_id")
        if not task_id or not bucket_id:
            result["errors"].append(f"Invalid assignment: {assignment}")
            continue

        try:
            _request("POST", f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
                    json={"task_id": task_id, "bucket_id": bucket_id, "project_view_id": view_id, "project_id": project_id})
            # CRITICAL: Commit the bucket assignment (bucket_id required!)
            _request("POST", f"/tasks/{task_id}/position", json={"project_view_id": view_id, "task_id": task_id, "bucket_id": bucket_id})
            result["moved_count"] += 1
            result["tasks"].append({"task_id": task_id, "bucket_id": bucket_id, "success": True})
        except Exception as e:
            result["errors"].append(f"Failed to move task {task_id}: {e}")

    return result


# ============================================================================
# SETUP & WORKFLOW TOOLS
# ============================================================================

KANBAN_TEMPLATES = {
    "gtd": ["📥 Inbox", "⏳ Next", "🔄 Waiting", "📅 Someday", "✅ Done"],
    "sprint": ["📋 Backlog", "📝 To Do", "🔨 In Progress", "👀 Review", "✅ Done"],
    "kitchen": ["💡 Idea", "📋 Planning", "🛒 Shopping", "🧹 Prep", "🍳 Cooking", "🍽️ Plating", "🧼 Cleanup", "📸 Photo", "✅ Done"],
    "simple": ["📝 To Do", "🔨 Doing", "✅ Done"]
}


@mcp.tool()
def setup_kanban_board(
    project_id: int = Field(default=0, description="Existing project ID (or use project_title for new)"),
    project_title: str = Field(default="", description="Create new project with this title"),
    template: str = Field(default="gtd", description="Template: gtd, sprint, kitchen, simple, or custom"),
    custom_buckets: list = Field(default=None, description="For template='custom': list of bucket titles"),
    view_title: str = Field(default="Kanban", description="Name for the kanban view"),
    delete_default_buckets: bool = Field(default=True, description="Delete auto-created buckets")
) -> dict:
    """Rapid kanban board setup with templates.

    Templates: gtd, sprint, kitchen, simple, custom
    Idempotent: safe to call multiple times without creating duplicates.
    """
    # Get or create project
    if project_title and not project_id:
        proj = _request("PUT", "/projects", json={"title": project_title})
        project_id = proj["id"]
        created_project = True
    else:
        created_project = False

    # Get bucket config from template
    if template == "custom" and custom_buckets:
        bucket_names = custom_buckets
    else:
        bucket_names = KANBAN_TEMPLATES.get(template, KANBAN_TEMPLATES["gtd"])

    # Get or create kanban view (idempotent - reuse existing by title)
    views = _request("GET", f"/projects/{project_id}/views")
    kanban_views = [v for v in views if v.get("view_kind") == "kanban" and v.get("title") == view_title]

    if kanban_views:
        view = kanban_views[0]  # Reuse existing
        view_existed = True
    else:
        view = _request("PUT", f"/projects/{project_id}/views", json={
            "title": view_title,
            "view_kind": "kanban",
            "bucket_configuration_mode": "manual"
        })
        view_existed = False
    view_id = view["id"]

    # Get existing buckets and create map by title (idempotent bucket creation)
    existing_buckets = _request("GET", f"/projects/{project_id}/views/{view_id}/buckets")
    existing_bucket_map = {b["title"]: b for b in existing_buckets}

    # Create buckets (idempotent - reuse existing by title)
    created_buckets = []
    for i, name in enumerate(bucket_names):
        if name in existing_bucket_map:
            bucket = existing_bucket_map[name]  # Reuse existing
        else:
            bucket = _request("PUT", f"/projects/{project_id}/views/{view_id}/buckets",
                            json={"title": name, "position": i * 1000})
        created_buckets.append({"id": bucket["id"], "title": bucket["title"]})

    # Delete default buckets AFTER template buckets exist
    buckets_deleted = 0
    if delete_default_buckets:
        all_buckets = _request("GET", f"/projects/{project_id}/views/{view_id}/buckets")
        created_bucket_ids = {b["id"] for b in created_buckets}
        for bucket in all_buckets:
            if bucket["id"] not in created_bucket_ids:
                try:
                    _request("DELETE", f"/projects/{project_id}/views/{view_id}/buckets/{bucket['id']}")
                    buckets_deleted += 1
                except:
                    pass

    return {
        "project_id": project_id,
        "project_created": created_project,
        "view_id": view_id,
        "view_title": view_title,
        "view_existed": view_existed,
        "buckets_created": len([b for b in created_buckets if b["title"] not in existing_bucket_map]),
        "buckets": created_buckets,
        "buckets_deleted": buckets_deleted
    }


@mcp.tool()
def setup_project(
    project_id: int = Field(description="Project ID to set up"),
    buckets: list = Field(default=[], description="Bucket names to create"),
    labels: list = Field(default=[], description="Labels to create: [{name, color?}]"),
    tasks: list = Field(default=[], description="Tasks to create (same schema as batch_create_tasks)")
) -> dict:
    """Set up a project with buckets, labels, and tasks in one call."""
    result = {"buckets_created": [], "labels_created": [], "tasks_result": None, "errors": []}

    # Get or create kanban view
    views = _request("GET", f"/projects/{project_id}/views")
    kanban_view = None
    for v in views:
        if v.get("view_kind") == "kanban":
            kanban_view = v
            break

    if not kanban_view and buckets:
        kanban_view = _request("PUT", f"/projects/{project_id}/views", json={
            "title": "Kanban",
            "view_kind": "kanban",
            "project_id": project_id
        })

    # Create buckets
    if buckets and kanban_view:
        view_id = kanban_view["id"]
        existing = {b["title"] for b in _request("GET", f"/projects/{project_id}/views/{view_id}/buckets")}
        for i, name in enumerate(buckets):
            if name not in existing:
                try:
                    _request("PUT", f"/projects/{project_id}/views/{view_id}/buckets",
                            json={"title": name, "position": i * 1000})
                    result["buckets_created"].append(name)
                except Exception as e:
                    result["errors"].append(f"Failed to create bucket '{name}': {e}")

    # Create labels
    existing_labels = {l["title"].lower() for l in _request("GET", "/labels")}
    for label_spec in labels:
        name = label_spec.get("name", "")
        if not name or name.lower() in existing_labels:
            continue
        try:
            data = {"title": name}
            if label_spec.get("color"):
                data["hex_color"] = label_spec["color"].lstrip("#")
            _request("PUT", "/labels", json=data)
            result["labels_created"].append(name)
            existing_labels.add(name.lower())
        except Exception as e:
            result["errors"].append(f"Failed to create label '{name}': {e}")

    # Create tasks
    if tasks:
        # Reuse batch_create_tasks logic
        task_result = batch_create_tasks(project_id=project_id, tasks=tasks)
        result["tasks_result"] = task_result

    return result


@mcp.tool()
def sort_bucket(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="Kanban view ID"),
    bucket_id: int = Field(description="Bucket ID to sort"),
    sort_by: str = Field(default="due_date", description="Sort field: due_date, priority, title, created"),
    then_by: str = Field(default="", description="Secondary sort for ties")
) -> dict:
    """Re-sort all tasks in a bucket."""
    # Get tasks in bucket
    response = _request("GET", f"/projects/{project_id}/views/{view_id}/tasks")
    bucket_tasks = []
    for item in response:
        if item.get("id") == bucket_id and "tasks" in item:
            bucket_tasks = item.get("tasks", [])
            break

    if not bucket_tasks:
        return {"sorted": 0, "error": "No tasks in bucket"}

    # Sort tasks
    def get_sort_key(task):
        primary = task.get(sort_by) or ""
        secondary = task.get(then_by) or "" if then_by else ""
        return (primary, secondary)

    sorted_tasks = sorted(bucket_tasks, key=get_sort_key)

    # Update positions
    result = {"sorted": 0, "tasks": [], "errors": []}
    for i, task in enumerate(sorted_tasks):
        position = (i + 1) * 1000
        try:
            _request("POST", f"/tasks/{task['id']}/position", json={
                "position": position,
                "view_id": view_id
            })
            result["sorted"] += 1
            result["tasks"].append({"task_id": task["id"], "position": position})
        except Exception as e:
            result["errors"].append(f"Failed to set position for task {task['id']}: {e}")

    return result


# ============================================================================
# TASK MOVEMENT & COMPLETION
# ============================================================================

@mcp.tool()
def move_task_to_project(
    task_id: int = Field(description="Task ID to move"),
    target_project_id: int = Field(description="Target project ID")
) -> dict:
    """Move a task to a different project."""
    task = _request("GET", f"/tasks/{task_id}")
    old_project_id = task.get("project_id")

    task["project_id"] = target_project_id
    _request("POST", f"/tasks/{task_id}", json=task)

    return {
        "task_id": task_id,
        "title": task.get("title", ""),
        "old_project_id": old_project_id,
        "new_project_id": target_project_id,
        "moved": True
    }


@mcp.tool()
def move_task_to_project_by_name(
    task_id: int = Field(description="Task ID to move"),
    project_name: str = Field(description="Target project name (fuzzy match)")
) -> dict:
    """Move a task to a project by name (fuzzy matching)."""
    # Find matching projects
    projects = _request("GET", "/projects")
    matches = []
    project_name_lower = project_name.lower()

    for p in projects:
        title = p.get("title", "")
        if title.lower() == project_name_lower:
            matches = [p]
            break
        elif project_name_lower in title.lower():
            matches.append(p)

    if not matches:
        return {"error": f"No project matching '{project_name}'", "available": [p.get("title") for p in projects[:10]]}
    elif len(matches) > 1:
        return {"error": "Ambiguous project name", "matches": [p.get("title") for p in matches]}

    target = matches[0]
    return move_task_to_project(task_id=task_id, target_project_id=target["id"])


@mcp.tool()
def complete_tasks_by_label(
    project_id: int = Field(description="Project ID"),
    label_filter: str = Field(description="Label name to match")
) -> dict:
    """Complete all tasks with a matching label."""
    tasks = _request("GET", f"/projects/{project_id}/tasks")
    result = {"completed": 0, "tasks": [], "errors": []}

    for task in tasks:
        if task.get("done"):
            continue

        task_labels = [l.get("title", "").lower() for l in task.get("labels", [])]
        if label_filter.lower() in " ".join(task_labels):
            try:
                _request("POST", f"/tasks/{task['id']}", json={"done": True})
                result["completed"] += 1
                result["tasks"].append({"id": task["id"], "title": task.get("title", "")})
            except Exception as e:
                result["errors"].append(f"Failed to complete task {task['id']}: {e}")

    return result


@mcp.tool()
def move_tasks_by_label(
    project_id: int = Field(description="Project ID"),
    label_filter: str = Field(description="Label name to match"),
    view_id: int = Field(description="Kanban view ID"),
    bucket_id: int = Field(description="Target bucket ID")
) -> dict:
    """Move all tasks with a matching label to a bucket."""
    tasks = _request("GET", f"/projects/{project_id}/tasks")
    result = {"moved": 0, "tasks": [], "errors": []}

    for task in tasks:
        if task.get("done"):
            continue

        task_labels = [l.get("title", "").lower() for l in task.get("labels", [])]
        if label_filter.lower() in " ".join(task_labels):
            try:
                _request("POST", f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
                        json={"task_id": task["id"], "bucket_id": bucket_id, "project_view_id": view_id, "project_id": project_id})
                # CRITICAL: Commit the bucket assignment (bucket_id required!)
                _request("POST", f"/tasks/{task['id']}/position", json={"project_view_id": view_id, "task_id": task["id"], "bucket_id": bucket_id})
                result["moved"] += 1
                result["tasks"].append({"id": task["id"], "title": task.get("title", "")})
            except Exception as e:
                result["errors"].append(f"Failed to move task {task['id']}: {e}")

    return result


@mcp.tool()
def move_tasks_by_label_to_buckets(
    project_id: int = Field(description="Project ID"),
    view_id: int = Field(description="Kanban view ID"),
    label_to_bucket_map: dict = Field(description="Map of label titles to bucket IDs")
) -> dict:
    """Move tasks to buckets based on their labels."""
    tasks = _request("GET", f"/projects/{project_id}/tasks")
    result = {"moved_count": 0, "by_label": {}, "errors": []}

    for label_title, bucket_id in label_to_bucket_map.items():
        result["by_label"][label_title] = 0
        label_lower = label_title.lower()

        for task in tasks:
            if task.get("done"):
                continue

            task_labels = [l.get("title", "").lower() for l in task.get("labels", [])]
            if label_lower in task_labels:
                try:
                    _request("POST", f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
                            json={"task_id": task["id"], "bucket_id": bucket_id, "project_view_id": view_id, "project_id": project_id})
                    # CRITICAL: Commit the bucket assignment (bucket_id required!)
                    _request("POST", f"/tasks/{task['id']}/position", json={"project_view_id": view_id, "task_id": task["id"], "bucket_id": bucket_id})
                    result["moved_count"] += 1
                    result["by_label"][label_title] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to move task {task['id']}: {e}")

    return result


# ============================================================================
# EXPORT & IMPORT
# ============================================================================

@mcp.tool()
def export_all_projects() -> dict:
    """Export all projects and tasks for backup."""
    projects = _request("GET", "/projects")
    result = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "project_count": len(projects),
        "task_count": 0,
        "projects": []
    }

    for project in projects:
        project_data = {
            "id": project["id"],
            "title": project.get("title", ""),
            "description": project.get("description", ""),
            "tasks": []
        }

        tasks = _request("GET", f"/projects/{project['id']}/tasks")
        for task in tasks:
            project_data["tasks"].append({
                "id": task["id"],
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "done": task.get("done", False),
                "priority": task.get("priority", 0),
                "due_date": task.get("due_date"),
                "labels": [l.get("title") for l in task.get("labels", [])]
            })

        result["task_count"] += len(project_data["tasks"])
        result["projects"].append(project_data)

    return result


@mcp.tool()
def import_from_export(
    export_json: str = Field(description="JSON string from export_all_projects"),
    skip_existing: bool = Field(default=True, description="Skip projects that already exist by title")
) -> dict:
    """Import projects from an export file."""
    try:
        export_data = json.loads(export_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    result = {"projects_created": 0, "tasks_created": 0, "skipped": [], "errors": []}

    # Get existing project titles
    existing = {p.get("title", "").lower() for p in _request("GET", "/projects")}

    for project_data in export_data.get("projects", []):
        title = project_data.get("title", "")

        if skip_existing and title.lower() in existing:
            result["skipped"].append(title)
            continue

        try:
            # Create project
            project = _request("PUT", "/projects", json={
                "title": title,
                "description": project_data.get("description", "")
            })
            result["projects_created"] += 1

            # Create tasks
            for task_data in project_data.get("tasks", []):
                _request("PUT", f"/projects/{project['id']}/tasks", json={
                    "title": task_data.get("title", ""),
                    "description": task_data.get("description", ""),
                    "done": task_data.get("done", False),
                    "priority": task_data.get("priority", 0),
                    "due_date": task_data.get("due_date")
                })
                result["tasks_created"] += 1
        except Exception as e:
            result["errors"].append(f"Failed to import project '{title}': {e}")

    return result


# ============================================================================
# ANALYSIS & HEALTH
# ============================================================================

@mcp.tool()
def analyze_project_dimensions(
    project_id: int = Field(description="Project ID to analyze")
) -> dict:
    """Analyze a project's data to discover meaningful grouping dimensions."""
    tasks = _request("GET", f"/projects/{project_id}/tasks")

    # Analyze labels
    label_counts = {}
    for task in tasks:
        for label in task.get("labels", []):
            title = label.get("title", "")
            label_counts[title] = label_counts.get(title, 0) + 1

    # Analyze priorities
    priority_counts = {}
    for task in tasks:
        p = task.get("priority", 0)
        priority_counts[p] = priority_counts.get(p, 0) + 1

    # Analyze assignees
    assignee_counts = {}
    for task in tasks:
        for assignee in task.get("assignees", []):
            username = assignee.get("username", "")
            assignee_counts[username] = assignee_counts.get(username, 0) + 1

    # Suggest kanbans
    suggestions = []
    if len(label_counts) >= 2:
        suggestions.append({
            "name": "By Label",
            "buckets": list(label_counts.keys())[:8]
        })
    if any(p >= 3 for p in priority_counts.keys()):
        suggestions.append({
            "name": "By Priority",
            "buckets": ["Critical (4-5)", "High (3)", "Normal (1-2)", "None (0)"]
        })

    return {
        "project_id": project_id,
        "task_count": len(tasks),
        "labels": [{"title": k, "count": v} for k, v in sorted(label_counts.items(), key=lambda x: -x[1])],
        "priorities_in_use": sorted(priority_counts.keys(), reverse=True),
        "assignees": [{"username": k, "count": v} for k, v in sorted(assignee_counts.items(), key=lambda x: -x[1])],
        "suggested_kanbans": suggestions
    }


@mcp.tool()
def check_token_health(
    instance: str = Field(default="", description="Instance name (empty = current)")
) -> dict:
    """Check if a Vikunja API token is valid."""
    instances = _get_instances()
    name = instance or _get_current_instance()

    if not name or name not in instances:
        return {"error": f"Unknown instance: {name}", "available": list(instances.keys())}

    config = instances[name]
    url = config.get("url", "")
    token = config.get("token", "")

    if not url or not token:
        return {"error": "Instance missing url or token"}

    # Test the token
    try:
        resp = requests.get(
            f"{url}/api/v1/user",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )

        if resp.status_code == 200:
            user = resp.json()
            return {
                "instance": name,
                "url": url,
                "token_valid": True,
                "user": user.get("username", ""),
                "email": user.get("email", "")
            }
        else:
            return {
                "instance": name,
                "url": url,
                "token_valid": False,
                "error": f"HTTP {resp.status_code}"
            }
    except Exception as e:
        return {
            "instance": name,
            "url": url,
            "token_valid": False,
            "error": str(e)
        }


# ============================================================================
# CROSS-INSTANCE QUERIES
# ============================================================================

@mcp.tool()
def list_all_projects() -> dict:
    """List projects from ALL configured Vikunja instances."""
    instances = _get_instances()
    result = {"projects": [], "by_instance": {}}

    for name, config in instances.items():
        try:
            projects = _request("GET", "/projects", instance=name)
            for p in projects:
                result["projects"].append({
                    "id": p["id"],
                    "title": p.get("title", ""),
                    "instance": name
                })
            result["by_instance"][name] = len(projects)
        except Exception as e:
            result["by_instance"][name] = f"Error: {e}"

    return result


@mcp.tool()
def list_all_tasks(
    filter_due: str = Field(default="", description="Filter: 'today', 'week', 'overdue', or empty for all"),
    include_done: bool = Field(default=False, description="Include completed tasks"),
    instance: str = Field(default="", description="Filter to specific instance (empty = all)")
) -> dict:
    """List tasks from all instances with optional filters."""
    instances = _get_instances()
    if instance:
        instances = {instance: instances[instance]} if instance in instances else {}

    result = {"tasks": [], "by_instance": {}}
    now = datetime.now(timezone.utc)

    for name, config in instances.items():
        try:
            projects = _request("GET", "/projects", instance=name)
            instance_tasks = []

            for project in projects:
                tasks = _request("GET", f"/projects/{project['id']}/tasks", instance=name)
                for task in tasks:
                    if not include_done and task.get("done"):
                        continue

                    due = task.get("due_date")
                    if filter_due and due:
                        try:
                            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                            if filter_due == "overdue" and due_dt >= now:
                                continue
                            elif filter_due == "today":
                                if due_dt.date() != now.date() and due_dt >= now:
                                    continue
                            elif filter_due == "week":
                                week_end = now + timedelta(days=7)
                                if due_dt > week_end and due_dt >= now:
                                    continue
                        except ValueError:
                            pass
                    elif filter_due and not due:
                        continue

                    instance_tasks.append({
                        "id": task["id"],
                        "title": task.get("title", ""),
                        "project": project.get("title", ""),
                        "project_id": project["id"],
                        "due_date": due,
                        "priority": task.get("priority", 0),
                        "instance": name
                    })

            result["tasks"].extend(instance_tasks)
            result["by_instance"][name] = len(instance_tasks)
        except Exception as e:
            result["by_instance"][name] = f"Error: {e}"

    # Sort by due date, then priority
    result["tasks"].sort(key=lambda t: (t.get("due_date") or "9999", -t.get("priority", 0)))

    return result


@mcp.tool()
def search_all(
    query: str = Field(description="Search term for task/project titles"),
    instance: str = Field(default="", description="Filter to specific instance (empty = all)")
) -> dict:
    """Search tasks and projects across all instances."""
    instances = _get_instances()
    if instance:
        instances = {instance: instances[instance]} if instance in instances else {}

    result = {"results": [], "by_instance": {}}
    query_lower = query.lower()

    for name, config in instances.items():
        try:
            matches = []

            # Search projects
            projects = _request("GET", "/projects", instance=name)
            for p in projects:
                if query_lower in p.get("title", "").lower():
                    matches.append({
                        "type": "project",
                        "id": p["id"],
                        "title": p.get("title", ""),
                        "instance": name
                    })

                # Search tasks in project
                tasks = _request("GET", f"/projects/{p['id']}/tasks", instance=name)
                for t in tasks:
                    if query_lower in t.get("title", "").lower():
                        matches.append({
                            "type": "task",
                            "id": t["id"],
                            "title": t.get("title", ""),
                            "project": p.get("title", ""),
                            "instance": name
                        })

            result["results"].extend(matches)
            result["by_instance"][name] = len(matches)
        except Exception as e:
            result["by_instance"][name] = f"Error: {e}"

    return result


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
