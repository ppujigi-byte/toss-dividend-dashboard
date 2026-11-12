#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows · macOS 공통 원클릭 대시보드 실행기
- Windows 탐색기에서 이 파일(실행하기.py)을 더블클릭하거나 터미널에서 `python 실행하기.py`를 실행하면
  로컬 대시보드 서버(127.0.0.1:8765)가 켜지고 기본 웹 브라우저가 자동으로 열립니다.
"""

import sys
import server

if __name__ == "__main__":
    if "--open-browser" not in sys.argv:
        sys.argv.append("--open-browser")
    server.main()
