"""
Vikunja MCP Server

MCP server that gives Claude full access to your Vikunja task management instance.
Works with any Vikunja instance - self-hosted, cloud, or local.

Tools:
- Projects: list, get, create, update, delete
- Tasks: list, get, create, update, complete, delete
- Labels: list, create, delete
- Kanban: list_buckets, create_bucket
- Relations: create, list

Configuration:
- VIKUNJA_URL: Base URL of Vikunja instance
- VIKUNJA_TOKEN: API authentication token

Source: https://github.com/ivantohelpyou/vikunja-mcp
PyPI: https://pypi.org/project/vikunja-mcp/
"""

import os
import logging
from typing import Optional

import requests
from fastmcp import FastMCP
from pydantic import Field

# ============================================================================
# CONFIGURATION
# ============================================================================

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
    instructions="""You have access to Vikunja task management tools.

Use these tools to help users manage their tasks, projects, labels, and kanban boards.
Always confirm destructive actions (delete) before executing."""
)

# ============================================================================
# VIKUNJA API CLIENT
# ============================================================================

def _get_vikunja_config() -> tuple[str, str]:
    """Get Vikunja URL and token from environment."""
    url = os.environ.get("VIKUNJA_URL", "").rstrip("/")
    token = os.environ.get("VIKUNJA_TOKEN", "")

    if not url or not token:
        raise ValueError("VIKUNJA_URL and VIKUNJA_TOKEN environment variables are required")

    return url, token


def _vikunja_request(method: str, endpoint: str, **kwargs) -> dict:
    """Make authenticated request to Vikunja API."""
    url, token = _get_vikunja_config()

    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"

    response = requests.request(
        method,
        f"{url}/api/v1{endpoint}",
        headers=headers,
        **kwargs
    )

    if response.status_code >= 400:
        try:
            error = response.json()
            msg = error.get("message", response.text)
        except:
            msg = response.text
        raise Exception(f"Vikunja API error ({response.status_code}): {msg}")

    if response.status_code == 204:
        return {}

    return response.json()


def _format_task(task: dict) -> str:
    """Format a task for display."""
    lines = [f"**{task.get('title', 'Untitled')}** (ID: {task['id']})"]

    if task.get("done"):
        lines[0] = f"~~{lines[0]}~~ ✓"

    if task.get("description"):
        desc = task["description"][:200]
        if len(task["description"]) > 200:
            desc += "..."
        lines.append(f"  {desc}")

    if task.get("due_date") and task["due_date"] != "0001-01-01T00:00:00Z":
        lines.append(f"  Due: {task['due_date'][:10]}")

    if task.get("priority"):
        priority_map = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
        lines.append(f"  Priority: {priority_map.get(task['priority'], task['priority'])}")

    if task.get("labels"):
        label_names = [l.get("title", "?") for l in task["labels"]]
        lines.append(f"  Labels: {', '.join(label_names)}")

    return "\n".join(lines)


def _format_project(project: dict) -> str:
    """Format a project for display."""
    return f"**{project.get('title', 'Untitled')}** (ID: {project['id']})"


# ============================================================================
# PROJECT TOOLS
# ============================================================================

@mcp.tool()
def list_projects() -> str:
    """List all projects in Vikunja."""
    projects = _vikunja_request("GET", "/projects")

    if not projects:
        return "No projects found."

    lines = ["**Projects:**", ""]
    for p in projects:
        lines.append(f"- {_format_project(p)}")

    return "\n".join(lines)


@mcp.tool()
def get_project(project_id: int = Field(description="The project ID")) -> str:
    """Get details of a specific project."""
    project = _vikunja_request("GET", f"/projects/{project_id}")

    lines = [
        f"**{project.get('title', 'Untitled')}**",
        f"ID: {project['id']}",
    ]

    if project.get("description"):
        lines.append(f"Description: {project['description']}")

    return "\n".join(lines)


@mcp.tool()
def create_project(
    title: str = Field(description="Project title"),
    description: str = Field(default="", description="Project description")
) -> str:
    """Create a new project."""
    project = _vikunja_request("PUT", "/projects", json={
        "title": title,
        "description": description
    })

    return f"Created project: {_format_project(project)}"


@mcp.tool()
def update_project(
    project_id: int = Field(description="The project ID"),
    title: str = Field(default=None, description="New title"),
    description: str = Field(default=None, description="New description")
) -> str:
    """Update an existing project."""
    data = {}
    if title is not None:
        data["title"] = title
    if description is not None:
        data["description"] = description

    if not data:
        return "No changes specified."

    project = _vikunja_request("POST", f"/projects/{project_id}", json=data)
    return f"Updated project: {_format_project(project)}"


@mcp.tool()
def delete_project(project_id: int = Field(description="The project ID")) -> str:
    """Delete a project. This cannot be undone!"""
    _vikunja_request("DELETE", f"/projects/{project_id}")
    return f"Deleted project {project_id}"


# ============================================================================
# TASK TOOLS
# ============================================================================

@mcp.tool()
def list_tasks(
    project_id: int = Field(default=None, description="Filter by project ID (required, or use list_all_tasks)"),
    done: bool = Field(default=None, description="Filter by completion status")
) -> str:
    """List tasks from a specific project."""
    if not project_id:
        # Get tasks from all projects
        projects = _vikunja_request("GET", "/projects")
        tasks = []
        for p in projects:
            try:
                project_tasks = _vikunja_request("GET", f"/projects/{p['id']}/tasks")
                tasks.extend(project_tasks)
            except:
                pass  # Skip projects we can't access
    else:
        tasks = _vikunja_request("GET", f"/projects/{project_id}/tasks")

    if done is not None:
        tasks = [t for t in tasks if t.get("done") == done]

    if not tasks:
        return "No tasks found."

    lines = ["**Tasks:**", ""]
    for t in tasks:
        lines.append(_format_task(t))
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def get_task(task_id: int = Field(description="The task ID")) -> str:
    """Get details of a specific task."""
    task = _vikunja_request("GET", f"/tasks/{task_id}")
    return _format_task(task)


@mcp.tool()
def create_task(
    project_id: int = Field(description="Project ID to create task in"),
    title: str = Field(description="Task title"),
    description: str = Field(default="", description="Task description"),
    due_date: str = Field(default=None, description="Due date (YYYY-MM-DD)"),
    priority: int = Field(default=0, description="Priority (1=low, 2=medium, 3=high, 4=urgent)")
) -> str:
    """Create a new task in a project."""
    data = {
        "title": title,
        "description": description,
        "priority": priority
    }

    if due_date:
        data["due_date"] = f"{due_date}T00:00:00Z"

    task = _vikunja_request("PUT", f"/projects/{project_id}/tasks", json=data)
    return f"Created task: {_format_task(task)}"


@mcp.tool()
def update_task(
    task_id: int = Field(description="The task ID"),
    title: str = Field(default=None, description="New title"),
    description: str = Field(default=None, description="New description"),
    due_date: str = Field(default=None, description="New due date (YYYY-MM-DD)"),
    priority: int = Field(default=None, description="New priority")
) -> str:
    """Update an existing task."""
    data = {}
    if title is not None:
        data["title"] = title
    if description is not None:
        data["description"] = description
    if due_date is not None:
        data["due_date"] = f"{due_date}T00:00:00Z"
    if priority is not None:
        data["priority"] = priority

    if not data:
        return "No changes specified."

    task = _vikunja_request("POST", f"/tasks/{task_id}", json=data)
    return f"Updated task: {_format_task(task)}"


@mcp.tool()
def complete_task(task_id: int = Field(description="The task ID")) -> str:
    """Mark a task as complete."""
    task = _vikunja_request("POST", f"/tasks/{task_id}", json={"done": True})
    return f"Completed task: {task.get('title', task_id)}"


@mcp.tool()
def delete_task(task_id: int = Field(description="The task ID")) -> str:
    """Delete a task. This cannot be undone!"""
    _vikunja_request("DELETE", f"/tasks/{task_id}")
    return f"Deleted task {task_id}"


# ============================================================================
# LABEL TOOLS
# ============================================================================

@mcp.tool()
def list_labels() -> str:
    """List all labels."""
    labels = _vikunja_request("GET", "/labels")

    if not labels:
        return "No labels found."

    lines = ["**Labels:**", ""]
    for label in labels:
        color = label.get("hex_color", "")
        lines.append(f"- **{label.get('title', '?')}** (ID: {label['id']}) {color}")

    return "\n".join(lines)


@mcp.tool()
def create_label(
    title: str = Field(description="Label title"),
    hex_color: str = Field(default="", description="Hex color (e.g., #ff0000)")
) -> str:
    """Create a new label."""
    label = _vikunja_request("PUT", "/labels", json={
        "title": title,
        "hex_color": hex_color.lstrip("#") if hex_color else ""
    })
    return f"Created label: {label.get('title')} (ID: {label['id']})"


@mcp.tool()
def delete_label(label_id: int = Field(description="The label ID")) -> str:
    """Delete a label."""
    _vikunja_request("DELETE", f"/labels/{label_id}")
    return f"Deleted label {label_id}"


@mcp.tool()
def add_label_to_task(
    task_id: int = Field(description="The task ID"),
    label_id: int = Field(description="The label ID to add")
) -> str:
    """Add a label to a task."""
    _vikunja_request("PUT", f"/tasks/{task_id}/labels", json={"label_id": label_id})
    return f"Added label {label_id} to task {task_id}"


# ============================================================================
# KANBAN TOOLS
# ============================================================================

@mcp.tool()
def list_buckets(project_id: int = Field(description="The project ID")) -> str:
    """List kanban buckets (columns) in a project."""
    # First get the project views to find the kanban view
    views = _vikunja_request("GET", f"/projects/{project_id}/views")

    kanban_view = None
    for v in views:
        if v.get("view_kind") == "kanban":
            kanban_view = v
            break

    if not kanban_view:
        return "No kanban view found for this project."

    buckets = _vikunja_request("GET", f"/projects/{project_id}/views/{kanban_view['id']}/buckets")

    if not buckets:
        return "No buckets found."

    lines = ["**Kanban Buckets:**", ""]
    for b in buckets:
        task_count = len(b.get("tasks", []))
        lines.append(f"- **{b.get('title', '?')}** (ID: {b['id']}) - {task_count} tasks")

    return "\n".join(lines)


@mcp.tool()
def create_bucket(
    project_id: int = Field(description="The project ID"),
    view_id: int = Field(description="The kanban view ID"),
    title: str = Field(description="Bucket title")
) -> str:
    """Create a new kanban bucket (column)."""
    bucket = _vikunja_request("PUT", f"/projects/{project_id}/views/{view_id}/buckets", json={
        "title": title
    })
    return f"Created bucket: {bucket.get('title')} (ID: {bucket['id']})"


# ============================================================================
# TASK RELATION TOOLS
# ============================================================================

@mcp.tool()
def create_task_relation(
    task_id: int = Field(description="The source task ID"),
    other_task_id: int = Field(description="The related task ID"),
    relation_kind: str = Field(
        default="related",
        description="Relation type: related, subtask, parenttask, blocking, blocked, duplicates, duplicateof"
    )
) -> str:
    """Create a relation between two tasks."""
    _vikunja_request("PUT", f"/tasks/{task_id}/relations", json={
        "other_task_id": other_task_id,
        "relation_kind": relation_kind
    })
    return f"Created {relation_kind} relation: task {task_id} -> task {other_task_id}"


@mcp.tool()
def list_task_relations(task_id: int = Field(description="The task ID")) -> str:
    """List all relations for a task."""
    task = _vikunja_request("GET", f"/tasks/{task_id}")

    relations = task.get("related_tasks", {})
    if not relations:
        return "No relations found for this task."

    lines = [f"**Relations for task {task_id}:**", ""]
    for relation_type, related_tasks in relations.items():
        if related_tasks:
            lines.append(f"**{relation_type}:**")
            for rt in related_tasks:
                lines.append(f"  - {rt.get('title', '?')} (ID: {rt['id']})")

    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
