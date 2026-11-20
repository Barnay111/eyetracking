# Сторонние компоненты Eye Control

## EyeTrax — MIT
Извлечение признаков взгляда (`core/gaze_engine.py`, `FeatureExtractor`: ориентиры глаз и
радужки MediaPipe в системе координат головы, yaw/pitch/roll) и подход с Ridge-регрессией
адаптированы из https://github.com/ck-zhang/EyeTrax.

    MIT License
    
    Copyright (c) 2024 ck-zhang
    
    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:
    
    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.
    
    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

## MediaPipe Face Landmarker — Apache License 2.0
`assets/face_landmarker.task` — модель Google MediaPipe
(https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker), Apache 2.0.

## wordfreq — данные CC BY-SA 4.0
`assets/words_ru.txt`, `assets/words_en.txt` построены по частотным спискам
https://github.com/rspeer/wordfreq (Robyn Speer). Данные wordfreq распространяются по
CC BY-SA 4.0, поэтому эти два файла тоже распространяются по CC BY-SA 4.0
(https://creativecommons.org/licenses/by-sa/4.0/).

## gaze-estimation (необязательно, не входит в репозиторий) — MIT
`core/neural_gaze.py` работает с моделью MobileOne-S0 из
https://github.com/yakhyo/gaze-estimation (код MIT, Copyright (c) 2024 Yakhyokhuja
Valikhujaev). Веса обучены на датасете Gaze360 и скачиваются отдельно
(`scripts/download_models.sh`); соблюдайте условия Gaze360.
