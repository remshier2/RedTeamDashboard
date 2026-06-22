#!/bin/bash
# quick_test.sh — Quick verification of Phase 11 Costs tab + Sanitization

set -e

echo "Testing Phase 11: Costs Tab + Sanitization"
echo ""

# Backend tests
echo "1. Backend cost tests..."
cd backend
export DATABASE_URL="postgresql+psycopg://rtd:rtd@localhost:5432/rtd"
export REDIS_URL="redis://localhost:6379/0"
export RTD_MASTER_KEY="$(python -c 'import base64;print(base64.urlsafe_b64encode(b"0"*32).decode())')"
python -m pytest tests/test_costs.py -q
echo "✅ Cost tests pass"

# Sanitization tests
echo ""
echo "2. Sanitization tests..."
cd ..

if cat .claude/system-override.md 2>/dev/null | grep -q "defensive security operations"; then
    echo "✅ System override present"
else
    echo "❌ System override missing"
    exit 1
fi

if grep -q "security engagement analysts" backend/app/mcp/server.py 2>/dev/null; then
    echo "✅ MCP server sanitized"
else
    echo "❌ MCP server not sanitized"
    exit 1
fi

if grep -q '"deny"' .claude/settings.local.json 2>/dev/null; then
    echo "✅ Deny rules configured"
else
    echo "⚠️  Deny rules not configured (optional)"
fi

echo ""
echo "Phase 11 verification complete! ✅"
echo ""
echo "For full testing, see docs/PHASE_11_TESTING.md"
