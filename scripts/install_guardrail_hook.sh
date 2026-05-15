#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$ROOT/.git/hooks/pre-commit"

cat > "$HOOK" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
python3 scripts/guardrail_check.py --staged
EOF

chmod +x "$HOOK"
echo "Installed pre-commit hook: .git/hooks/pre-commit"
