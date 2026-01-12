# vikunja-mcp

MCP server that gives Claude full access to your [Vikunja](https://vikunja.io) task management instance.

Works with **any Vikunja instance** — self-hosted, cloud, or [Factumerit](https://factumerit.com).

## Features

- **Multi-instance support** — Connect multiple Vikunja accounts (personal, work, etc.)
- **Power queries** — "What's overdue?", "Focus mode", "Due this week"
- **X-Q (Exchange Queue)** — Hand off tasks between Claude Desktop and Claude Code
- **Full Vikunja API** — Projects, tasks, labels, kanban boards, relations

## Quick Start (Factumerit Users)

If you're using Factumerit's hosted Vikunja, the setup is automatic:

1. Your welcome email contains the complete config — just copy it
2. Paste into your Claude Desktop config file (see [Config File Location](#config-file-location))
3. [Restart Claude Desktop](#restarting-claude-desktop)
4. Ask Claude: *"What's on my todo list?"*

## Manual Setup

### 1. Install uv

[uv](https://docs.astral.sh/uv/) is a fast Python package manager. Install it once:

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Get your Vikunja API token

Go to your Vikunja instance → **Settings** → **API Tokens** → **Create a token**.

Give it a name (e.g., "Claude Desktop") and grant all permissions.

### 3. Configure Claude Desktop

Add to your Claude Desktop config file:

**Single instance:**
```json
{
  "mcpServers": {
    "vikunja": {
      "command": "uvx",
      "args": ["vikunja-mcp@latest"],
      "env": {
        "VIKUNJA_URL": "https://your-vikunja-instance.com",
        "VIKUNJA_TOKEN": "your-api-token"
      }
    }
  }
}
```

**Multiple instances:**
```json
{
  "mcpServers": {
    "vikunja": {
      "command": "uvx",
      "args": ["vikunja-mcp@latest"],
      "env": {
        "VIKUNJA_INSTANCES": "[{\"name\": \"personal\", \"url\": \"https://vikunja.example.com\", \"token\": \"tk_xxx\"}, {\"name\": \"work\", \"url\": \"https://app.vikunja.cloud\", \"token\": \"tk_yyy\"}]",
        "VIKUNJA_DEFAULT_INSTANCE": "personal"
      }
    }
  }
}
```

> **Tip:** Use `vikunja-mcp@latest` to always get the newest version.

### Config File Location

| OS | Path |
|----|------|
| **macOS** | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Windows** | `%APPDATA%\Claude\claude_desktop_config.json` |
| **Linux** | `~/.config/claude/claude_desktop_config.json` |

**Tip:** If the file doesn't exist, create it with just the config above.

### 4. Restart Claude Desktop

**macOS:** Cmd+Q, then reopen

**Windows:** Either:
- Close Claude, open Task Manager (Ctrl+Shift+Esc), end any "Claude" processes, reopen
- Or run: `uv cache prune` then reopen Claude

**Linux:** Close and reopen the app

### 5. Test it

Ask Claude:
> "What projects do I have in Vikunja?"

If it works, you'll see your projects listed!

## Available Tools

### Power Queries (Fast)
- `focus_now` — High priority + overdue tasks (best for "what should I work on?")
- `due_today` — Tasks due today + overdue
- `due_this_week` — Tasks due in 7 days
- `overdue_tasks` — Past-due tasks only
- `high_priority_tasks` — Priority 3+ tasks
- `urgent_tasks` — Priority 4+ (critical) tasks
- `unscheduled_tasks` — Tasks without due dates
- `task_summary` — Quick counts (no task details)

### Instance Management
- `list_instances` — Show all configured instances
- `switch_instance` — Change active instance
- `get_active_context` / `set_active_context` — Get/set default instance
- `connect_instance` — Add a new instance

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
- `assign_user` / `unassign_user` — Manage task assignments

### Labels
- `list_labels` — List all labels
- `create_label` — Create new label with color
- `delete_label` — Delete label

### Kanban
- `get_kanban_view` — Get kanban view ID for a project
- `list_buckets` — List kanban columns
- `create_bucket` — Create new kanban column

### Views
- `list_views` — List views for a project
- `create_view` — Create list/kanban/gantt/table view
- `get_view_tasks` — Get tasks via a specific view

### Relations
- `create_task_relation` — Link tasks (blocking, subtask, etc.)
- `list_task_relations` — List task dependencies

### X-Q (Exchange Queue)
- `check_xq` — Check for pending handoff items
- `setup_xq` — Initialize X-Q project with proper buckets
- `claim_xq_task` — Claim a task for processing
- `complete_xq_task` — Mark task as filed with destination

## Usage Examples

Once configured, just ask Claude:

- *"What needs my attention?"* (uses focus_now)
- *"What's due this week?"*
- *"Show me all my tasks due this week"*
- *"Create a task to review the Q4 report in the Work project"*
- *"What's blocking the website redesign task?"*
- *"Move the 'Fix login bug' task to the Done column"*
- *"Switch to my work instance"*
- *"List all high-priority tasks across all projects"*

## Troubleshooting

### "No MCP servers found" or tools not appearing

1. Check your config file syntax (valid JSON?)
2. Ensure `uv` is installed and in your PATH
3. Restart Claude Desktop completely (see above)

### "VIKUNJA_URL and VIKUNJA_TOKEN environment variables are required"

Your config is missing the `env` section. Make sure it looks like:
```json
"env": {
  "VIKUNJA_URL": "https://...",
  "VIKUNJA_TOKEN": "tk_..."
}
```

### "401 Unauthorized" errors

Your API token may have expired or been revoked. Create a new one in Vikunja Settings → API Tokens.

### Windows: Claude won't restart properly

Use Task Manager (Ctrl+Shift+Esc) to ensure all Claude processes are ended before reopening. Or run `uv cache prune` to clear cached environments.

### Not getting latest version

Use `vikunja-mcp@latest` in your args, and run `uv cache prune` after closing Claude Desktop.

## Requirements

- Python 3.10+ (installed automatically by uv)
- A Vikunja instance with API access
- Claude Desktop (or any MCP-compatible client)

## Links

- [Vikunja](https://vikunja.io) — The open-source todo app
- [Factumerit](https://factumerit.com) — Managed Vikunja hosting with AI features
- [MCP Protocol](https://modelcontextprotocol.io) — Model Context Protocol
- [uv](https://docs.astral.sh/uv/) — Fast Python package manager

## License

MIT
