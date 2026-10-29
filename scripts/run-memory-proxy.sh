#!/data/data/com.termux/files/usr/bin/sh
# 记忆胶囊 · OpenAI 兼容记忆代理（Termux 保活启动）
#
#   sh scripts/run-memory-proxy.sh https://api.deepseek.com/v1
#   UPSTREAM=... PORT=8790 sh scripts/run-memory-proxy.sh
#
# 宿主 baseURL 填 http://127.0.0.1:8790/v1 即可。
# API key 仍由宿主带在 Authorization 头里，代理由此透传、不存不管。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DB="${BRAIN_DB:-$HOME/.brain-memory/memory.db}"
PORT="${PORT:-8790}"
UPSTREAM="${UPSTREAM:-}"
# 第一个位置参数是上游 URL 时必须先 shift，否则它会同时被当成 --upstream
# 的值和多余的位置参数，argparse 直接报错、代理起不来。
if [ -z "$UPSTREAM" ] && [ "$#" -gt 0 ]; then
  case "$1" in
    -*) : ;;
    *) UPSTREAM="$1"; shift ;;
  esac
fi

if [ -z "$UPSTREAM" ]; then
  echo "用法: sh scripts/run-memory-proxy.sh <上游 baseURL>" >&2
  echo "  例如: sh scripts/run-memory-proxy.sh https://api.deepseek.com/v1" >&2
  exit 2
fi

mkdir -p "$(dirname "$DB")"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
  echo "[brain-memory] wake-lock 已获取"
fi

echo "[brain-memory] 代理 http://127.0.0.1:$PORT/v1  ->  $UPSTREAM"
exec python3 "$ROOT/memory_proxy.py" --db "$DB" --upstream "$UPSTREAM" --port "$PORT" "$@"
