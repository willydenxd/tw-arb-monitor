#!/usr/bin/env bash
# arb_monitor_watchdog.sh -- cron no_agent（每 5 分鐘）：盤別時段內 monitor.py 沒在跑就用 WMI 背景啟動。
# 正常 stdout 為空；中途掛掉重啟時，每個盤別只通知一次。
# REPO = 本 repo 路徑、PYEXE = 你的 python（Windows Git Bash；用 WMI 背景啟動，cron 不會被卡住）
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
PYEXE="${PYEXE:-python}"
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV PYTHONSTARTUP
export PYTHONIOENCODING=utf-8
cd "$REPO" || exit 1
tag=$(python -c "
import datetime as dt
n=dt.datetime.now(); t=n.time(); w=n.weekday()
if w<5 and dt.time(8,30)<=t<dt.time(13,44): print(n.strftime('%Y%m%d')+'D')
elif w<5 and t>=dt.time(14,50): print(n.strftime('%Y%m%d')+'N')
elif 1<=w<=5 and t<dt.time(4,55): print((n-dt.timedelta(days=1)).strftime('%Y%m%d')+'N')
else: print('')")
[ -n "$tag" ] || exit 0
n=$(powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object {\$_.CommandLine -like '*arb_monitor*monitor.py*' -or \$_.CommandLine -like '* -u monitor.py*'} | Measure-Object).Count")
[ "${n//[^0-9]/}" = "0" ] || exit 0
seen=0; grep -q "start session=$tag" log/monitor.log 2>/dev/null && seen=1
WREPO=$(cygpath -w "$REPO" 2>/dev/null || echo "$REPO")
rv=$(powershell -NoProfile -Command "\$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd /c \"$PYEXE -u monitor.py > log\stdout.log 2> log\stderr.log\"'; CurrentDirectory='$WREPO'}; \$r.ReturnValue")
[ "${rv//[^0-9]/}" = "0" ] || echo "[撿錢監控] 啟動失敗（Win32_Process.Create rv=$rv）"
flag="log/notified_$tag"
if [ "$seen" = "1" ] && [ ! -f "$flag" ]; then
  touch "$flag"; echo "[撿錢監控] $tag 中途停止，已重啟（看 log/monitor.log）；本盤之後默默重試不再通知"
fi
