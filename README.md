# eyetracking

Управление компьютером взглядом через обычную веб-камеру (macOS + Windows) и
справочные проекты, на основе которых оно сделано.

| Папка | Что это | Лицензия |
|---|---|---|
| [`eye_control/`](eye_control/) | **Eye Control** — основное приложение: калибровка, dwell-клик, магнит к кнопкам, лупа, экранная клавиатура с подсказками, панель команд, подстройка и тренировка по реальным целям экрана | см. [`eye_control/THIRD_PARTY.md`](eye_control/THIRD_PARTY.md) |
| [`EyeTrax/`](EyeTrax/) | копия [ck-zhang/EyeTrax](https://github.com/ck-zhang/EyeTrax) — источник извлечения признаков | MIT |
| [`Eye-Tracking/`](Eye-Tracking/) | копия [veyndor1/Eye-Tracking](https://github.com/veyndor1/Eye-Tracking) | MIT |
| [`screen-eye-tracking/`](screen-eye-tracking/) | копия [PINTO0309/screen-eye-tracking](https://github.com/PINTO0309/screen-eye-tracking) | MIT |

Справочные проекты — снимки исходного кода без истории git, лицензии авторов сохранены
в их папках.

## Быстрый старт

```bash
cd eye_control
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Подробности — в [`eye_control/README.md`](eye_control/README.md).
