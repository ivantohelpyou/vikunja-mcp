# Changelog

## 0.10.0

The first release carrying the **reckoning** — the deterministic engine for deciding
what to do today — and the first release in which the published package matches the
source it was generated from.

**This release renames every tool.** If you are on 0.9.3, read §Migration.

### Added — the `today_*` family

Vikunja stores tasks; it has no opinion about which of them matters this morning. These
nine tools answer that, with no LLM in the scoring path — same tasks and same clock give
the same ranking every time.

| Tool | |
|---|---|
| `today_actions` | Scored, clustered candidates under named intents, each carrying a `why` trace |
| `today_apply` | Claim a task for today; the claim expires on its own tomorrow |
| `today_snooze` | Reason-aware deferral. A reason is required; deferred tasks escalate on return |
| `today_reset` | Clear today's claims early |
| `today_reckoning` | Only tasks deferred 3+ times — the chronic avoiders. Exists to kill things |
| `today_set_weights` / `today_get_weights` | Retune the ranking at runtime, per user, no code change |
| `triage_park` / `triage_parked` | Sweep a speculative project tree aside, reversibly |

The whole algorithm is documented in [docs/SCORING.md](docs/SCORING.md) — every weight,
both caps, the door-decay curve, the cluster rules, and worked examples you can
reproduce.

### Added — everything else

Comments (`comment_*`), batch operations (`batch_*`), search (`search_all`,
`search_all_tasks`), project templates, import/export, per-project config (`config_*`),
reminders, task moves, an assignment queue, calendar events (`cal_add_event`), and
instance health checks. **73 tools → 83.**

### Fixed

- **The package imports.** 0.9.3 could not be imported at all. This release is verified
  end to end: a clean install, then an MCP handshake that returns the full tool list.
- `markdown` and `cryptography` are now declared as dependencies. They were imported and
  undeclared, so installs could fail on first use.
- The server reports **its own version** in the MCP handshake. It previously reported the
  version of `fastmcp`.
- **22 error hints and docstring examples named tools that no longer existed** — an
  earlier rename updated the code but not the strings. A tool's docstring *is* the
  description a model reads, so these actively instructed it to call things that were not
  there; the "too many results" recovery path, for one, offered five suggestions that all
  named a removed tool.

### Removed — X-Q (Exchange Queue)

`check_xq`, `setup_xq`, `claim_xq_task` and `complete_xq_task` are gone with no
replacement.

They were never really a Vikunja integration. Every other tool here wraps a Vikunja
concept; these four imposed a handoff protocol — three specially-named kanban buckets and
a project id in config — that Vikunja knows nothing about and nothing enforces. It was
internal tooling that had outlived its use.

If you were using them, the protocol is trivial to rebuild on the general tools:
`kanban_create_bucket` for the buckets, `kanban_tasks_by_bucket` to see what's waiting,
`task_set_position` to move a card between them, `comment_add` to record where an item
was filed. That is all the removed tools did.

### Migration from 0.9.x

Tool names are now namespaced by object (`task_*`, `project_*`, `label_*`, …), and the
eight flat "power queries" collapsed into modes of a single `task_query`.

| 0.9.3 | 0.10.0 |
|---|---|
| `focus_now` | `task_query(query='focus')` |
| `due_today` | `task_query(query='today')` |
| `due_this_week` | `task_query(query='week')` |
| `overdue_tasks` | `task_query(query='overdue')` |
| `high_priority_tasks` | `task_query(query='high_priority')` |
| `urgent_tasks` | `task_query(query='urgent')` |
| `unscheduled_tasks` | `task_query(query='unscheduled')` |
| `task_summary` | `task_query(query='summary')` |
| `list_tasks` | `task_list` |
| `get_task` | `task_get` |
| `create_task` | `task_create` |
| `update_task` | `task_update` |
| `complete_task` | `task_complete` |
| `delete_task` | `task_delete` |
| `set_task_position` | `task_set_position` |
| `add_label_to_task` | `task_add_label` |
| `assign_user` | `task_assign_user` |
| `unassign_user` | `task_unassign_user` |
| `create_task_relation` | `task_create_relation` |
| `list_task_relations` | `task_list_relations` |
| `list_projects` | `project_list` |
| `get_project` | `project_get` |
| `create_project` | `project_create` |
| `delete_project` | `project_delete` |
| `list_labels` | `label_list` |
| `create_label` | `label_create` |
| `delete_label` | `label_delete` |
| `get_kanban_view` | `kanban_get` |
| `list_buckets` | `kanban_list_buckets` |
| `create_bucket` | `kanban_create_bucket` |
| `list_views` | `view_list` |
| `create_view` | `view_create` |
| `get_view_tasks` | `view_get_tasks` |
| `list_instances` | `instance_list` |
| `switch_instance` | `instance_switch` |
| `get_active_context` | `ctx_get` |
| `set_active_context` | `ctx_set` |

`task_query` also gained `query='upcoming'` (with `days=N`).

**Nothing in your Vikunja instance changes** — this is a rename of the MCP surface, not
of any stored data. Your config file needs no edits either. In practice Claude reads the
tool list on connect, so upgrading and restarting is usually the whole migration; the
table matters if you have written prompts, scripts, or notes that name tools explicitly.

## 0.9.3 and earlier

See the git history. Released January 2026; superseded by 0.10.0.
