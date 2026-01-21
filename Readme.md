For executing URLs based:

python versionC.py \
  --env prod \
  --urls urls.txt \
  --duration 60 \
  --delay 5 \
  --bucket 5


For business Journey:
Journey (Login → Search → AddToCart → Checkout)
  ├─ Step 1 timing
  ├─ Step 2 timing
  ├─ Step 3 timing
  └─ Journey total timing
//////////////////////////////////////////////////////////////////////////////

merged usecase:

python synthetic_monitor.py \
  --mode url \
  --env prod \
  --urls urls.txt \
  --duration 30 \
  --delay 5


OR

python synthetic_monitor.py \
  --mode journey \
  --env prod \
  --duration 30 \
  --delay 10
