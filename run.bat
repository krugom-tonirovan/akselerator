@echo off
cd /d "%~dp0"
python -c "import fastapi, uvicorn, numpy, rasterio" >nul 2>&1
if errorlevel 1 (
  echo Python packages are missing. Run: python -m pip install -r requirements.txt
  pause
  exit /b 1
)
where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js and npm are missing. Install Node.js 20.19+ or 22.12+ first.
  pause
  exit /b 1
)
if not exist "frontend\node_modules" (
  echo Frontend packages are missing. Run: npm --prefix frontend install
  pause
  exit /b 1
)
start "Pole Les API" cmd /k python server.py
call npm --prefix frontend run dev
pause
