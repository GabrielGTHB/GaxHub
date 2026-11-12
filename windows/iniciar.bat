@echo off
cd /d "%~dp0"
if not exist static mkdir static
if exist index.html move /y index.html static\ >nul
if exist icon.svg move /y icon.svg static\ >nul
if not exist static\index.html (
  echo Falta index.html. Debe estar junto a app.py o dentro de la carpeta static.
  pause
  exit /b
)
set PY=py
%PY% --version >nul 2>&1
if errorlevel 1 set PY=python
%PY% --version >nul 2>&1
if errorlevel 1 (
  echo No se encontro Python. Reinstalalo marcando Add Python to PATH.
  pause
  exit /b
)
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo No se pudieron instalar los componentes. Revisa tu internet.
  pause
  exit /b
)
start "" %PY%w app.py
