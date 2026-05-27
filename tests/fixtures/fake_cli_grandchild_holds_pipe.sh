#!/bin/sh
# regression fixture: parent prints output, spawns a backgrounded
# grandchild that inherits stdout AND uses setsid to leave the process
# group so killpg can't reach it, then exits. The grandchild keeps the
# pipe write-end open with `sleep 5`. Without the request_stop
# mechanism, the substrate's reader thread would block in os.read()
# until the grandchild dies (or join timeout fires).
echo "hello from parent"
setsid sh -c 'sleep 5' </dev/null >&1 2>&1 &
exit 0
