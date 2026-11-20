#!/bin/bash
# Сборка «Eye Control.app» — обычного приложения macOS (иконка, Launchpad, автозапуск).
#
#   scripts/build_mac_app.sh            → ~/Applications/Eye Control.app
#   scripts/build_mac_app.sh --login    → то же + добавить в автозапуск (Объекты входа)
#
# Это лёгкий бандл: он запускает main.py из этой папки через её .venv, поэтому
# папку проекта нельзя перемещать (иначе просто пересоберите). Логи пишутся в
# ~/.eye_control/app.log. После первого запуска выдайте приложению «Eye Control»
# права Камера и Универсальный доступ (и «Запись экрана» для лупы).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
APP="$HOME/Applications/Eye Control.app"
LOGIN=0
[[ "${1:-}" == "--login" ]] && LOGIN=1

[[ -x "$PY" ]] || { echo "Нет $PY — сначала создайте .venv (см. README)"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Eye Control</string>
  <key>CFBundleDisplayName</key><string>Eye Control</string>
  <key>CFBundleIdentifier</key><string>local.eyecontrol.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>eye-control</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
  <key>NSCameraUsageDescription</key><string>Eye Control определяет направление взгляда по веб-камере.</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/eye-control" <<LAUNCH
#!/bin/bash
mkdir -p "\$HOME/.eye_control"
cd "$ROOT"
exec "$PY" -u main.py "\$@" >> "\$HOME/.eye_control/app.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/eye-control"

# Иконка: рисуем Qt-ом и собираем .icns
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
"$PY" - "$ICONSET" <<'PYICON'
import sys
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QRadialGradient
app = QGuiApplication([])
out = sys.argv[1]
def draw(n):
    img = QImage(n, n, QImage.Format_ARGB32); img.fill(Qt.transparent)
    p = QPainter(img); p.setRenderHint(QPainter.Antialiasing)
    m = n * 0.06; r = QRectF(m, m, n - 2 * m, n - 2 * m)
    p.setPen(Qt.NoPen); p.setBrush(QColor("#1C1E24")); p.drawRoundedRect(r, n * 0.2, n * 0.2)
    cx = cy = n / 2
    p.setPen(QPen(QColor("#DDE3EA"), max(1.0, n * 0.04))); p.setBrush(QColor("#2A2D35"))
    w, h = n * 0.62, n * 0.36
    p.drawEllipse(QRectF(cx - w / 2, cy - h / 2, w, h))
    g = QRadialGradient(cx, cy, n * 0.16); g.setColorAt(0, QColor("#8FD0FF")); g.setColorAt(1, QColor("#2D9CDB"))
    p.setPen(Qt.NoPen); p.setBrush(g); p.drawEllipse(QRectF(cx - n * 0.15, cy - n * 0.15, n * 0.3, n * 0.3))
    p.setBrush(QColor("#0B0C0F")); p.drawEllipse(QRectF(cx - n * 0.06, cy - n * 0.06, n * 0.12, n * 0.12))
    p.setPen(QPen(QColor("#27AE60"), max(1.0, n * 0.035))); p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(cx - n * 0.36, cy - n * 0.36, n * 0.72, n * 0.72), 90 * 16, -270 * 16)
    p.end(); return img
for s in (16, 32, 128, 256, 512):
    draw(s).save(f"{out}/icon_{s}x{s}.png"); draw(s * 2).save(f"{out}/icon_{s}x{s}@2x.png")
PYICON
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

touch "$APP"
echo "Готово: $APP"

if [[ $LOGIN == 1 ]]; then
  osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP\", hidden:false}" >/dev/null
  echo "Добавлено в автозапуск (Системные настройки → Основные → Объекты входа)."
fi
