#!/bin/bash
# install argus service
cp argus.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable argus
systemctl start argus
echo "Argus service installed and started (mock mode)"