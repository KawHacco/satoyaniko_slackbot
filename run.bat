@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Python仮想環境を作成しています...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Python 3 が見つかりません。Python 3.10以降をインストールしてください。
        pause
        exit /b 1
    )
)

echo [2/3] 必要なライブラリを確認・インストールしています...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ライブラリのインストールに失敗しました。
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env がありません。.env.example をコピーしてトークンを設定してください。
    pause
    exit /b 1
)

echo [3/3] Slack Appを起動します...
".venv\Scripts\python.exe" app.py
pause
