#!/bin/bash
# Laeuft in GitHub Actions: alle 3 Bots bis zu MAX Minuten, Stand alle 15 min ins Repo pushen.
cd "$(dirname "$0")"
MAX=${MAX_MINUTES:-345}
git config user.name "trading-bot"; git config user.email "trading-bot@users.noreply.github.com"
push() {
  ( cd .. && git add trading-bot/runs && { git diff --cached --quiet || {
      git commit -qm "Bot-Stand $(date -u '+%F %H:%M') UTC"
      for i in 1 2 3 4; do git pull -q --rebase origin "$GITHUB_REF_NAME" && git push -q origin HEAD:"$GITHUB_REF_NAME" && break; sleep $((2**i)); done; }; } )
}
pids=()
for b in mr trend xsmom; do
  if grep -q "ENDE" runs/$b/bot.log 2>/dev/null; then echo "[$b] bereits beendet"; continue; fi
  case $b in
    mr)    python3 bot.py live --dir runs/mr --max-minutes $MAX & ;;
    trend) python3 bot.py live --strategy trend --dir runs/trend --max-minutes $MAX & ;;
    xsmom) python3 xsmom.py live --dir runs/xsmom --max-minutes $MAX & ;;
  esac
  pids+=($!)
done
alive=1
while [ ${#pids[@]} -gt 0 ] && [ $alive = 1 ]; do
  for t in $(seq 90); do
    sleep 10; alive=0
    for p in "${pids[@]}"; do kill -0 $p 2>/dev/null && alive=1; done
    [ $alive = 0 ] && break
  done
  push
done
push
