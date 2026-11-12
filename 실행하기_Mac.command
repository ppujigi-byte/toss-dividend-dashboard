#!/bin/bash
cd "$(dirname "$0")"

echo "================================================================"
echo "  토스증권 배당·매매 통합 수익 대시보드 (macOS 로컬 실행)"
echo "  - 100% 조회·열람 전용 / 모든 데이터는 내 맥에만 저장됩니다."
echo "================================================================"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo "[설치 안내] python3를 찾을 수 없습니다. https://www.python.org/downloads/macos/ 에서 Python 3를 설치해주세요."
  read -p "엔터를 누르면 창이 닫힙니다..."
  exit 1
fi

python3 server.py --open-browser
