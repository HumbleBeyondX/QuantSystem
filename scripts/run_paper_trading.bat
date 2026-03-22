@echo off
REM SPY Gamma Scalp - Daily Paper Trading Runner
REM Schedule this with Windows Task Scheduler to run at 4:30 PM ET (market close + 30min)
REM
REM Usage:
REM   run_paper_trading.bat              (default: Alpaca + execution)
REM   run_paper_trading.bat yahoo        (Yahoo only, no execution)
REM   run_paper_trading.bat alpaca       (Alpaca data + execution)
REM   run_paper_trading.bat continuous   (Run continuously)

cd /d E:\Duke\QuantTrade\QuantSystem

set PYTHON=E:\CodingDownload\Anaconda\envs\quantsystem\python.exe
set SCRIPT=scripts\live_paper_trade.py

REM Load .env if exists
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        set "%%a=%%b"
    )
)

if "%1"=="yahoo" (
    echo Running with Yahoo Finance data...
    %PYTHON% %SCRIPT% --data-source yahoo --mode once
) else if "%1"=="continuous" (
    echo Running in continuous mode with Alpaca...
    %PYTHON% %SCRIPT% --data-source alpaca --mode continuous --alpaca-execution --interval 30
) else (
    echo Running with Alpaca data + execution...
    %PYTHON% %SCRIPT% --data-source alpaca --mode once --alpaca-execution
)

echo.
echo Paper trading run complete at %date% %time%
pause
