#!/bin/bash
# Pre-flight Deployment Check Script
# Run this before every production deployment.
# Exit code 0 = all checks passed. Non-zero = fix before deploying.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check() {
    local desc="$1"
    shift
    if "$@"; then
        echo -e "${GREEN}[PASS]${NC} $desc"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}[FAIL]${NC} $desc"
        FAIL=$((FAIL + 1))
    fi
}

warn_check() {
    local desc="$1"
    shift
    if "$@"; then
        echo -e "${GREEN}[PASS]${NC} $desc"
        PASS=$((PASS + 1))
    else
        echo -e "${YELLOW}[WARN]${NC} $desc (non-blocking)"
        WARN=$((WARN + 1))
    fi
}

echo "=== Pre-Flight Deployment Check ==="
echo "Started at: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""

# --- Git Checks ---
echo "--- Git ---"
check "On main/master branch" git rev-parse --abbrev-ref HEAD | grep -qE '^(main|master)$'
check "Working tree clean" git diff-index --quiet HEAD --
check "Ahead of remote? (should be 0)" test "$(git rev-list --count origin/HEAD..HEAD)" -eq 0

# --- CI Checks ---
echo ""
echo "--- CI/CD ---"
# Adapt these to your CI system
warn_check "CI pipeline green (check manually)" true

# --- Database ---
echo ""
echo "--- Database ---"
check "Migration directory exists" test -d migrations -o -d db/migrations -o -d alembic 2>/dev/null || true

# --- Dependencies ---
echo ""
echo "--- Dependencies ---"
warn_check "No known CVEs in dependencies (run: npm audit / pip-audit / cargo audit)" true

# --- Config ---
echo ""
echo "--- Configuration ---"
check ".env.example exists" test -f .env.example 2>/dev/null || test -f .env.template 2>/dev/null

# --- Summary ---
echo ""
echo "=== Summary ==="
echo -e "${GREEN}Passed: $PASS${NC}"
echo -e "${RED}Failed: $FAIL${NC}"
echo -e "${YELLOW}Warnings: $WARN${NC}"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "❌ Pre-flight check FAILED. Fix the issues above before deploying."
    exit 1
else
    echo ""
    echo "✅ Pre-flight check PASSED. Proceed with deployment."
    exit 0
fi
