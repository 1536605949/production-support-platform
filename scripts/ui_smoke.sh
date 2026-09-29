#!/usr/bin/env bash
# Run the UI smoke test, having first located jsdom.
#
# Why this wrapper exists
# ----------------------
# jsdom is not vendored into this repository — there is no package.json — and
# ``ui_smoke_test.mjs`` resolves it through CommonJS precisely so that NODE_PATH
# is honoured (ESM ``import`` ignores NODE_PATH entirely). The consequence was
# that the UI layer of ``release_gate.py`` depended on an ambient NODE_PATH that
# nothing set and nothing documented. In a clean shell the step exited 2 and the
# gate reported
#
#     命令失败（exit 2）: node scripts/ui_smoke_test.mjs https://.../
#
# which reads like a UI regression rather than a missing npm package. A gate whose
# verdict depends on variables exported in your shell is not a gate.
#
# So one place resolves jsdom, and everything that runs the test goes through it:
# ``make ui``, ``release_gate.py``, and a human. Search order, first hit wins:
#
#   1. jsdom already resolvable — a repo-local ``node_modules``, or ``NODE_PATH``
#   2. ``$JSDOM_HOME/node_modules``
#
# If neither works, the failure names all three fixes instead of leaking exit 2.
#
# Usage: bash scripts/ui_smoke.sh https://host/support [scripts/<page>_smoke.mjs]
#
# The second argument exists because this repository now serves *two* frontends
# with genuinely different contracts — the operator console (index.html) and the
# C-end page (customer.html). The C-end page must *not* render pipeline
# internals, so it needs its own assertions. Adding a second wrapper would
# duplicate the jsdom search below, and duplicated search logic is how one copy
# silently stops finding jsdom; so the page script is a parameter instead.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

die() { echo "UI 冒烟无法运行: $*" >&2; exit 2; }

URL="${1:-}"
PAGE_SCRIPT="${2:-scripts/ui_smoke_test.mjs}"
[ -n "$URL" ] || die '用法: scripts/ui_smoke.sh https://host/support [scripts/<page>_smoke.mjs]'
# The page is served under a path prefix; the trailing slash keeps relative
# fetches resolving against the page rather than its parent.
URL="${URL%/}/"

command -v node >/dev/null 2>&1 || die '找不到 node'

# ``node -e "require.resolve(...)"`` is the same resolution path the test itself
# uses, so agreeing with it here means agreeing with the test.
resolvable() {
    NODE_PATH="${1:-}" node -e "require.resolve('jsdom')" >/dev/null 2>&1
}

NODE_PATH_VALUE="${NODE_PATH:-}"
if [ -n "${JSDOM_HOME:-}" ] && [ -d "$JSDOM_HOME/node_modules/jsdom" ]; then
    NODE_PATH_VALUE="${NODE_PATH_VALUE:+$NODE_PATH_VALUE:}$JSDOM_HOME/node_modules"
fi

if ! resolvable "$NODE_PATH_VALUE"; then
    cat >&2 <<'MSG'
UI 冒烟无法运行：找不到 jsdom。

jsdom 没有随仓库提供（本仓库没有 package.json），需要显式告诉脚本它在哪。三选一：

  1) 在项目里装一份 —— 之后不需要任何环境变量
       npm i jsdom

  2) 指向一个已经装了 jsdom 的目录，脚本用它的 node_modules
       JSDOM_HOME=/path/to/node bash scripts/ui_smoke.sh <url>

  3) 直接给 NODE_PATH
       NODE_PATH=/path/to/node_modules bash scripts/ui_smoke.sh <url>

（ESM 的 import 不认 NODE_PATH，所以 ui_smoke_test.mjs 用 createRequire 加载 jsdom。）
MSG
    exit 2
fi

cd "$ROOT"
[ -f "$PAGE_SCRIPT" ] || die "找不到页面冒烟脚本: $PAGE_SCRIPT"
export NODE_PATH="$NODE_PATH_VALUE"
# The deployment uses a self-signed certificate; that is a known, documented
# limitation, not something this smoke test is meant to police.
export NODE_TLS_REJECT_UNAUTHORIZED=0
exec node "$PAGE_SCRIPT" "$URL"
