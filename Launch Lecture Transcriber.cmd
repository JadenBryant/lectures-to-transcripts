@echo off
setlocal

cd /d "%~dp0"

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "VENV_PYTHONW=%CD%\.venv\Scripts\pythonw.exe"
set "NEEDS_SETUP=0"

if not exist "%VENV_PYTHON%" (
    echo The project virtual environment was not found.
    set "NEEDS_SETUP=1"
) else (
    "%VENV_PYTHON%" -c "import groq, yt_dlp" >nul 2>nul
    if errorlevel 1 (
        echo The project virtual environment is missing required packages.
        set "NEEDS_SETUP=1"
    )
)

if "%NEEDS_SETUP%"=="1" (
    echo.
    choice /m "Set up the app now"
    if errorlevel 2 exit /b 1

    if not exist "%VENV_PYTHON%" (
        echo.
        echo Creating .venv...
        python -m venv .venv
        if errorlevel 1 (
            echo.
            echo Could not create .venv. Make sure Python 3.10 or newer is installed and available on PATH.
            echo.
            pause
            exit /b 1
        )
    )

    echo.
    echo Installing dependencies...
    "%VENV_PYTHON%" -m pip install --upgrade pip
    if errorlevel 1 (
        echo.
        echo Could not upgrade pip.
        echo.
        pause
        exit /b 1
    )

    "%VENV_PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Could not install dependencies.
        echo.
        pause
        exit /b 1
    )
)

if exist "%VENV_PYTHONW%" (
    start "Lecture Transcriber" "%VENV_PYTHONW%" -m lecture_transcriber
) else (
    start "Lecture Transcriber" "%VENV_PYTHON%" -m lecture_transcriber
)

endlocal
