@echo off
title Magang Finder - Hentikan
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\panel.ps1" berhenti
timeout /t 5 >nul
