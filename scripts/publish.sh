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

# Check for private code patterns (hosted-server code that must not ship).
# Word boundaries matter: a bare "ECO" matches _TTL_SECONDS.
# Run just this gate against a file:  ./scripts/publish.sh --check-private [file]
check_private() {
    local file="$1" found=1
    # Case-sensitive: identifiers and acronyms.
    if grep -nE "\bRBAC\b|\bECO\b|\bslash_|\bcredits\b|\boauth\b|\bgranter\b|\btier_|\b_get_user|\b_set_user|\bslack_|\b_for_slack\b" "$file"; then
        found=0
    fi
    # Imports of modules that are not shipped in the package (private server modules).
    if grep -nE "^[[:space:]]*(from[[:space:]]+\.?(vikunja_mcp\.)?(token_broker|bot_provisioning|bot_jwt_manager|project_cloner|today_claims|deferrals|occasions|fe_tasks|routines|user_settings|vikunja_client|handoffs|label_cache)\b|from[[:space:]]+\.[[:space:]]+import[[:space:]]+.*\b(token_broker|bot_provisioning|bot_jwt_manager|project_cloner|today_claims|deferrals|occasions|fe_tasks|routines|user_settings|vikunja_client|handoffs|label_cache)\b|import[[:space:]]+(token_broker|bot_provisioning|bot_jwt_manager|project_cloner|today_claims|deferrals|occasions|fe_tasks|routines|user_settings|vikunja_client|handoffs|label_cache)\b)" "$file"; then
        found=0
    fi
    # Case-insensitive: names and phrases.
    if grep -niE "factumerit|factum erit commands|\beis bot\b|@eis\b|\bslack_|BOT_TOKEN|ADMIN_USER_IDS|DATABASE_URL|alembic" "$file"; then
        found=0
    fi
    return $found
}

if [[ "$1" == "--check-private" ]]; then
    if check_private "${2:-src/vikunja_mcp/server.py}"; then
        echo "FAIL: private code patterns found"
        exit 1
    fi
    echo "PASS: no private patterns found"
    exit 0
fi

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

echo "2. Checking for private code..."
if check_private src/vikunja_mcp/server.py; then
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
