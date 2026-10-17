#!/bin/bash
# 더블클릭으로 실행. 처음 한 번만 패키지를 설치하고, 이후엔 바로 켜져요.
cd "$(dirname "$0")"
clear
echo "  Threads Studio 켜는 중..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "  python3가 없어요. 뜨는 창에서 '설치'를 누른 뒤 다시 더블클릭해 주세요."
  xcode-select --install 2>/dev/null
  read -n 1 -s -r -p "  아무 키나 누르면 닫힙니다."
  exit 1
fi
if [ ! -d .venv ]; then
  echo "  처음 실행이라 필요한 것들을 설치해요 (30초쯤)..."
  python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt || { echo "  설치 실패. 인터넷 연결을 확인하고 다시 시도해 주세요."; read -n 1 -s -r; exit 1; }
fi
# 이미 켜져 있는 예전 서버가 있으면 먼저 끔
lsof -ti tcp:8787 2>/dev/null | xargs kill -9 2>/dev/null
sleep 0.5
./.venv/bin/python app.py &
PID=$!
sleep 1.5
open "http://127.0.0.1:8787"
echo
echo "  ✓ 브라우저에 열렸어요: http://127.0.0.1:8787"
echo "  이 창은 닫지 마세요. 끄려면 이 창을 닫거나 Ctrl+C."
echo
wait $PID
