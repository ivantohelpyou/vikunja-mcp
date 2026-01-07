#!/bin/bash
# Publish vikunja-mcp to PyPI
#
# Usage:
#   ./scripts/publish.sh [--skip-tests]
#
# Requires:
#   - PYPI_TOKEN env var or ~/.pypirc configured
#   - VIKUNJA_URL and VIKUNJA_TOKEN for integration tests

set -e

cd "$(dirname "$0")/.."

echo "=== vikunja-mcp publish script ==="
echo ""

# Check for skip flag
SKIP_TESTS=false
if [[ "$1" == "--skip-tests" ]]; then
    SKIP_TESTS=true
    echo "⚠️  Skipping tests (--skip-tests)"
fi

# Run tests unless skipped
if [[ "$SKIP_TESTS" == "false" ]]; then
    echo "1. Running tests..."

    if [[ -z "$VIKUNJA_URL" || -z "$VIKUNJA_TOKEN" ]]; then
        echo "   ⚠️  VIKUNJA_URL/TOKEN not set - running unit tests only"
        uv run pytest tests/ -v -k "not TestVikunjaConnection"
    else
        echo "   Running full test suite (including integration tests)"
        uv run pytest tests/ -v
    fi
    echo ""
fi

# Check for private code patterns
echo "2. Checking for private code..."
if grep -rE "RBAC|ECO|slash_|credits|oauth|granter|tier_|_get_user|_set_user" src/vikunja_mcp/server.py; then
    echo "❌ ERROR: Private code patterns found!"
    exit 1
fi
echo "   ✅ No private patterns found"
echo ""

# Build
echo "3. Building package..."
rm -rf dist/
uv build
echo ""

# Show what will be published
echo "4. Package contents:"
unzip -l dist/*.whl | grep -E "\.py$|Name"
echo ""

# Confirm
read -p "5. Publish to PyPI? [y/N] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

# Publish
echo "6. Publishing..."
if [[ -n "$PYPI_TOKEN" ]]; then
    uv publish --token "$PYPI_TOKEN"
else
    uv publish
fi

echo ""
echo "✅ Published successfully!"
echo ""
echo "Don't forget to:"
echo "  git add -A && git commit -m 'Release vX.Y.Z' && git push"
