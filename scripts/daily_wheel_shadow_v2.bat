@echo off
rem Phase B: runtime-v2 wheel shadows (btc_wheel_v2 + btc_wheel_delta_v2)
rem + daily parity diff vs the untouched originals. Runs 16:55, after the
rem originals' 16:30/16:40/16:50 block, so decisions are near-simultaneous.
cd /d E:\Duke\QuantTrade\QuantSystem
E:\CodingDownload\Anaconda\envs\quantsystem\python.exe -u -m core.runtime.runner --only btc_wheel_v2 btc_wheel_delta_v2 --once >> logs\paper_trading\scheduler.log 2>&1
if errorlevel 1 (
  echo [%date% %time%] wheel shadow v2 FAILED errorlevel=%errorlevel% >> logs\paper_trading\scheduler.log
)
E:\CodingDownload\Anaconda\envs\quantsystem\python.exe -u scripts\wheel_parity_check.py >> logs\paper_trading\scheduler.log 2>&1
if errorlevel 1 (
  echo [%date% %time%] wheel shadow PARITY DIVERGENCE >> logs\paper_trading\scheduler.log
  exit /b 1
)
echo [%date% %time%] wheel shadow v2 + parity completed >> logs\paper_trading\scheduler.log
