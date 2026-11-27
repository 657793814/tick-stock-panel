#!/usr/bin/env bash
# 本地 CI 验证 — 与 .github/workflows/ci.yml 同口径, push 前自己跑一遍。
#
# 用法:
#   ./scripts/ci-check.sh                 # 全量: 后端测试 + 前端构建
#   ./scripts/ci-check.sh --backend       # 只跑后端
#   ./scripts/ci-check.sh --frontend      # 只跑前端
#   ./scripts/ci-check.sh --skip-install  # 跳过依赖安装 (依赖未变时提速)
#
# 与 CI 的差异:
#   - ci.yml 的 windows-mining-lock job 只在 Windows 跑 (原生锁/进程语义),
#     macOS/Linux 本地不模拟; 如需覆盖见脚本内 MINING_LOCK_TESTS。
#
# 任一步失败即以非零码退出, 失败摘要见末尾。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"

DO_BACKEND=1
DO_FRONTEND=1
SKIP_INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --backend)      DO_FRONTEND=0 ;;
    --frontend)     DO_BACKEND=0 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    -h|--help) grep '^#   ' "$0" | sed 's/^#   //'; exit 0 ;;
    *) echo "未知参数: $arg (用 -h 看用法)"; exit 2 ;;
  esac
done

# 与 ci.yml windows-mining-lock job 相同的用例集; 非 Windows 平台可选跑
MINING_LOCK_TESTS=(
  tests/test_mining_process_lock.py
  tests/test_mining_lock_admin.py
  tests/test_mining_lock_http.py
  tests/test_main_mining_lifespan.py
  tests/test_desktop_startup.py
  tests/test_spa_static_routes.py
  tests/test_data_dir_upgrade.py
  tests/test_macos_data_dir.py
  tests/test_windows_installer_contract.py
  tests/test_docker_data_dir.py
  tests/test_enriched_generation.py
)

FAILURES=()
STEP_START=""

step_begin() { STEP_START=$(date +%s); printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }
step_end() {
  local took=$(( $(date +%s) - STEP_START ))
  if [[ $1 -eq 0 ]]; then
    printf '\033[1;32m==> PASS (%ds)\033[0m\n' "$took"
  else
    printf '\033[1;31m==> FAIL (%ds)\033[0m\n' "$took"
    FAILURES+=("$2")
  fi
}

run_step() {  # run_step <标题> <失败标签> <命令...>
  local title="$1" label="$2"; shift 2
  step_begin "$title"
  if "$@"; then
    step_end 0 ""
  else
    step_end 1 "$label"
  fi
}

printf '\033[1m本地 CI 验证\033[0m (对齐 .github/workflows/ci.yml)\n'

# ---------- 本机环境预清理 ----------
# pytest 临时根 ($TMPDIR/pytest-of-*) 残留时, 本机注入的 Python 站点钩子会在
# mkdir 已存在目录时抛 FileExistsError, 曾导致上千用例 error (CI 无此问题)。
# 跑测试前清掉残留根目录 (pytest 自动重建, 只含临时产物, 可安全删除)。
step_begin "预清理: pytest 临时目录残留"
rm -rf "$TMPDIR"/pytest-of-* 2>/dev/null || true
step_end 0 ""

# ---------- backend job ----------
if [[ $DO_BACKEND -eq 1 ]]; then
  if [[ $SKIP_INSTALL -eq 0 ]]; then
    run_step "后端: 安装依赖 (uv sync --extra dev --frozen)" "backend-install" \
      bash -c "cd '$BACKEND_DIR' && uv sync --extra dev --frozen"
  fi
  run_step "后端: 全量测试 (uv run pytest tests -q)" "backend-tests" \
    bash -c "cd '$BACKEND_DIR' && uv run pytest tests -q"

  # 非 Windows 本地可选: windows-mining-lock job 的用例集 (CI 仅 Windows 执行)
  if [[ "$(uname -s)" != MINGW* && "$(uname -s)" != CYGWIN* ]]; then
    run_step "后端(可选): mining lock 用例集 (CI 仅 Windows 强制)" "mining-lock-tests" \
      bash -c "cd '$BACKEND_DIR' && uv run pytest ${MINING_LOCK_TESTS[*]} -q"
  fi
fi

# ---------- frontend job ----------
if [[ $DO_FRONTEND -eq 1 ]]; then
  if [[ $SKIP_INSTALL -eq 0 ]]; then
    run_step "前端: 安装依赖 (pnpm install --frozen-lockfile)" "frontend-install" \
      bash -c "cd '$FRONTEND_DIR' && pnpm install --frozen-lockfile"
  fi
  run_step "前端: 构建 (pnpm build = tsc -b + vite)" "frontend-build" \
    bash -c "cd '$FRONTEND_DIR' && pnpm build"
fi

# ---------- 摘要 ----------
printf '\n\033[1m========== 摘要 ==========\033[0m\n'
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  printf '\033[1;32m全部通过 ✓\033[0m\n'
  exit 0
fi
printf '\033[1;31m失败步骤:\033[0m\n'
for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
exit 1
