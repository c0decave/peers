#!/bin/sh
# Emits a line, sleeps, emits another. Total ~6s, idle gaps ~1s.
for i in 1 2 3 4 5 6; do
  echo "progress $i"
  sleep 1
done
echo "DONE"
exit 0
