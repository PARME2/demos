#!/bin/bash
# UI検討エージェントチーム - tmux議論ビューア
# Usage: ./discuss.sh "議論のお題"
# 表示: tmux attach -t agent-discuss

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION="agent-discuss"
LOG_DIR="/tmp/agent-discuss-logs"
rm -rf "$LOG_DIR"
mkdir -p "$LOG_DIR"

TOPIC="${1:-"「1年の振り返り → 進路の悩み解決」アプリのUI案を出してください"}"

# 既存セッションがあれば終了
tmux kill-session -t "$SESSION" 2>/dev/null

# エージェントファイル一覧を取得（README除外）
AGENT_FILES=()
for f in "$AGENTS_DIR"/[0-9]*.md; do
  [ -f "$f" ] && AGENT_FILES+=("$f")
done

AGENT_COUNT=${#AGENT_FILES[@]}

if [ "$AGENT_COUNT" -eq 0 ]; then
  echo "エージェントファイルが見つかりません"
  exit 1
fi

# 各エージェント用のプロンプトファイルを生成
for i in "${!AGENT_FILES[@]}"; do
  AGENT_FILE="${AGENT_FILES[$i]}"
  AGENT_NAME=$(basename "$AGENT_FILE" .md)
  PROMPT_FILE="$LOG_DIR/${AGENT_NAME}_prompt.txt"

  cat > "$PROMPT_FILE" <<PROMPT_EOF
以下のシステムプロンプトに従って回答してください。

--- システムプロンプト ---
$(cat "$AGENT_FILE")
--- システムプロンプトここまで ---

お題:
$TOPIC
PROMPT_EOF
done

# tmuxセッション作成（デタッチモード）
tmux new-session -d -s "$SESSION" -x 220 -y 60
tmux rename-window -t "$SESSION" "discussion"

# ペイン分割
for i in $(seq 1 $((AGENT_COUNT - 1))); do
  tmux split-window -t "$SESSION" -d
done
tmux select-layout -t "$SESSION" tiled

# 各ペインでclaude CLIを起動（出力をログにも書き出し）
for i in "${!AGENT_FILES[@]}"; do
  AGENT_FILE="${AGENT_FILES[$i]}"
  AGENT_NAME=$(basename "$AGENT_FILE" .md)
  PROMPT_FILE="$LOG_DIR/${AGENT_NAME}_prompt.txt"
  LOG_FILE="$LOG_DIR/${AGENT_NAME}.log"

  tmux send-keys -t "$SESSION.$i" \
    "echo '━━━━━━━━━━━━━━━━━━━━━━━━' && echo ' $AGENT_NAME' && echo '━━━━━━━━━━━━━━━━━━━━━━━━' && echo '' && unset CLAUDECODE && cat '$PROMPT_FILE' | claude -p 2>&1 | tee '$LOG_FILE'" Enter
done

echo "=== UI検討エージェントチーム ==="
echo "お題: $TOPIC"
echo "エージェント数: $AGENT_COUNT"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " tmux attach -t $SESSION   ← ターミナルで実行してリアルタイム表示"
echo " tmux kill-session -t $SESSION  ← 終了"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "ログ出力先: $LOG_DIR/"
for i in "${!AGENT_FILES[@]}"; do
  AGENT_NAME=$(basename "${AGENT_FILES[$i]}" .md)
  echo "  - ${AGENT_NAME}.log"
done
