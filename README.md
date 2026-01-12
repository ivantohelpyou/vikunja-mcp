# vikunja-mcp

MCP server that gives Claude full access to your [Vikunja](https://vikunja.io) task management instance.

Works with **any Vikunja instance** — self-hosted, cloud, or [Factumerit](https://factumerit.com).

## Quick Start (Factumerit Users)

If you're using Factumerit's hosted Vikunja, the setup is automatic:

1. Your welcome message contains the complete config — just copy it
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

```json
{
  "mcpServers": {
    "vikunja": {
      "command": "uvx",
      "args": ["vikunja-mcp"],
      "env": {
        "VIKUNJA_URL": "https://your-vikunja-instance.com",
        "VIKUNJA_TOKEN": "your-api-token"
      }
    }
  }
}
```

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
- Or use our [restart script](scripts/restart-claude.ps1):
  ```powershell
  .\scripts\restart-claude.ps1
  ```

**Linux:** Close and reopen the app

### 5. Test it

Ask Claude:
> "What projects do I have in Vikunja?"

If it works, you'll see your projects listed!

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
- `assign_user` / `unassign_user` — Manage task assignments

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

- *"Show me all my tasks due this week"*
- *"Create a task to review the Q4 report in the Work project"*
- *"What's blocking the website redesign task?"*
- *"Move the 'Fix login bug' task to the Done column"*
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

Use Task Manager (Ctrl+Shift+Esc) to ensure all Claude processes are ended before reopening.

## Requirements

- Python 3.12+ (installed automatically by uv)
- A Vikunja instance with API access
- Claude Desktop (or any MCP-compatible client)

## Links

- [Vikunja](https://vikunja.io) — The open-source todo app
- [Factumerit](https://factumerit.com) — Managed Vikunja hosting with AI features
- [MCP Protocol](https://modelcontextprotocol.io) — Model Context Protocol
- [uv](https://docs.astral.sh/uv/) — Fast Python package manager

## License

MIT
