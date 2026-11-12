@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 토스증권 배당·매매 통합 수익 대시보드

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 "실행하기.py"
    goto :end
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python "실행하기.py"
    goto :end
)

echo.
echo [안내] Python 3.9 이상이 설치되어 있지 않거나 PATH에 등록되지 않았습니다.
echo Python 3 설치 후 다시 실행해 주세요.
echo.
pause

:end
if %errorlevel% neq 0 (
    echo.
    pause
)
