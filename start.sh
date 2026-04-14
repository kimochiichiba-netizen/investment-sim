#!/bin/bash
# Claude投資シミュレーター 起動スクリプト
# このファイルをダブルクリック or ターミナルで bash start.sh で起動できます

cd "$(dirname "$0")"

echo "🚀 Claude投資シミュレーターを起動中..."
echo "📌 ブラウザが自動で開きます"

# 既に起動中のサーバーを停止する
pkill -f "uvicorn main:app" 2>/dev/null
sleep 1

# サーバーをバックグラウンドで起動
python3 main.py &
SERVER_PID=$!

# サーバーが起動するまで待つ（最大10秒）
for i in {1..10}; do
  sleep 1
  if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo "✅ サーバー起動完了！"
    break
  fi
  echo "  待機中... ($i秒)"
done

# ブラウザを自動で開く
open http://localhost:8000

echo ""
echo "✅ ブラウザでダッシュボードが開きました"
echo "🛑 終了するには Ctrl+C を押してください"

# サーバーが終了するまで待つ
wait $SERVER_PID
