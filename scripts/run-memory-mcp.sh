#!/data/data/com.termux/files/usr/bin/sh
# 记忆胶囊 · MCP 服务器（Termux 保活启动）
#
#   sh scripts/run-memory-mcp.sh              # 默认 HTTP :8788
#   PORT=9000 sh scripts/run-memory-mcp.sh
#   BRAIN_DB=/sdcard/mem.db sh scripts/run-memory-mcp.sh
#
# termux-wake-lock 会挂一个 wake lock，系统才不会在息屏后回收 python 进程。
# 要长期常驻，还得关掉这个 App 的电池优化（电池 → 应用启动管理）。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DB="${BRAIN_DB:-$HOME/.brain-memory/memory.db}"
PORT="${PORT:-8788}"

mkdir -p "$(dirname "$DB")"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
  echo "[brain-memory] wake-lock 已获取"
fi

echo "[brain-memory] 启动 HTTP 模式 http://127.0.0.1:$PORT/mcp"
echo "[brain-memory] 库: $DB"
exec python3 "$ROOT/mcp_server.py" --db "$DB" --http "$PORT" "$@"
