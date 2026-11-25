#!/bin/bash
# Container entry: start the root-side SSM helper, then run the hook server as the crew user. Everything else in the VM stays
# unprivileged; the helper (guest/ssm-helper.py) is the only root process and accepts one kind of request.
set -eu
mkdir -p /run/kirocrew /var/log/amazon/ssm
chown root:crew /run/kirocrew && chmod 0770 /run/kirocrew
/usr/bin/python3 /opt/guest-root/ssm-helper.py &
exec setpriv --reuid=1000 --regid=1000 --init-groups env HOME=/home/crew python /opt/guest/hooks.py
