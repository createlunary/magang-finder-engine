@echo off
title Magang Finder - Mulai
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\panel.ps1" mulai
echo.
echo Jendela ini boleh ditutup; panel tetap menyala di latar.
timeout /t 8 >nul
