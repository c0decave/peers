#!/bin/sh
# Stays silent for too long, then prints. Used to trigger idle-timeout.
sleep 5
echo "late output"
exit 0
