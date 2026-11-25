#!/bin/zsh
# Installs the Trend Intelligence launchd jobs for the CURRENT user. Run it yourself:
#   zsh scripts/launchd/install.sh
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
UV="$(command -v uv)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$PROJECT/data/logs"
for job in collect backup; do
  label="com.trendintel.$job"
  target="$AGENTS/$label.plist"
  sed -e "s#__UV__#$UV#g" -e "s#__PROJECT__#$PROJECT#g" \
      "$PROJECT/scripts/launchd/$label.plist.template" > "$target"
  plutil -lint "$target" >/dev/null
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$target"
  echo "installed $label -> $target"
done
echo "check: launchctl print gui/$(id -u)/com.trendintel.collect | grep -E 'state|last exit'"
