#!/usr/bin/env bash
# 一键安装 AutoLoop 监工插件：GrokBot 做监工，PI/dsh 等编码 CLI 做 worker。
# 技能目录不代表 worker 选择；worker 仍由 AUTOLOOP_WORKER_CMD / 项目配置指定。
# 云机请显式提供 GrokBot 可读取的技能目录；本脚本不猜测 Grok 私有目录。
#
#   bash bootstrap.sh                       # 安装 + 探测环境 + 对当前目录跑 preflight
#   bash bootstrap.sh /path/to/目标仓库      # 同上，位置参数也接受
#   bash bootstrap.sh --repo /path/to/仓库   # 同上，长选项
#   bash bootstrap.sh --skills-dir '/srv/bot skills' --repo /path/to/仓库
#   AUTOLOOP_SKILLS_DIR=/srv/skills bash bootstrap.sh
#   bash bootstrap.sh --install              # 额外打印缺失工具的安装命令（只打印）
#   AUTOLOOP_TEST_CMD='npm test' bash bootstrap.sh /path/to/仓库
#   FORCE=1 bash bootstrap.sh               # 内容不一致时强制刷新（也可用 --force）
#   bash bootstrap.sh --help                # 只显示帮助，不安装
#
# 技能目录优先级：--skills-dir > AUTOLOOP_SKILLS_DIR > DSH_SKILLS > ~/.dsh/skills。
# --skills-dir 缺值/空值、选中的 AUTOLOOP_SKILLS_DIR 空值会在复制前报错（退出码 2）。
# 可重复执行：内容一致时跳过；更新前备份旧版本。显式配置技能目录时，备份根默认
# 为该目录同级的 skills-backup；未配置时仍为 ~/.dsh/skills-backup。
# DSH_SKILLS_BACKUP 可覆盖备份根目录；DSH_SKILLS 保持兼容。
#
# 这个脚本不装后台进程、不改 git 配置、不碰 GitHub、不替你 apt-get。
# 它只复制技能文件、检查环境、告诉你下一步命令；--install 只打印安装建议。
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_ROOT="${AUTOLOOP_SKILLS_DIR-${DSH_SKILLS:-$HOME/.dsh/skills}}"
TARGET=""
PLAN=0
TEST_CMD="${AUTOLOOP_TEST_CMD:-}"
PY="${PYTHON:-python3}"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)  TARGET="${2:-}"; if [ $# -ge 2 ]; then shift 2; else shift; fi ;;
    --skills-dir)
      if [ $# -lt 2 ] || [ -z "${2:-}" ] || [[ "${2:-}" == -* ]]; then
        echo "!! --skills-dir 需要非空路径参数" >&2
        exit 2
      fi
      DEST_ROOT="$2"
      shift 2 ;;
    --install|--install-yes|--yes) PLAN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,/^set -uo pipefail/{ /^set -uo pipefail/d; p; }' "${BASH_SOURCE[0]}"; exit 0 ;;
    --*) echo "!! 未知参数: $1（--skills-dir <路径> / --repo <路径> / --install / --force / --help）" >&2; exit 2 ;;
    *)   TARGET="$1"; shift ;;
  esac
done
if [ -z "$DEST_ROOT" ]; then
  echo "!! AUTOLOOP_SKILLS_DIR 需要非空路径" >&2
  exit 2
fi
while [ "$DEST_ROOT" != / ] && [[ "$DEST_ROOT" == */ ]]; do
  DEST_ROOT="${DEST_ROOT%/}"
done
DEST="$DEST_ROOT/grokbot-autoloop"
BACKUP_ROOT="${DSH_SKILLS_BACKUP:-$(dirname -- "$DEST_ROOT")/skills-backup}"
RAW_TARGET="${TARGET:-${AUTOLOOP_REPO:-$PWD}}"
TARGET="$(cd "$RAW_TARGET" 2>/dev/null && pwd)" || {
  echo "!! 目标仓库路径不存在: $RAW_TARGET —— 用 --repo <真实路径>，或先 cd 进仓库" >&2
  exit 2
}

echo "AutoLoop bootstrap"
echo "  源: $SRC"
echo "  目标仓库: $TARGET"

for need in git "$PY"; do
  command -v "$need" >/dev/null 2>&1 || { echo "!! 缺少 $need，先装它（§八 禁止脚本自动重装环境）"; exit 2; }
done

# env.manifest 是工具清单（TAB 分隔）。这里只探测、只打印，不执行 install_hint。
if [ -f "$SRC/env.manifest" ]; then
  echo
  echo "── 环境探测（env.manifest；required 缺了跑不了循环，pr 缺了只能出分支不能出 PR）"
  while IFS=$'\t' read -r alo_name alo_req alo_check alo_hint alo_why; do
    case "$alo_name" in ''|\#*|name) continue ;;
    esac
    if sh -c "$alo_check" >/dev/null 2>&1; then
      printf '  [ok]   %-9s %s\n' "$alo_name" "$alo_req"
    else
      printf '  [缺]   %-9s %s — %s\n' "$alo_name" "$alo_req" "$alo_why"
      if [ "$PLAN" = 1 ] && [ -n "$alo_hint" ] && [ "$alo_hint" != "—" ]; then
        printf '         → 由你执行: %s\n' "$alo_hint"
      fi
    fi
  done < "$SRC/env.manifest"
  if [ "$PLAN" != 1 ]; then
    echo "  （加 --install 才打印缺失项的安装命令；本脚本不会替你执行它们）"
  fi
fi

changed=0
if [ -d "$DEST" ]; then
  if diff -rq --exclude=.git --exclude=__pycache__ --exclude=.autoloop "$SRC" "$DEST" \
      >/dev/null 2>&1; then
    echo "  已安装且内容一致，跳过复制（FORCE=1 可强制刷新）"
  else
    mkdir -p "$BACKUP_ROOT"
    ts="$(date +%Y%m%d-%H%M%S)"
    cp -R "$DEST" "$BACKUP_ROOT/grokbot-autoloop.$ts"
    echo "  内容有变化：旧版本备份到 $BACKUP_ROOT/grokbot-autoloop.$ts"
    changed=1
  fi
fi
if [ ! -d "$DEST" ] || [ "$changed" = 1 ] || [ "${FORCE:-0}" = 1 ]; then
  mkdir -p "$DEST_ROOT"
  rsync -a --delete --exclude .git --exclude __pycache__ --exclude .autoloop \
    "$SRC/" "$DEST/" 2>/dev/null || {
    # 没有 rsync 就走 cp -R（先清掉旧目录里的托管文件）
    rm -rf "$DEST"; mkdir -p "$DEST"; cp -R "$SRC/." "$DEST/"
  }
  echo "  已安装 → $DEST"
fi

# 技能文件必须可执行，否则 tick 跑不了闸门
chmod +x "$DEST/scripts/autoloop.py" 2>/dev/null || true
[ -f "$DEST/tests/selftest.py" ] && chmod +x "$DEST/tests/selftest.py" 2>/dev/null || true
[ -f "$DEST/tests/bin/dsh" ] && chmod +x "$DEST/tests/bin/dsh" 2>/dev/null || true
[ -f "$DEST/tests/fake_worker.py" ] && chmod +x "$DEST/tests/fake_worker.py" 2>/dev/null || true
[ -f "$DEST/tests/fake_gh.py" ] && chmod +x "$DEST/tests/fake_gh.py" 2>/dev/null || true
[ -f "$DEST/install-cron.sh" ] && chmod +x "$DEST/install-cron.sh" 2>/dev/null || true

ENGINE="$DEST/scripts/autoloop.py"

echo
echo "── 前置体检（只读：缺什么给什么命令，本脚本不会替你修 GitHub）"
"$PY" "$ENGINE" --repo "$TARGET" preflight || {
  rc=$?
  echo
  echo "preflight 退出码 $rc —— 上面的 → 后面就是需要你自己做的步骤。"
  echo "典型缺口：git 身份、SSH key 未加到 GitHub、gh 未登录、没配 worker CLI。"
  exit "$rc"
}

echo
echo "── 初始化目标仓库（幂等；已存在则保留你的 config/policy/queue）"
if [ -n "$TEST_CMD" ]; then
  "$PY" "$ENGINE" --repo "$TARGET" init --test-cmd "$TEST_CMD" || exit 1
else
  "$PY" "$ENGINE" --repo "$TARGET" init
  echo "   （没给 AUTOLOOP_TEST_CMD，闸门里的 test 还是空的 —— 空闸门永远不可能变绿，"
  echo "     补一条：$PY $ENGINE --repo $TARGET init --test-cmd '<仓库真实测试命令>' --force-gate）"
fi

echo
echo "── 下一步（在你自己的 agent 会话里，不需要定时任务）"
cat <<EOF
1. 每轮开发前先跑：$PY $ENGINE --repo $TARGET round
   它会打印 $DEST/PROTOCOL.md 的路径和 sha256 —— 完整读入那份协议再开工。
2. 让候选池有活：$PY $ENGINE --repo $TARGET suggest
3. 前台看护循环（约每分钟一次 tick，worker 喊完成就在一分钟内收尾交付，不装后台）：
   $PY $ENGINE --repo $TARGET watch --minutes 240
   要用定时任务的话：bash $DEST/install-cron.sh $TARGET
4. 每小时巡检汇报用模板，别自己组织语言：
   $PY $ENGINE --repo $TARGET report --patrol
EOF
