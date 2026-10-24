#!/bin/bash
# Zwischenbericht: Bots pruefen, ggf. wiederherstellen/neu starten, sichern, pushen.
cd "$(dirname "$0")"
now=$(date +%s)
declare -A DIR=( [mr]=. [trend]=runs/trend [xsmom]=runs/xsmom )
declare -A CMD=( [mr]="python3 bot.py live --hours 72" \
                 [trend]="python3 bot.py live --strategy trend --hours 72 --dir runs/trend" \
                 [xsmom]="python3 xsmom.py live --hours 72 --dir runs/xsmom" )
for b in mr trend xsmom; do
  d=${DIR[$b]}; mkdir -p "$d"
  if [ ! -f "$d/state.json" ] && [ -f "snapshot/$b/state.json" ]; then
    cp snapshot/$b/* "$d/"; echo "[$b] Stand aus Sicherung wiederhergestellt"
  fi
  if ! ps -eo cmd | grep -v grep | grep -qF "${CMD[$b]%% --hours*}"; then
    if [ -f "$d/state.json" ] && ! grep -q "ENDE" "$d/bot.log" 2>/dev/null; then
      setsid nohup ${CMD[$b]} >/dev/null 2>&1 </dev/null & echo "[$b] neu gestartet"
    fi
  fi
done
sleep 5
for b in mr trend xsmom; do
  d=${DIR[$b]}; mkdir -p snapshot/$b
  cp $d/state.json $d/status.md $d/bot.log snapshot/$b/ 2>/dev/null; cp $d/trades.csv snapshot/$b/ 2>/dev/null
  echo "=================== $b"; cat $d/status.md 2>/dev/null; echo "--- Log:"; tail -n 8 $d/bot.log 2>/dev/null
done
cd .. && git add -A trading-bot >/dev/null && git commit -qm "Bot-Zwischenstand $(date -u +%F\ %H:%M)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012Vf2XYH45Gdgypk9G9HE6r" && git push -q -u origin claude/katapult-video && echo "gesichert"
