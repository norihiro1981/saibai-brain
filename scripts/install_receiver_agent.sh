#!/bin/sh
# 受け手を launchd（ログインした人の自動起動）に入れる／外す。
#   sh scripts/install_receiver_agent.sh             模擬のプラグで入れて起動する
#   sh scripts/install_receiver_agent.sh switchbot   本物のプラグで入れて起動する
#   sh scripts/install_receiver_agent.sh remove      止めて外す
# ログインすると起動し、落ちたら起こし直す（受け手は起動するたびにプラグを「切」から始める）。
# 本物のプラグで常駐させるのは、プラグの先が空か電気スタンドの時だけ（設計書 §5：人が居ない時は加湿器をつながない）。
set -eu

LABEL="local.saibai.receiver"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "remove" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "外した：${LABEL}"
  exit 0
fi

PLUG="dummy"
if [ "${1:-}" = "switchbot" ]; then
  PLUG="switchbot"
fi

mkdir -p "$REPO/run" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>--directory</string><string>$REPO</string>
    <string>run</string><string>saibai-receiver</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$UV"):/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>SAIBAI_PLUG</key><string>$PLUG</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$REPO/run/receiver.log</string>
  <key>StandardErrorPath</key><string>$REPO/run/receiver.log</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# 外し終わる前に入れると「Bootstrap failed: 5」になる＝外し終わるまで待つ
i=0
while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && [ $i -lt 20 ]; do
  sleep 0.5
  i=$((i + 1))
done
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "入れた：${PLIST}（プラグ＝${PLUG}）"
echo "記録：${REPO}/run/receiver.log"
