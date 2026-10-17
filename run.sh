#!/bin/bash
# Threads Studio 실행 (Mac). 처음 한 번은 가상환경과 패키지를 설치하고, 이후엔 바로 켜짐.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv || { echo "python3가 필요해요. https://www.python.org/downloads/ 에서 설치 후 다시 실행"; exit 1; }
  ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/python app.py &
PID=$!
sleep 1.5
open "http://127.0.0.1:8787" 2>/dev/null || true
echo "Threads Studio 실행 중 (종료: 이 창에서 Ctrl+C)"
wait $PID
