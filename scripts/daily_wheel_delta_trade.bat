@echo off
cd /d E:\Duke\QuantTrade\QuantSystem
E:\CodingDownload\Anaconda\envs\quantsystem\python.exe scripts\wheel_delta_paper_trade.py --mode once >> logs\paper_trading\scheduler.log 2>&1
if errorlevel 1 (
  echo [%date% %time%] BTC wheel DELTA paper FAILED errorlevel=%errorlevel% >> logs\paper_trading\scheduler.log
  exit /b 1
)
echo [%date% %time%] BTC wheel DELTA paper completed >> logs\paper_trading\scheduler.log
