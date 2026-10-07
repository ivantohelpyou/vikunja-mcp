"""
Tests for vikunja-mcp server.

Run the unit tests (no network, no Vikunja needed):

    uv run --extra dev pytest -q -k "not TestVikunjaConnection"

For the integration tests (against a real Vikunja), set:

    VIKUNJA_URL=https://your-instance.com
    VIKUNJA_TOKEN=your-token

and run `uv run --extra dev pytest -q`. The integration tests are read-only.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

# ============================================================================
# HELPERS AND FIXTURES
# ============================================================================


def list_tool_names() -> list:
    """The tool names a real MCP client sees from tools/list (in-memory transport)."""
    from fastmcp import Client
    from vikunja_mcp.server import mcp

    async def _list():
        async with Client(mcp) as client:
            return [t.name for t in await client.list_tools()]

    return asyncio.run(_list())


class FakeResponse:
    """The slice of requests.Response that server._request touches."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self.payload


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the server at an empty config dir and no Vikunja env, so nothing on the
    developer's machine (~/.vikunja-mcp, VIKUNJA_*) leaks into a test."""
    from vikunja_mcp import server

    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / "config.yaml")
    for var in ("VIKUNJA_URL", "VIKUNJA_TOKEN", "VIKUNJA_BOT_TOKEN", "VIKUNJA_INSTANCES"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def vikunja_configured():
    """Check if Vikunja credentials are configured."""
    url = os.environ.get("VIKUNJA_URL")
    token = os.environ.get("VIKUNJA_TOKEN")
    if not url or not token:
        pytest.skip("VIKUNJA_URL and VIKUNJA_TOKEN not set")
    return url, token


# ============================================================================
# UNIT TESTS (no network required)
# ============================================================================


class TestServerImport:
    """The package imports and identifies itself."""

    def test_import_package(self):
        import vikunja_mcp

        assert vikunja_mcp is not None

    def test_import_server(self):
        from vikunja_mcp import server

        assert callable(server.main)

    def test_server_identity(self):
        """A client's initialize handshake reports name 'vikunja' and the wheel's version."""
        from fastmcp import Client
        from vikunja_mcp.server import mcp

        async def _init():
            async with Client(mcp) as client:
                return client.initialize_result.serverInfo

        info = asyncio.run(_init())
        assert info.name == "vikunja"
        assert info.version not in ("", "unknown", None)


class TestToolsRegistered:
    """tools/list is the public surface: 81 tools, with these names."""

    # The 0.10.0 surface, grouped by prefix.
    EXPECTED_TOOLS = {
        "project": ["project_list", "project_list_all", "project_get", "project_create",
                    "project_update", "project_delete", "project_analyze", "project_setup",
                    "project_export", "project_import", "project_create_from_template"],
        "task": ["task_list", "task_get", "task_create", "task_update", "task_complete",
                 "task_delete", "task_move", "task_query", "task_add_label",
                 "task_assign_user", "task_unassign_user", "task_set_position",
                 "task_set_reminders", "task_create_relation", "task_list_relations"],
        "label": ["label_list", "label_create", "label_delete"],
        "kanban": ["kanban_get", "kanban_list_buckets", "kanban_create_bucket",
                   "kanban_delete_bucket", "kanban_setup", "kanban_sort_bucket",
                   "kanban_tasks_by_bucket"],
        "view": ["view_list", "view_create", "view_update", "view_delete",
                 "view_get_tasks", "view_set_position"],
        "batch": ["batch_create_tasks", "batch_update_tasks", "batch_create_labels",
                  "batch_relabel", "batch_assign_buckets", "batch_label_to_buckets",
                  "batch_move_by_label", "batch_complete_by_label", "batch_reorder_tasks"],
        "comment": ["comment_add", "comment_list", "comment_update", "comment_delete",
                    "comment_recent"],
        "instance": ["instance_list", "instance_connect", "instance_disconnect",
                     "instance_rename", "instance_switch", "instance_check_health"],
        "ctx_config": ["ctx_get", "ctx_set", "config_get", "config_set", "config_list",
                       "config_update", "config_delete"],
        "today_triage": ["today_actions", "today_snooze", "today_reckoning",
                         "today_get_weights", "today_set_weights", "triage_park",
                         "triage_parked", "assign_queue", "assign_apply"],
        "search_cal": ["search_all", "search_all_tasks", "cal_add_event"],
    }

    def test_tool_count(self):
        assert len(list_tool_names()) == 81

    def test_all_expected_tools_registered(self):
        names = set(list_tool_names())
        expected = {n for group in self.EXPECTED_TOOLS.values() for n in group}
        assert expected <= names, f"missing: {sorted(expected - names)}"
        # The expected list is the whole surface, not a sample.
        assert names == expected, f"unlisted tools: {sorted(names - expected)}"

    def test_tool_names_are_unique(self):
        names = list_tool_names()
        assert len(names) == len(set(names))

    def test_old_pre_0_10_names_are_gone(self):
        names = set(list_tool_names())
        for old in ("list_projects", "get_project", "create_project", "list_tasks",
                    "create_task", "complete_task", "list_labels", "add_label_to_task"):
            assert old not in names

    def test_every_tool_has_a_description_and_schema(self):
        from fastmcp import Client
        from vikunja_mcp.server import mcp

        async def _tools():
            async with Client(mcp) as client:
                return await client.list_tools()

        for tool in asyncio.run(_tools()):
            assert tool.description and tool.description.strip(), tool.name
            assert tool.inputSchema.get("type") == "object", tool.name


class TestFormatters:
    """The formatter helpers return plain dicts with a stable shape."""

    def test_format_task_shape(self):
        from vikunja_mcp.server import _format_task

        result = _format_task({"id": 123, "title": "Test Task", "done": False, "priority": 3})
        assert isinstance(result, dict)
        assert result["id"] == 123
        assert result["title"] == "Test Task"
        assert result["done"] is False
        assert result["priority"] == 3
        # Defaults for everything the API did not send.
        assert result["description"] == ""
        assert result["labels"] == []
        assert result["assignees"] == []
        assert result["reminders"] == []
        assert result["bucket_id"] == 0
        assert result["repeat_after"] == 0

    def test_format_task_done_flag(self):
        from vikunja_mcp.server import _format_task

        assert _format_task({"id": 1, "title": "Done Task", "done": True})["done"] is True

    def test_format_task_flattens_labels_assignees_reminders(self):
        from vikunja_mcp.server import _format_task

        result = _format_task({
            "id": 5, "title": "t", "project_id": 9,
            "labels": [{"id": 1, "title": "home", "hex_color": "ff0000"}],
            "assignees": [{"id": 2, "username": "ivan", "email": "x@example.com"}],
            "reminders": [{"reminder": "2026-01-01T09:00:00Z", "relative_period": 0}],
        })
        assert result["project_id"] == 9
        assert result["labels"] == [{"id": 1, "title": "home"}]
        assert result["assignees"] == [{"id": 2, "username": "ivan"}]
        assert result["reminders"] == ["2026-01-01T09:00:00Z"]

    def test_format_task_requires_id_and_title(self):
        from vikunja_mcp.server import _format_task

        with pytest.raises(KeyError):
            _format_task({"title": "no id"})

    def test_format_project_shape(self):
        from vikunja_mcp.server import _format_project

        result = _format_project({"id": 456, "title": "Test Project"})
        assert result == {
            "id": 456,
            "title": "Test Project",
            "description": "",
            "parent_project_id": 0,
            "hex_color": "",
            "is_favorite": False,
            "is_archived": False,
            "position": 0,
        }

    def test_format_project_keeps_given_values(self):
        from vikunja_mcp.server import _format_project

        result = _format_project({"id": 1, "title": "P", "parent_project_id": 7,
                                  "is_archived": True, "hex_color": "3498db"})
        assert result["parent_project_id"] == 7
        assert result["is_archived"] is True
        assert result["hex_color"] == "3498db"

    def test_format_label_shape(self):
        from vikunja_mcp.server import _format_label

        assert _format_label({"id": 3, "title": "urgent", "extra": "dropped"}) == {
            "id": 3, "title": "urgent", "hex_color": ""}

    def test_format_comment_shape(self):
        from vikunja_mcp.server import _format_comment

        result = _format_comment({"id": 8, "comment": "<p>hi</p>", "task_id": 4,
                                  "author": {"id": 2, "username": "ivan", "name": "Ivan"}})
        assert result["id"] == 8
        assert result["comment"] == "<p>hi</p>"
        assert result["author_username"] == "ivan"
        assert result["task_id"] == 4


class TestTextHelpers:
    def test_sanitize_title_strips_html(self):
        from vikunja_mcp.server import _sanitize_title

        assert "<" not in _sanitize_title("<b>Bold Title</b>")
        assert "Bold Title" in _sanitize_title("<b>Bold Title</b>")
        assert "script" not in _sanitize_title("<script>alert(1)</script>Test").lower()

    def test_md_to_html_converts_markdown(self):
        from vikunja_mcp.server import md_to_html

        assert md_to_html("**bold**") == "<p><strong>bold</strong></p>"

    def test_md_to_html_passes_html_through(self):
        from vikunja_mcp.server import md_to_html

        assert md_to_html("<p>already</p>") == "<p>already</p>"


class TestDeferLogic:
    """The deferral marker lives in the task description and survives round-trips."""

    def test_write_then_extract_roundtrip(self):
        from vikunja_mcp.defer_logic import _extract_defer_meta, _write_defer_meta

        state = {"defer_count": 2, "defer_reason": "dread", "deferred_until": "2026-07-20"}
        desc = _write_defer_meta("<p>Body</p>", state)
        assert desc.startswith("<p>Body</p>")
        assert _extract_defer_meta(desc) == state

    def test_clearing_marker_leaves_body(self):
        from vikunja_mcp.defer_logic import _extract_defer_meta, _write_defer_meta

        desc = _write_defer_meta("<p>Body</p>", {"defer_count": 1})
        cleared = _write_defer_meta(desc, {})
        assert cleared == "<p>Body</p>"
        assert _extract_defer_meta(cleared) == {}

    def test_malformed_marker_is_empty(self):
        from vikunja_mcp.defer_logic import _extract_defer_meta

        assert _extract_defer_meta("<!-- defer-meta: {not json} -->") == {}
        assert _extract_defer_meta(None) == {}

    def test_defer_count_ignores_bools_and_negatives(self):
        from vikunja_mcp.defer_logic import _defer_count

        assert _defer_count({"defer": {"defer_count": 3}}) == 3
        assert _defer_count({"defer": {"defer_count": True}}) == 0
        assert _defer_count({"defer": {"defer_count": -2}}) == 0
        assert _defer_count({"description": ""}) == 0


class TestTodayEngine:
    """The today_* scoring and clustering engine is pure; check what it decides."""

    NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    def _score(self, task):
        from vikunja_mcp.server import _TODAY_DEFAULT_WEIGHTS, _score_today_candidate

        return _score_today_candidate(task, self.NOW, dict(_TODAY_DEFAULT_WEIGHTS))

    def test_overdue_task_outscores_unscheduled(self):
        overdue = {"id": 1, "due_date": (self.NOW - timedelta(days=3)).isoformat()}
        floater = {"id": 2}
        s_over, why_over = self._score(overdue)
        s_floater, _ = self._score(floater)
        assert s_over > s_floater
        assert "3d overdue" in why_over

    def test_why_trace_is_a_list_of_strings(self):
        _, why = self._score({"id": 1, "due_date": (self.NOW - timedelta(days=1)).isoformat()})
        assert why and all(isinstance(w, str) for w in why)

    def test_task_kind(self):
        from vikunja_mcp.server import _task_kind

        assert _task_kind({"id": 1}) == "deed"
        assert _task_kind({"id": 1, "end_date": "2026-07-16T10:00:00Z"}) == "event"
        assert _task_kind({"id": 1, "end_date": "0001-01-01T00:00:00Z"}) == "deed"
        assert _task_kind({"id": 1, "labels": [{"title": "anno"}]}) == "occasion"

    def test_parse_vikunja_dt_zero_sentinel_is_none(self):
        from vikunja_mcp.server import _parse_vikunja_dt

        assert _parse_vikunja_dt("0001-01-01T00:00:00Z") is None
        assert _parse_vikunja_dt("") is None
        assert _parse_vikunja_dt("garbage") is None
        parsed = _parse_vikunja_dt("2026-07-15T10:00:00Z")
        assert parsed == self.NOW

    def test_snoozed_means_future_start_date(self):
        from vikunja_mcp.server import _is_snoozed

        assert _is_snoozed({"start_date": (self.NOW + timedelta(days=2)).isoformat()}, self.NOW)
        assert not _is_snoozed({"start_date": (self.NOW - timedelta(days=2)).isoformat()}, self.NOW)
        assert not _is_snoozed({}, self.NOW)

    def test_deferred_means_future_deferred_until(self):
        from vikunja_mcp.defer_logic import _write_defer_meta
        from vikunja_mcp.server import _is_deferred

        later = _write_defer_meta("", {"deferred_until": "2026-07-20"})
        past = _write_defer_meta("", {"deferred_until": "2026-07-01"})
        assert _is_deferred({"description": later}, self.NOW)
        assert not _is_deferred({"description": past}, self.NOW)

    def test_clusters_put_due_today_in_must_clear(self):
        from vikunja_mcp.server import _cluster_candidates

        task = {"id": 11, "title": "Pay rent", "instance": "default", "project_id": 1,
                "due_date": self.NOW.isoformat(), "score": 40, "why": ["due today"]}
        clusters = _cluster_candidates([task], self.NOW, {("default", 1): "Home"}, {})
        assert [c["intent"] for c in clusters] == ["must_clear"]
        item = clusters[0]["items"][0]
        assert item["task_id"] == "11"
        assert item["title"] == "Pay rent"
        assert item["project"] == "Home"
        assert item["kind"] == "deed"
        assert item["defer_count"] == 0

    def test_empty_candidates_make_no_clusters(self):
        from vikunja_mcp.server import _cluster_candidates

        assert _cluster_candidates([], self.NOW) == []

    def test_today_apply_is_honest_when_there_is_no_claim_store(self):
        from vikunja_mcp.server import _today_apply_impl

        assert _today_apply_impl(1)["error"] == "claiming_unavailable"

    def test_weights_default_and_reject_unknown(self, isolated_config):
        from vikunja_mcp.server import (_TODAY_DEFAULT_WEIGHTS, _set_today_action_weights,
                                        _today_action_weights)

        assert _today_action_weights() == _TODAY_DEFAULT_WEIGHTS
        result = _set_today_action_weights({"W_GOAL": 20, "W_BOGUS": 1, "W_QUICK": "x"})
        assert result["set"] == {"W_GOAL": 20}
        assert sorted(result["rejected"]) == ["W_BOGUS", "W_QUICK"]
        assert _today_action_weights()["W_GOAL"] == 20
        # Persisted to the (temporary) config file only.
        assert (isolated_config / "config.yaml").exists()

    def test_all_rejected_weights_write_nothing(self, isolated_config):
        from vikunja_mcp.server import _set_today_action_weights

        result = _set_today_action_weights({"W_BOGUS": 1})
        assert result["set"] == {}
        assert not (isolated_config / "config.yaml").exists()

    def test_park_roundtrip_uses_config_file(self, isolated_config):
        from vikunja_mcp.server import _triage_parked, _triage_set_parked

        assert _triage_parked() == set()
        assert _triage_set_parked(42, "work", True)["parked"] is True
        assert _triage_parked() == {"work:42"}
        _triage_set_parked(42, "work", False)
        assert _triage_parked() == set()


class TestStandaloneConfig:
    """The standalone server takes its Vikunja from VIKUNJA_URL / VIKUNJA_TOKEN only."""

    def test_no_url_means_error_not_a_hosted_default(self, isolated_config):
        from vikunja_mcp.server import _get_instance_config

        with pytest.raises(ValueError):
            _get_instance_config()

    def test_url_and_token_come_from_env(self, isolated_config, monkeypatch):
        from vikunja_mcp.server import _get_instance_config

        monkeypatch.setenv("VIKUNJA_URL", "http://localhost:3456/")
        monkeypatch.setenv("VIKUNJA_TOKEN", "tk_test")
        assert _get_instance_config() == ("http://localhost:3456", "tk_test")

    def test_bot_token_env_is_not_read(self, isolated_config, monkeypatch):
        from vikunja_mcp.server import _get_instance_config

        monkeypatch.setenv("VIKUNJA_URL", "http://localhost:3456")
        monkeypatch.setenv("VIKUNJA_BOT_TOKEN", "tk_bot")
        assert _get_instance_config() == ("http://localhost:3456", "")

    def test_instances_come_from_config_dir(self, isolated_config):
        import yaml
        from vikunja_mcp.server import _get_instances

        (isolated_config / "config.yaml").write_text(yaml.safe_dump({
            "instances": {"work": {"url": "https://work.example.com", "token": "tk_w"}}}))
        assert _get_instances()["work"]["url"] == "https://work.example.com"

    def test_request_hits_only_the_configured_url(self, isolated_config, monkeypatch):
        from vikunja_mcp import server

        monkeypatch.setenv("VIKUNJA_URL", "http://localhost:3456")
        monkeypatch.setenv("VIKUNJA_TOKEN", "tk_test")
        seen = {}

        def fake_request(method, url, headers=None, **kwargs):
            seen.update(method=method, url=url, auth=headers["Authorization"])
            return FakeResponse([{"id": 1, "title": "P"}])

        monkeypatch.setattr(server.requests, "request", fake_request)
        result = server._request("GET", "/api/v1/projects", allow_instance_fallback=True)
        assert result == [{"id": 1, "title": "P"}]
        assert seen == {"method": "GET", "url": "http://localhost:3456/api/v1/projects",
                        "auth": "Bearer tk_test"}

    def test_request_without_fallback_is_refused(self, isolated_config, monkeypatch):
        from vikunja_mcp import server

        monkeypatch.setenv("VIKUNJA_URL", "http://localhost:3456")
        monkeypatch.setenv("VIKUNJA_TOKEN", "tk_test")
        monkeypatch.setattr(server.requests, "request",
                            lambda *a, **k: pytest.fail("must not reach the network"))
        with pytest.raises(ValueError):
            server._request("GET", "/api/v1/projects")

    def test_api_errors_become_value_errors(self, isolated_config, monkeypatch):
        from vikunja_mcp import server

        monkeypatch.setenv("VIKUNJA_URL", "http://localhost:3456")
        monkeypatch.setenv("VIKUNJA_TOKEN", "tk_test")
        monkeypatch.setattr(server.requests, "request",
                            lambda *a, **k: FakeResponse({"message": "nope"}, 404))
        with pytest.raises(ValueError, match="not found"):
            server._request("GET", "/api/v1/projects/9", allow_instance_fallback=True)


class TestToolCallsThroughMCP:
    """Call tools the way a client does, with only the HTTP layer faked."""

    def _call(self, name, args, monkeypatch, payload):
        from fastmcp import Client
        from vikunja_mcp import server

        monkeypatch.setenv("VIKUNJA_URL", "http://localhost:3456")
        monkeypatch.setenv("VIKUNJA_TOKEN", "tk_test")
        monkeypatch.setattr(server.requests, "request",
                            lambda *a, **k: FakeResponse(payload))

        async def _go():
            async with Client(server.mcp) as client:
                return await client.call_tool(name, args)

        return asyncio.run(_go())

    def test_project_list_returns_formatted_projects(self, isolated_config, monkeypatch):
        result = self._call("project_list", {}, monkeypatch,
                            [{"id": 3, "title": "Kitchen", "extra": "dropped"}])
        text = "".join(block.text for block in result.content)
        assert "Kitchen" in text
        assert "dropped" not in text


# ============================================================================
# INTEGRATION TESTS (require a real Vikunja instance; read-only)
# ============================================================================


class TestVikunjaConnection:
    """Integration tests against a real Vikunja instance."""

    def test_projects(self, vikunja_configured):
        from vikunja_mcp.server import _request

        projects = _request("GET", "/api/v1/projects", allow_instance_fallback=True)
        assert isinstance(projects, list)
        if projects:
            assert "id" in projects[0]
            assert "title" in projects[0]

    def test_labels(self, vikunja_configured):
        from vikunja_mcp.server import _request

        assert isinstance(_request("GET", "/api/v1/labels", allow_instance_fallback=True), list)

    def test_get_project_and_its_tasks(self, vikunja_configured):
        from vikunja_mcp.server import _request

        projects = _request("GET", "/api/v1/projects", allow_instance_fallback=True)
        if not projects:
            pytest.skip("No projects to test")
        pid = projects[0]["id"]
        assert _request("GET", f"/api/v1/projects/{pid}",
                        allow_instance_fallback=True)["id"] == pid
        assert isinstance(_request("GET", f"/api/v1/projects/{pid}/tasks",
                                   allow_instance_fallback=True), list)


# ============================================================================
# SMOKE TEST (quick validation before publish)
# ============================================================================


class TestSmokeTest:
    """Quick smoke test to validate before publishing."""

    def test_smoke(self):
        """Import the package, list tools, run both formatters."""
        from vikunja_mcp.server import _format_project, _format_task

        assert len(list_tool_names()) == 81
        assert _format_task({"id": 1, "title": "Test"})["title"] == "Test"
        assert _format_project({"id": 1, "title": "Test"})["title"] == "Test"


# ============================================================================
# IMPORT FIDELITY (2026-10-06): an export of a board, imported, must come back the same.
# Found pushing a PDD board between instances: comments dropped, the last Kanban column's
# tasks ticked done, filtered views refused, every default view doubled.
# ============================================================================


class FakeVikunja:
    """Just enough of Vikunja for _import_all_projects_impl: ids, views, buckets, calls."""

    def __init__(self):
        self.calls, self.next = [], 1000
        self.views = {}   # project id -> [view dicts]
        self.done = {}    # task id -> done
        self.buckets = [] # (endpoint, title, new id), in the order made
        self.position = {}  # bucket id -> position, Vikunja's rule: 0 means id * 65536
        self.fail_view_posts = False

    def nid(self):
        self.next += 1
        return self.next

    def request(self, method, endpoint, **kw):
        body = kw.get("json") or {}
        self.calls.append((method, endpoint, body))
        parts = endpoint.strip("/").split("/")[2:]   # after api/v1
        if method == "PUT" and parts == ["labels"]:
            self.label_id = self.nid()
            return {"id": self.label_id, "title": body["title"]}
        if method == "PUT" and parts == ["projects"]:
            pid = self.nid()
            self.views[pid] = [{"id": self.nid(), "title": t, "view_kind": k, "project_id": pid}
                               for t, k in (("List", "list"), ("Gantt", "gantt"), ("Table", "table"), ("Kanban", "kanban"))]
            return {"id": pid, "title": body["title"]}
        if parts[:1] == ["projects"] and parts[2:] == ["views"] and method == "GET":
            return list(self.views[int(parts[1])])
        if parts[:1] == ["projects"] and parts[2:] == ["views"] and method == "PUT":
            v = {"id": self.nid(), **body}
            self.views[int(parts[1])].append(v)
            return v
        if parts[:1] == ["projects"] and len(parts) == 4 and parts[2] == "views" and method == "POST":
            if self.fail_view_posts and "bucket_configuration_mode" not in body:
                raise ValueError("400 Bad Request")
            return {"id": int(parts[3]), **body}
        if parts[:1] == ["projects"] and len(parts) == 4 and parts[2] == "views" and method == "DELETE":
            pid, vid = int(parts[1]), int(parts[3])
            self.views[pid] = [v for v in self.views[pid] if v["id"] != vid]
            return {}
        if parts[-1] == "buckets" and method == "GET":
            return []
        if parts[-1] == "buckets" and method == "PUT":
            bid = self.nid()
            self.buckets.append((endpoint, body["title"], bid))
            self.position[bid] = body.get("position") or bid * 65536
            return {"id": bid, **body, "position": self.position[bid]}
        if parts[:1] == ["projects"] and parts[-1] == "tasks" and method == "PUT":
            tid = self.nid()
            self.done[tid] = body.get("done", False)
            return {"id": tid, **body}
        return {}


@pytest.fixture
def fake_vikunja(monkeypatch):
    from vikunja_mcp import server
    fake = FakeVikunja()
    monkeypatch.setattr(server, "_request", fake.request)
    monkeypatch.setattr(server, "_fetch_all_pages", lambda *a, **k: [])
    return fake


def _board_export():
    """One board: a filtered List, a Kanban with no done column (A, B; task 2 in B, the
    LAST column), a Keep view filtered by label 26, and a comment on task 1."""
    return {
        "labels": [{"id": 26, "title": "keep", "hex_color": "047857"}],
        "projects": [{
            "id": 7, "title": "Saturday night", "parent_project_id": 0,
            "views": [
                {"id": 1, "title": "List", "view_kind": "list", "filter": "done = false"},
                {"id": 2, "title": "Kanban", "view_kind": "kanban", "done_bucket_id": 0, "default_bucket_id": 0,
                 "buckets": [{"id": 31, "title": "A", "task_ids": [1]}, {"id": 32, "title": "B", "task_ids": [2]}]},
                {"id": 3, "title": "Keep", "view_kind": "kanban", "filter": "labels in 26 && done = false",
                 "done_bucket_id": 0, "buckets": [{"id": 41, "title": "A", "task_ids": [1]}]},
            ],
            "tasks": [
                {"id": 1, "title": "Elliott Bay", "done": False, "labels": [{"id": 26}],
                 "comments": [{"id": 9, "comment": "<p><b>Keep</b></p><p>Serves: walkable</p>"}]},
                {"id": 2, "title": "Oddfellows", "done": False, "labels": []},
            ],
        }],
    }


class TestImportFidelity:
    def test_comments_written_by_the_export_are_imported(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        out = _import_all_projects_impl(_board_export())
        posted = [b["comment"] for m, e, b in fake_vikunja.calls if m == "PUT" and e.endswith("/comments")]
        assert posted == ["<p><b>Keep</b></p><p>Serves: walkable</p>"]
        assert out["comments_created"] == 1

    def test_no_done_column_unless_the_source_had_one(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        _import_all_projects_impl(_board_export())
        kanban_cfg = [b for m, e, b in fake_vikunja.calls
                      if m == "POST" and "/views/" in e and b.get("bucket_configuration_mode") == "manual"]
        assert kanban_cfg and all(b["done_bucket_id"] == 0 for b in kanban_cfg)
        assert not any(fake_vikunja.done.values())   # nothing arrived ticked done

    def test_a_source_done_column_maps_to_its_copy(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        data = _board_export()
        data["projects"][0]["views"][1]["done_bucket_id"] = 31   # column A was the done column
        _import_all_projects_impl(data)
        cfg = next(b for m, e, b in fake_vikunja.calls
                   if m == "POST" and b.get("title") == "Kanban" and "done_bucket_id" in b)
        kanban_a = next(bid for e, t, bid in fake_vikunja.buckets if t == "A")   # Kanban's columns come first
        assert cfg["done_bucket_id"] == kanban_a

    def test_filters_go_as_objects_with_label_ids_remapped(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        out = _import_all_projects_impl(_board_export())
        sent = [b["filter"] for m, e, b in fake_vikunja.calls if "/views" in e and b.get("filter")]
        assert sent and all(isinstance(f, dict) for f in sent)
        keep = [f["filter"] for f in sent if "labels" in f["filter"]]
        assert keep and all(f == f"labels in {fake_vikunja.label_id} && done = false" for f in keep)
        assert out["errors"] == []

    def test_default_views_are_reused_not_doubled(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        _import_all_projects_impl(_board_export())
        views = next(iter(fake_vikunja.views.values()))
        titles = sorted(v["title"] for v in views)
        assert titles == ["Kanban", "Keep", "List"]   # one each, Keep added, unused defaults gone

    def test_remap_filter_labels(self):
        from vikunja_mcp.server import _remap_filter_labels
        m = {26: 501, 27: 502}
        assert _remap_filter_labels("labels in 26 && done = false", m) == "labels in 501 && done = false"
        assert _remap_filter_labels("labels in 26, 27", m) == "labels in 501, 502"
        assert _remap_filter_labels("labels != 27", m) == "labels != 502"
        assert _remap_filter_labels("done = false", m) == "done = false"
        missing = []
        assert _remap_filter_labels("labels in 99", m, missing) == "labels in 0"   # unknown: matches nothing
        assert missing == [99]

    def test_a_reused_default_view_loses_its_own_filter_when_the_source_had_none(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        data = _board_export()
        del data["projects"][0]["views"][0]["filter"]   # the source's List shows done tasks too
        _import_all_projects_impl(data)
        list_post = next(b for m, e, b in fake_vikunja.calls if m == "POST" and b.get("title") == "List")
        assert list_post["filter"] == {"filter": ""}

    def test_a_failed_takeover_creates_the_view_instead(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        fake_vikunja.fail_view_posts = True
        out = _import_all_projects_impl(_board_export())
        titles = sorted(v["title"] for v in next(iter(fake_vikunja.views.values())))
        assert titles == ["Kanban", "Keep", "List"]   # each made once, the refused defaults gone
        assert any("reusing the default failed" in e for e in out["errors"])

    def test_default_views_the_source_did_not_have_are_removed(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        _import_all_projects_impl(_board_export())   # the source has List, Kanban, Keep: no Gantt, no Table
        titles = sorted(v["title"] for v in next(iter(fake_vikunja.views.values())))
        assert titles == ["Kanban", "Keep", "List"]

    def test_an_unknown_label_in_a_filter_matches_nothing_and_is_reported(self, fake_vikunja):
        from vikunja_mcp.server import _import_all_projects_impl
        data = _board_export()
        data["projects"][0]["views"][2]["filter"] = "labels in 99 && done = false"
        out = _import_all_projects_impl(data)
        sent = [b["filter"]["filter"] for m, e, b in fake_vikunja.calls if "/views" in e and b.get("filter")]
        assert "labels in 0 && done = false" in sent
        assert any("[99]" in e for e in out["errors"])


# ============================================================================
# Kanban column order. Found in the PDD setup test (2026-10-07): custom columns numbered from 0,
# and Vikunja reads 0 as unset, so the first column ("5 min") landed after the last ("Transit").
# ============================================================================


class TestColumnOrder:
    def _order(self, fake):
        return [t for _, t, bid in sorted(fake.buckets, key=lambda b: fake.position[b[2]])]

    def _project(self, fake):
        return fake.request("PUT", "/api/v1/projects", json={"title": "Saturday night"})["id"]

    def test_custom_titles_keep_their_order(self, fake_vikunja):
        from vikunja_mcp import server
        pid = self._project(fake_vikunja)
        server._setup_kanban_board_impl(project_id=pid, template="custom",
                                        custom_buckets=["5 min", "10 min", "15 min", "Transit"])
        assert self._order(fake_vikunja) == ["5 min", "10 min", "15 min", "Transit"]

    def test_custom_dicts_without_positions_keep_their_order(self, fake_vikunja):
        from vikunja_mcp import server
        pid = self._project(fake_vikunja)
        server._setup_kanban_board_impl(project_id=pid, template="custom",
                                        custom_buckets=[{"title": "Now"}, {"title": "Next", "limit": 3}, {"title": "Later"}])
        assert self._order(fake_vikunja) == ["Now", "Next", "Later"]
        assert [b["limit"] for _, _, b in fake_vikunja.calls if b.get("title") == "Next"] == [3]

    def test_explicit_positions_win(self, fake_vikunja):
        from vikunja_mcp import server
        pid = self._project(fake_vikunja)
        server._setup_kanban_board_impl(project_id=pid, template="custom",
                                        custom_buckets=[{"title": "B", "position": 20}, {"title": "A", "position": 10}])
        assert self._order(fake_vikunja) == ["A", "B"]

    def test_import_of_columns_without_positions_keeps_their_order(self, fake_vikunja):
        from vikunja_mcp import server
        data = _board_export()
        for v in data["projects"][0]["views"]:
            for b in v.get("buckets") or []:
                b.pop("position", None)
        server._import_all_projects_impl(data)
        kanban = [(t, bid) for ep, t, bid in fake_vikunja.buckets if t in ("A", "B")][:2]
        assert sorted(kanban, key=lambda x: fake_vikunja.position[x[1]])[0][0] == "A"

    def test_import_keeps_a_zero_position_column_last(self, fake_vikunja):
        from vikunja_mcp import server
        data = _board_export()
        kanban = next(v for v in data["projects"][0]["views"] if v["view_kind"] == "kanban")
        kanban["buckets"] = [{"id": 31, "title": "A", "position": 65536, "task_ids": [1]},
                             {"id": 32, "title": "B", "position": 0, "task_ids": [2]},
                             {"id": 33, "title": "C", "position": 196608, "task_ids": []}]
        server._import_all_projects_impl(data)
        made = [(t, bid) for ep, t, bid in fake_vikunja.buckets if t in ("A", "B", "C")][:3]
        assert [t for t, bid in sorted(made, key=lambda x: fake_vikunja.position[x[1]])] == ["A", "C", "B"]

    def test_import_reports_a_titleless_column_and_carries_on(self, fake_vikunja, monkeypatch):
        from vikunja_mcp import server
        real = fake_vikunja.request
        def strict(method, endpoint, **kw):   # Vikunja refuses a bucket with no title
            if method == "PUT" and endpoint.endswith("/buckets") and not (kw.get("json") or {}).get("title"):
                raise ValueError("400 Bad Request")
            return real(method, endpoint, **kw)
        monkeypatch.setattr(server, "_request", strict)
        data = _board_export()
        kanban = next(v for v in data["projects"][0]["views"] if v["view_kind"] == "kanban")
        kanban["buckets"].insert(0, {"id": 30, "task_ids": []})
        summary = server._import_all_projects_impl(data)
        assert any("Bucket 'None'" in e for e in summary["errors"])
        assert {"A", "B"} <= {t for _, t, _ in fake_vikunja.buckets}

