#!/bin/sh
set -eu
# Deliberately ignore DHCP default routes, DNS, search domains and shell commands.
exec /usr/bin/python3 -I -c "import sys; sys.path.insert(0, '/opt/netem-traffic-simulator'); from trafficgen.data_dhcp import main; main()" "$1"
