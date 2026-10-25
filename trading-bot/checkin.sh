#!/bin/bash
# Zwischenbericht: neuesten Stand aus GitHub holen und anzeigen (Bots laufen in GitHub Actions).
cd "$(dirname "$0")/.." && git pull -q origin claude/katapult-video
cd trading-bot
for b in mr trend xsmom; do
  echo "=================== $b"; cat runs/$b/status.md 2>/dev/null; echo "--- Log:"; tail -n 8 runs/$b/bot.log 2>/dev/null
done
