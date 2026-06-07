#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

AGENT_REMOTE_NAME="hermes-agent-upstream"
AGENT_REMOTE_URL="https://github.com/NousResearch/hermes-agent.git"
AGENT_REMOTE_REF="main"
AGENT_PREFIX="vendor/hermes-agent"

WEBUI_REMOTE_NAME="hermes-webui-upstream"
WEBUI_REMOTE_URL="https://github.com/nesquena/hermes-webui"
WEBUI_REMOTE_REF="master"
WEBUI_PREFIX="vendor/hermes-webui"

run() {
  echo "+ $*"
  "$@"
}

fail() {
  echo "[sync] $*" >&2
  exit 1
}

ensure_clean_tree() {
  git diff --quiet || fail "working tree has unstaged changes"
  git diff --cached --quiet || fail "index has staged changes"
  if [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    fail "working tree has untracked files"
  fi
}

ensure_remote() {
  local name="$1"
  local url="$2"
  local current
  current="$(git remote get-url "$name" 2>/dev/null || true)"
  if [[ -z "$current" ]]; then
    run git remote add "$name" "$url"
    return
  fi
  if [[ "$current" != "$url" ]]; then
    fail "remote $name points to $current, expected $url"
  fi
}

# Abort any incomplete subtree merge left by a previous failed run.
if [[ -f ".git/MERGE_HEAD" ]]; then
  echo "[sync] incomplete merge detected — aborting before retry"
  git merge --abort
fi

ensure_clean_tree
ensure_remote "$AGENT_REMOTE_NAME" "$AGENT_REMOTE_URL"
ensure_remote "$WEBUI_REMOTE_NAME" "$WEBUI_REMOTE_URL"

run git fetch "$AGENT_REMOTE_NAME" "$AGENT_REMOTE_REF"
run git fetch "$WEBUI_REMOTE_NAME" "$WEBUI_REMOTE_REF"
run git subtree pull --prefix="$AGENT_PREFIX" "$AGENT_REMOTE_NAME" "$AGENT_REMOTE_REF" --squash

# Reset config.py to upstream before pulling webui so the patch commit from
# the previous sync does not conflict with upstream's own edits to that file.
WEBUI_CONFIG="${WEBUI_PREFIX}/api/config.py"
if git log -1 --format="%s" | grep -q "patch webui model" 2>/dev/null || \
   git log -1 HEAD -- "$WEBUI_CONFIG" --format="%s" | grep -q "patch webui model" 2>/dev/null; then
  echo "[sync] reverting patch commit on ${WEBUI_CONFIG} to avoid subtree conflict..."
  git checkout HEAD~1 -- "$WEBUI_CONFIG"
  git commit -m "chore(sync): revert webui model patch before upstream pull"
fi

run git subtree pull --prefix="$WEBUI_PREFIX" "$WEBUI_REMOTE_NAME" "$WEBUI_REMOTE_REF" --squash || {
  # If conflict only in config.py (expected), take upstream and re-patch.
  conflicts=$(git diff --name-only --diff-filter=U)
  if [[ "$conflicts" == "$WEBUI_CONFIG" ]]; then
    echo "[sync] conflict in ${WEBUI_CONFIG} — taking upstream version for re-patching"
    git checkout --theirs "$WEBUI_CONFIG"
    git add "$WEBUI_CONFIG"
    git commit --no-edit
  else
    echo "[sync] unexpected conflicts: $conflicts" >&2
    exit 1
  fi
}

echo
echo "[sync] patching vendor model lists from hermes-agent..."
python3 "${ROOT_DIR}/scripts/patch-vendor-models.py"
if ! git diff --quiet "$WEBUI_CONFIG"; then
  git add "$WEBUI_CONFIG"
  git commit -m "chore(sync): patch webui model list from hermes-agent"
fi

echo "[sync] upstream refresh complete"
echo "[sync] next steps:"
echo "  1. Review changes in vendor/ and root integration files"
echo "  2. Run ./scripts/smoke.sh"
echo "  3. Redeploy only after the smoke checks pass"
