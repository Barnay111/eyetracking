#!/bin/bash
# Необязательная нейросеть взгляда (эксперимент, см. README → «Нейросеть»).
# MobileOne-S0 из github.com/yakhyo/gaze-estimation (код MIT; веса обучены на Gaze360 —
# используйте в соответствии с условиями Gaze360: https://gaze360.csail.mit.edu).
set -euo pipefail
cd "$(dirname "$0")/.."
curl -fL --progress-bar -o assets/gaze_mobileone_s0.onnx \
  https://github.com/yakhyo/gaze-estimation/releases/download/weights/mobileone_s0_gaze.onnx
echo "Готово: assets/gaze_mobileone_s0.onnx"
