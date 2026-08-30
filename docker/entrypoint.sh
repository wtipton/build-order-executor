#!/bin/bash
set -e

if [ "$ALLOW_RUN" = "1" ]; then
    cp /opt/executor/run-build /usr/local/bin/run-build
    chown root:root /usr/local/bin/run-build
    chmod 4755 /usr/local/bin/run-build

    cat << 'RUN_EOF' > /workspace/run.py
#!/usr/bin/env python3
import os
import sys

os.execv("/usr/local/bin/run-build", ["run-build"] + sys.argv[1:])
RUN_EOF
    chmod 755 /workspace/run.py
    chown agent:agent /workspace/run.py
else
    rm -f /usr/local/bin/run-build /workspace/run.py
fi

exec runuser -u agent -- "$@"
