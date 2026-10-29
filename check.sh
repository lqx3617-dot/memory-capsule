#!/usr/bin/env sh
# brain_memory 检查脚本（三步）
#   1. 静态扫描：禁用依赖 / 危险模式 / schema 一致性 / journal action / CLI 完整性
#   2. 语法编译：compileall
#   3. 真建库体检：PRAGMA 断言 + API 存在性 + ingest->recall 往返
#   4. 可选 --full：跑 _smoke / _test_t4 / embed_demo
#
# 用法：
#   sh check.sh
#   sh check.sh --full
#   PYTHON=/path/to/python3 sh check.sh
#
# 说明：真正的检查逻辑在 _selfcheck.py，本脚本只做调度。
# 不把 Python 塞进 heredoc —— Android mksh 的 heredoc 会往 /data/local 写临时文件，
# PRoot 下通常没有权限。

PY="$PYTHON"
if [ -z "$PY" ]; then
  PY="python3"
fi
ROOT="$(cd "$(dirname "$0")" && pwd)"
FAILED=0
FULL=0

for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    -h|--help)
      echo "用法: sh check.sh [--full]"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 2
      ;;
  esac
done

ok()   { printf '  [OK]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAILED=$((FAILED + 1)); }
warn() { printf '  [WARN] %s\n' "$1"; }
step() { printf '\n== %s ==\n' "$1"; }

step "1/4 静态扫描"
if "$PY" "$ROOT/tests/selfcheck.py" --step scan; then
  ok "静态扫描通过"
else
  bad "静态扫描未通过"
fi

step "1b/4 文档代码块语法"
if "$PY" "$ROOT/tests/doccheck.py" | tee /dev/stderr | grep -q "问题 0 处"; then
  ok "文档里的 Python 代码块语法正确"
else
  warn "文档代码块存在语法问题（见上）"
fi

step "2/4 语法编译"
if "$PY" -m compileall -q "$ROOT/brain_memory" "$ROOT/examples" "$ROOT/app_integration.py" "$ROOT/记忆胶囊.py" >/dev/null 2>&1; then
  ok "compileall 通过（含中文别名入口）"
else
  bad "compileall 失败"
  "$PY" -m compileall "$ROOT/brain_memory" "$ROOT/examples" "$ROOT/app_integration.py" "$ROOT/记忆胶囊.py"
fi

step "3/4 真建库体检"
if "$PY" "$ROOT/tests/selfcheck.py" --step doctor; then
  ok "建库与往返正常"
else
  bad "建库体检未通过"
fi

if [ "$FULL" -eq 1 ]; then
  step "4/4 全量测试"
  for script in tests/test_smoke.py tests/test_t4.py tests/test_phase2.py tests/test_escalation.py examples/embed_demo.py \
    tests/test_gate_assertive.py tests/test_revive.py tests/test_connectivity.py tests/test_vec_window.py \
    tests/test_quality_live.py tests/test_call_name.py tests/test_vec_metric.py tests/test_mcp_server.py tests/test_memory_proxy.py tests/test_version.py tests/test_reindex.py tests/test_journal.py tests/test_indexes.py tests/test_concurrency.py tests/test_conflict.py tests/test_migration.py tests/test_dream.py; do
    if [ ! -f "$ROOT/$script" ]; then
      warn "$script 不存在，跳过"
      continue
    fi
    # 单独取退出码：有些失败是「测试自己说全部通过、退出码却非 0」，
    # 不打出来分不清是断言失败还是解释器/信号问题。
    # -X faulthandler：如果崩在 C 层（segfault / 断言失败），至少能看到栈
    ( cd "$ROOT" && "$PY" -X faulthandler "$script" >/dev/null 2>&1 )
    rc=$?
    if [ "$rc" -eq 0 ]; then
      ok "$script"
    else
      bad "$script (exit=$rc)"
      ( cd "$ROOT" && "$PY" -X faulthandler "$script" 2>&1 | tail -20 )
    fi
  done
else
  step "4/4 全量测试（--full 才跑）"
  warn "已跳过；加 --full 运行 _smoke / _test_t4 / embed_demo"
fi

printf '\n== 汇总 ==\n'
if [ "$FAILED" -eq 0 ]; then
  printf '  全部通过\n'
  exit 0
fi
printf '  失败 %d 项\n' "$FAILED"
exit 1
