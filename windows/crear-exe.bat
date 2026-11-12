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
echo Creando Gax.exe, espera unos minutos...
%PY% -m pip install -q -r requirements.txt pyinstaller
set ICO=
if exist icon.ico set ICO=--icon icon.ico
%PY% -m PyInstaller --onefile --noconsole --name Gax %ICO% --add-data "static;static" app.py
if errorlevel 1 (
  echo.
  echo Hubo un error al crear el programa. Copia el mensaje de arriba.
  pause
  exit /b
)
echo.
echo Listo. Tu programa esta en la carpeta dist: Gax.exe
pause
