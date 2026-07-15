@echo off
echo.
echo ============================================
echo  Step 1: Cleaning old build files...
echo ============================================
if exist dist\print_app rmdir /s /q dist\print_app
if exist build\print_app rmdir /s /q build\print_app
if exist print_app.spec del /q print_app.spec
echo  Done cleaning.

echo.
echo ============================================
echo  Step 2: Installing required packages...
echo ============================================
py -m pip install reportlab PyPDF2 flask werkzeug pywebview pyinstaller pywin32

echo.
echo ============================================
echo  Step 3: Building executable...
echo ============================================
py -m PyInstaller --onedir --windowed --icon=icon.ico ^
  --name=print_app ^
  --collect-all=reportlab ^
  --hidden-import=PyPDF2 ^
  --hidden-import=webview ^
  --hidden-import=flask ^
  --hidden-import=werkzeug ^
  --add-data="templates;templates" ^
  --add-data="static;static" ^
  app.py

echo.
echo ============================================
echo  Step 4: Creating installer...
echo ============================================
set ISCC=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files (x86)\Inno Setup 5\ISCC.exe" set ISCC="C:\Program Files (x86)\Inno Setup 5\ISCC.exe"

if "%ISCC%"=="" (
  echo  Inno Setup not found. Install it from https://jrsoftware.org/isdl.php
  echo  Then run:  "C:\Program Files ^(x86^)\Inno Setup 6\ISCC.exe" setup.iss
) else (
  %ISCC% setup.iss
)

echo.
echo ============================================
echo  ALL DONE!
echo ============================================
echo  Installer: dist\AccountablePrinting_Installer.exe
echo.
echo  REMINDER before shipping this as an update:
echo   1. Did you bump APP_VERSION in app.py?
echo   2. Did you bump AppVersion in setup.iss to match?
echo   3. Upload dist\AccountablePrinting_Installer.exe to a new GitHub
echo      Release tagged v^<version^> (e.g. v1.1.0) on UPDATE_REPO.
echo      See UPDATES.md for the full checklist.
echo ============================================
pause
