How you use it
URL monitoring
python ui_synthetic_monitor.py \
  --mode url \
  --input urls.txt \
  --duration 60 \
  --lighthouse

Business journeys
python ui_synthetic_monitor.py \
  --mode journey \
  --input journeys.txt \
  --duration 30


(Each line in journeys.txt maps to a function in JOURNEYS)
