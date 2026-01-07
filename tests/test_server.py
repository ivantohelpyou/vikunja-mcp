"""
Tests for vikunja-mcp server.

Run with:
    cd ~/vikunja-mcp
    uv run pytest tests/ -v

For integration tests (against real Vikunja), set:
    VIKUNJA_URL=https://your-instance.com
    VIKUNJA_TOKEN=your-token
"""

import os
import pytest

# ============================================================================
# FIXTURES
# ============================================================================

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
    """Test that the server module can be imported."""

    def test_import_server(self):
        """Server module should import without errors."""
        from vikunja_mcp import server
        assert server is not None

    def test_import_mcp(self):
        """MCP instance should be importable."""
        from vikunja_mcp.server import mcp
        assert mcp is not None

    def test_mcp_has_tools(self):
        """MCP should have tools registered."""
        from vikunja_mcp.server import mcp
        tools = mcp._tool_manager._tools
        assert len(tools) > 0


class TestToolsRegistered:
    """Test that all expected tools are registered."""

    EXPECTED_TOOLS = [
        # Projects
        "list_projects",
        "get_project",
        "create_project",
        "update_project",
        "delete_project",
        # Tasks
        "list_tasks",
        "get_task",
        "create_task",
        "update_task",
        "complete_task",
        "delete_task",
        # Labels
        "list_labels",
        "create_label",
        "delete_label",
        "add_label_to_task",
        # Kanban
        "list_buckets",
        "create_bucket",
        # Relations
        "create_task_relation",
        "list_task_relations",
    ]

    def test_all_tools_registered(self):
        """All expected tools should be registered."""
        from vikunja_mcp.server import mcp

        tool_names = [t.name for t in mcp._tool_manager._tools.values()]

        for expected in self.EXPECTED_TOOLS:
            assert expected in tool_names, f"Missing tool: {expected}"

    def test_no_private_tools(self):
        """No private/internal tools should be exposed."""
        from vikunja_mcp.server import mcp

        tool_names = [t.name for t in mcp._tool_manager._tools.values()]

        private_patterns = [
            "slash_",  # Slack commands
            "oauth_",  # OAuth handlers
            "_user_",  # User management
            "credits",  # Billing
            "ECO",     # Slack gamification
        ]

        for tool in tool_names:
            for pattern in private_patterns:
                assert pattern not in tool, f"Private tool exposed: {tool}"

    def test_tool_count(self):
        """Should have exactly 19 tools."""
        from vikunja_mcp.server import mcp

        tools = mcp._tool_manager._tools
        assert len(tools) == 19, f"Expected 19 tools, got {len(tools)}"


class TestHelperFunctions:
    """Test helper functions."""

    def test_format_task(self):
        """_format_task should format a task dict."""
        from vikunja_mcp.server import _format_task

        task = {
            "id": 123,
            "title": "Test Task",
            "done": False,
            "priority": 3,
        }

        result = _format_task(task)
        assert "Test Task" in result
        assert "123" in result
        assert "High" in result  # priority 3 = High

    def test_format_task_completed(self):
        """_format_task should show checkmark for done tasks."""
        from vikunja_mcp.server import _format_task

        task = {"id": 1, "title": "Done Task", "done": True}
        result = _format_task(task)
        assert "✓" in result

    def test_format_project(self):
        """_format_project should format a project dict."""
        from vikunja_mcp.server import _format_project

        project = {"id": 456, "title": "Test Project"}
        result = _format_project(project)
        assert "Test Project" in result
        assert "456" in result


# ============================================================================
# INTEGRATION TESTS (require real Vikunja instance)
# ============================================================================

class TestVikunjaConnection:
    """Integration tests against real Vikunja instance."""

    def test_connection(self, vikunja_configured):
        """Should connect to Vikunja API."""
        from vikunja_mcp.server import _vikunja_request

        projects = _vikunja_request("GET", "/projects")
        assert isinstance(projects, list)

    def test_list_projects(self, vikunja_configured):
        """Should list projects."""
        from vikunja_mcp.server import _vikunja_request

        projects = _vikunja_request("GET", "/projects")
        assert len(projects) >= 0

        if projects:
            assert "id" in projects[0]
            assert "title" in projects[0]

    def test_list_labels(self, vikunja_configured):
        """Should list labels."""
        from vikunja_mcp.server import _vikunja_request

        labels = _vikunja_request("GET", "/labels")
        assert isinstance(labels, list)

    def test_get_project(self, vikunja_configured):
        """Should get a specific project."""
        from vikunja_mcp.server import _vikunja_request

        projects = _vikunja_request("GET", "/projects")
        if not projects:
            pytest.skip("No projects to test")

        project = _vikunja_request("GET", f"/projects/{projects[0]['id']}")
        assert project["id"] == projects[0]["id"]

    def test_list_tasks_from_project(self, vikunja_configured):
        """Should list tasks from a project."""
        from vikunja_mcp.server import _vikunja_request

        projects = _vikunja_request("GET", "/projects")
        if not projects:
            pytest.skip("No projects to test")

        tasks = _vikunja_request("GET", f"/projects/{projects[0]['id']}/tasks")
        assert isinstance(tasks, list)


# ============================================================================
# SMOKE TEST (quick validation before publish)
# ============================================================================

class TestSmokeTest:
    """Quick smoke test to validate before publishing."""

    def test_smoke(self):
        """Basic smoke test - import, check tools, no crashes."""
        from vikunja_mcp.server import mcp, _format_task, _format_project

        # Tools registered
        tools = mcp._tool_manager._tools
        assert len(tools) == 19

        # Formatters work
        assert "Test" in _format_task({"id": 1, "title": "Test"})
        assert "Test" in _format_project({"id": 1, "title": "Test"})

        print("✅ Smoke test passed")
