@echo off
cd /d E:\Duke\QuantTrade\QuantSystem
E:\CodingDownload\Anacondanvs\quantsystem\python.exe scripts\wheel_paper_trade.py --mode once
echo [%date% %time%] BTC wheel paper completed >> logs\paper_trading\scheduler.log
