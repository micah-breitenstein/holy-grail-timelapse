# Holy Grail Timelapse

Timelapse auto-exposure tooling extracted from the MOCO workspace.

## Included scripts

- `camera_auto_tune.py`: iterative capture/analyze/adjust loop
- `camera_ai_analyzer.py`: image analysis and exposure recommendations

## Requirements

- Python 3.10+
- `gphoto2`
- Optional for richer EXIF reads: `exiftool`

Install Python deps:

```bash
pip install -r requirements.txt
```

## Example

```bash
python camera_auto_tune.py \
  --duration-minutes 120 \
  --interval-seconds 3 \
  --stop-on-optimal
```
