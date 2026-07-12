@echo off
REM ============================================================
REM  Daily Paper Trading - Automated Scheduler
REM  Runs SPY Gamma Scalp paper trading once per day
REM  Triggered by Windows Task Scheduler at 4:30 PM ET (market close)
REM ============================================================

cd /d E:\Duke\QuantTrade\QuantSystem

E:\CodingDownload\Anaconda\envs\quantsystem\python.exe scripts/live_paper_trade.py --data-source yahoo --mode once
if errorlevel 1 (
  echo [%date% %time%] Paper trading FAILED errorlevel=%errorlevel% >> logs\paper_trading\scheduler.log
  exit /b 1
)
echo [%date% %time%] Paper trading completed >> logs\paper_trading\scheduler.log
