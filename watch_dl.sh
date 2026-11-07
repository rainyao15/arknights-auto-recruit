#!/bin/sh
# 后台盯《明日方舟》首次资源下载进度（每 3 分钟一行），用于判断“卡死”还是“在下载”。
cd "$(dirname "$0")"
ADB="D:/Program Files/Netease/MuMu/nx_main/adb.exe"
S=127.0.0.1:16384
i=0
while [ $i -lt 60 ]; do
  i=$((i+1))
  ts=$(date "+%H:%M:%S")
  rx=$(timeout 20 "$ADB" -s $S shell cat /proc/net/dev 2>/dev/null | tr -d '\r' | awk '/wlan0/{print $2}')
  line=$(PYTHONIOENCODING=utf-8 timeout 120 python probe.py 2>/dev/null | grep -E "释放神经递质|[0-9]+%" | head -2 | tr '\n' ' ')
  echo "$ts  rx=${rx:-NA}  ${line:-（无进度文字）}" >> logs/download_progress.log
  sleep 180
done
