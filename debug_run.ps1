Write-Host "--- Campus Sync Debug Launcher ---" -ForegroundColor Cyan

# 1. Check Python
Write-Host "Checking Backend..." -ForegroundColor Yellow
if (Test-Path ".\venv\Scripts\python.exe") {
    Write-Host "Found venv/Scripts/python.exe"
} else {
    Write-Host "ERROR: venv/Scripts/python.exe NOT FOUND!" -ForegroundColor Red
    exit
}

# 2. Check Node
Write-Host "Checking Frontend..." -ForegroundColor Yellow
if (Test-Path ".\frontend\node_modules") {
    Write-Host "Found frontend/node_modules"
} else {
    Write-Host "WARNING: frontend/node_modules NOT FOUND! Running npm install..." -ForegroundColor Yellow
    Set-Location frontend
    npm install
    Set-Location ..
}

# 3. Launch Backend
Write-Host "Launching Backend in new window..." -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit", "-Command", ".\venv\Scripts\python.exe run.py"

# 4. Launch Frontend
Write-Host "Launching Frontend in new window..." -ForegroundColor Green
Set-Location frontend
Start-Process powershell -ArgumentList "-NoExit", "-Command", "npm run dev"
Set-Location ..

Write-Host "Done! Check the new windows for logs." -ForegroundColor Cyan
Write-Host "Backend: http://127.0.0.1:5000"
Write-Host "Frontend: http://localhost:5173"
