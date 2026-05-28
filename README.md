# Holy Grail Timelapse

Timelapse auto-exposure tooling extracted from the MOCO workspace.

## Included scripts

- `camera_auto_tune.py`: iterative capture/analyze/adjust loop
- `camera_ai_analyzer.py`: image analysis and exposure recommendations

## Requirements

- Python 3.10+
- `gphoto2`
- Optional for richer EXIF reads: `exiftool`

### Metadata utility (ISO/shutter/f-stop)

`camera_ai_analyzer.py` reads capture metadata in this order:

1. `exiftool` (preferred)
2. Pillow EXIF
3. `mdls` fallback on macOS

Install `exiftool` on macOS:

```bash
brew install exiftool
```

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

## Day-to-night example command

```bash
camera_auto_tune.py \
  --duration-minutes 1440 \
  --interval-seconds 30 \
  --capture-timeout 90 \
  --capture-retry-seconds 5 \
  --max-consecutive-capture-failures 10 \
  --keep-dir timelapse \
  --reject-log reject_candidates.csv \
  --iso-strategy last \
  --startup-priority low-iso \
  --startup-set-baseline \
  --startup-iso 100 \
  --startup-fstop 16 \
  --target-luma 60 \
  --deadband-ev 0.10 \
  --startup-deadband-ev 0.30 \
  --max-step-ev 0.50 \
  --settled-max-step-ev 0.22 \
  --settled-breakout-ev 0.5 \
  --startup-max-step-ev 1.50 \
  --startup-retry-seconds 1 \
  --startup-max-iterations 0 \
  --shutter-min 1/8000 \
  --shutter-max 1 \
  --shutter-stops "1,1/1.1,1/1.2,1/1.3,1/1.5,1/1.6,1/1.8,1/2,1/2.5,1/3,1/4,1/5,1/6,1/8,1/10,1/13,1/15,1/20,1/25,1/30,1/40,1/50,1/60,1/80,1/100,1/125,1/160,1/200,1/250,1/320,1/400,1/500,1/640,1/800,1/1000,1/1250,1/1600,1/2000,1/2500,1/3200,1/4000,1/5000,1/6400,1/8000" \
  --iso-min 100 \
  --iso-max 12800 \
  --iso-stops "100,125,160,200,250,320,400,500,640,800,1000,1250,1600,2000,2500,3200,4000,5000,6400,8000,12800,25600" \
  --fstop-stops "1.4,1.6,1.8,2,2.2,2.5,2.8,3.2,3.5,4,4.5,5,5.6,6.3,7.1,8,9,10,11,13,14,16"
```

## Example result

Created with this workflow:

https://www.youtube.com/watch?v=9VeuSSQI37s
