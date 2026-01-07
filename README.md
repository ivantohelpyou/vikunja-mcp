# vikunja-mcp

MCP server that gives Claude full access to your [Vikunja](https://vikunja.io) task management instance.

Works with **any Vikunja instance** — self-hosted, cloud, or local.

## Installation

```bash
pip install vikunja-mcp
# or
uv add vikunja-mcp
```

## Configuration

### 1. Get your Vikunja API token

Go to your Vikunja instance → Settings → API Tokens → Create a token.

### 2. Configure Claude Desktop

Add to your Claude Desktop config (`~/.config/claude/claude_desktop_config.json` on Linux, `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "vikunja": {
      "command": "vikunja-mcp",
      "env": {
        "VIKUNJA_URL": "https://your-vikunja-instance.com",
        "VIKUNJA_TOKEN": "your-api-token"
      }
    }
  }
}
```

### 3. Restart Claude Desktop

The Vikunja tools will now be available in Claude.

## Available Tools

### Projects
- `list_projects` — List all projects
- `get_project` — Get project details
- `create_project` — Create new project
- `delete_project` — Delete project

### Tasks
- `list_tasks` — List tasks (with filters)
- `get_task` — Get task details with labels/assignees
- `create_task` — Create task with title, description, due date, priority
- `update_task` — Update task fields
- `complete_task` — Mark task as done
- `delete_task` — Delete task
- `set_task_position` — Move task to kanban bucket
- `add_label_to_task` — Attach label to task
- `assign_user` — Assign user to task
- `unassign_user` — Remove user from task

### Labels
- `list_labels` — List all labels
- `create_label` — Create new label with color
- `delete_label` — Delete label

### Kanban
- `get_kanban_view` — Get kanban view ID for a project
- `list_buckets` — List kanban columns
- `create_bucket` — Create new kanban column

### Relations
- `create_task_relation` — Link tasks (blocking, subtask, etc.)
- `list_task_relations` — List task dependencies

## Usage Examples

Once configured, just ask Claude:

- "Show me all my tasks due this week"
- "Create a task to review the Q4 report in the Work project"
- "What's blocking the website redesign task?"
- "Move the 'Fix login bug' task to the Done column"
- "List all high-priority tasks across all projects"

## Requirements

- Python 3.12+
- A Vikunja instance with API access
- Claude Desktop (or any MCP-compatible client)

## Links

- [Vikunja](https://vikunja.io) — The open-source todo app
- [MCP Protocol](https://modelcontextprotocol.io) — Model Context Protocol
- [Source Code](https://github.com/ivantohelpyou/vikunja-mcp)

## License

MIT
