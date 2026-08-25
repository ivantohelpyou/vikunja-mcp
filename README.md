# vikunja-mcp

An MCP server that gives Claude full access to your [Vikunja](https://vikunja.io)
instance — and, on top of Vikunja's API, a **deterministic engine for deciding what
to actually do today**.

Works with any Vikunja instance: self-hosted, cloud, or
[Factum Erit](https://factumerit.com).

---

## Install

```bash
# Claude Desktop config — see Setup below for the full block
uvx vikunja-mcp@latest
```

Requires a Vikunja instance and an API token. Full configuration in [Setup](#setup).

> **Upgrading from 0.9.x?** The tool surface was renamed and the whole `today_*` family
> is new. See [CHANGELOG.md](CHANGELOG.md) — the old power-query names are gone.

## What this does that a task API doesn't

Vikunja stores tasks. It has no opinion about which of them matters this morning.
Most "AI task manager" integrations answer *"what should I do today?"* by dumping the
task list into a model and letting it improvise a ranking — which is unrepeatable, and
gets more confident as it gets more wrong.

These tools answer it **deterministically**. No LLM in the scoring path.

Every ranked item carries a `why` trace naming the terms that fired — *"12d overdue ·
door closes in 3d · priority 3"* — so a ranking you disagree with can be argued with
rather than merely believed.

### `today_actions` — the daily question

Returns scored, clustered candidates under named intents — *Must clear*, *Quick wins*,
*Move a goal*, *Context batches*, *Been waiting* — each item carrying a numeric `score`
and a **`why` trace** explaining the score. Same inputs, same output, every time. Local
timezone throughout.

### `today_snooze` — deferral that costs something

Swipe-left requires a **reason**. The taxonomy matters: a deferral tagged `dread`
increments a counter and demands a date; deferrals past a task's `door_closes` are
rejected outright; the third `dread` defer raises `rule_of_three`. Deferred tasks
**escalate** on return rather than sinking quietly to the bottom. A bare snooze is
refused — the old snooze-to-tomorrow, which cost nothing and therefore meant nothing,
was retired.

### `today_reckoning` — the weekly kill list

Surfaces **only** tasks deferred N+ times (default 3): the chronic avoiders, loudest
first, each with its defer count, reason, history depth, and next return date.

This is not a to-do list. It exists to kill things. The allowed answers are
do / shrink / park / kill — deferring again is not among them.

### `triage_park` / `triage_parked` — read the desk, don't organize it

Park a whole speculative project tree aside in one gesture so the work that matters can
speak. Durable, reversible, per-user; never touches Vikunja, never deletes a task, never
marks anything done.

### `today_set_weights` / `today_get_weights` — argue with the ranking

If the ordering is wrong for you, retune the scoring weights at runtime — no code
change, no redeploy. Overrides are per-user and every later deterministic run honors
them.

Two of them have a ceiling, and it is worth knowing before you tune. The overdue and
deferral terms are per-unit and therefore **capped**, or a task four hundred days late
would score four thousand and own your list forever. Past the cap, raising those weights
changes nothing — at the default, anything more than a few days overdue is already
pinned there. Lowering them still works. The other seven are unbounded.

---

## Tools

81 tools. Names are namespaced by object, so `task_*`, `project_*`, and so on.

**Today / reckoning** — `today_actions`, `today_snooze`, `today_reckoning`,
`today_set_weights`, `today_get_weights`, `triage_park`, `triage_parked`

**Tasks** — `task_query`, `task_list`, `task_get`, `task_create`, `task_update`,
`task_complete`, `task_delete`, `task_move`, `task_set_position`, `task_set_reminders`,
`task_add_label`, `task_assign_user`, `task_unassign_user`, `task_create_relation`,
`task_list_relations`

**Projects** — `project_list`, `project_list_all`, `project_get`, `project_create`,
`project_update`, `project_delete`, `project_setup`, `project_analyze`,
`project_create_from_template`, `project_export`, `project_import`

**Batch** — `batch_create_tasks`, `batch_update_tasks`, `batch_relabel`,
`batch_move_by_label`, `batch_complete_by_label`, `batch_create_labels`,
`batch_assign_buckets`, `batch_label_to_buckets`, `batch_reorder_tasks`

**Kanban & views** — `kanban_get`, `kanban_setup`, `kanban_list_buckets`,
`kanban_create_bucket`, `kanban_delete_bucket`, `kanban_sort_bucket`,
`kanban_tasks_by_bucket`, `view_list`, `view_create`, `view_update`, `view_delete`,
`view_get_tasks`, `view_set_position`

**Labels** — `label_list`, `label_create`, `label_delete`

**Comments** — `comment_list`, `comment_add`, `comment_update`, `comment_delete`,
`comment_recent`

**Search** — `search_all`, `search_all_tasks`

**Instances** — `instance_list`, `instance_connect`, `instance_disconnect`,
`instance_switch`, `instance_rename`, `instance_check_health`, `ctx_get`, `ctx_set`

**Config** — `config_get`, `config_set`, `config_list`, `config_update`, `config_delete`

**Calendar** — `cal_add_event`

**Assignment queue** — `assign_queue`, `assign_apply`

> **Renamed since 0.9.3.** The PyPI release still uses the old flat names. `focus_now`,
> `due_today`, `due_this_week`, `overdue_tasks`, `high_priority_tasks`, `urgent_tasks`,
> `unscheduled_tasks` and `task_summary` are all now modes of a single
> `task_query(query=...)`; `list_tasks` → `task_list`, `create_task` → `task_create`,
> `list_projects` → `project_list`, and so on.

---

## Setup

Install [uv](https://docs.astral.sh/uv/):

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```
```powershell
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Get a Vikunja API token from **Settings → API Tokens → Create a token**, then add to
your Claude Desktop config:

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

Multiple instances, via `VIKUNJA_INSTANCES` (a JSON array) plus a default:

```json
"env": {
  "VIKUNJA_INSTANCES": "[{\"name\":\"personal\",\"url\":\"https://vikunja.example.com\",\"token\":\"tk_xxx\"},{\"name\":\"work\",\"url\":\"https://app.vikunja.cloud\",\"token\":\"tk_yyy\"}]",
  "VIKUNJA_DEFAULT_INSTANCE": "personal"
}
```

> **Tip:** `vikunja-mcp@latest` always resolves to the newest release. After upgrading,
> quit Claude Desktop fully and run `uv cache prune` if you still see the old tools.

| OS | Config file |
|----|-------------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/claude/claude_desktop_config.json` |

Then restart Claude Desktop completely — on macOS Cmd+Q and reopen; on Windows end every
`Claude` process in Task Manager first. Ask *"What projects do I have in Vikunja?"* to
confirm.

### Optional YAML config

`~/.vikunja-mcp/config.yaml` carries per-instance and per-project settings — default
buckets, sort strategies:

```yaml
instances:
  personal:
    url: https://vikunja.example.com
    token: tk_xxx

projects:
  '123':
    instance: work
    name: Sprint Board
    default_bucket: 📝 To Do
    sort_strategy:
      default: due_date
      buckets:
        "In Progress": start_date
```

---

## This file is generated

`src/vikunja_mcp/server.py` is extracted from a larger private server, keeping the
generic Vikunja surface and dropping everything tenant-specific.

**Edits to it are overwritten by the next extraction.** If something in it is wrong,
please open an issue rather than a PR against that file — the fix has to be made
upstream and re-extracted. Issues against everything else are normal PRs.

## Troubleshooting

**Tools don't appear.** Check the config file is valid JSON, that `uv` is on your PATH,
and that Claude Desktop was fully quit rather than just closed.

**"VIKUNJA_URL and VIKUNJA_TOKEN environment variables are required."** The `env` block
is missing or misspelled.

**401 Unauthorized.** The token was revoked or expired — issue a new one.

**Still on an old version.** Use `vikunja-mcp@latest`, then `uv cache prune` with Claude
Desktop closed.

## Requirements

Python 3.10+ (uv installs it), a Vikunja instance with API access, and Claude Desktop or
any MCP-compatible client.

## Links

- [Vikunja](https://vikunja.io) — the open-source todo app underneath
- [Factum Erit](https://factumerit.com) — managed Vikunja hosting, where these tools run
- [MCP](https://modelcontextprotocol.io) — the protocol
- [uv](https://docs.astral.sh/uv/) — the package manager

## License

MIT
