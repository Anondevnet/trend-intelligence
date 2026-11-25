#!/bin/zsh
# Removes the Trend Intelligence launchd jobs. Data in ./data is NOT touched.
set -euo pipefail
for job in collect backup; do
  label="com.trendintel.$job"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
  echo "removed $label"
done
