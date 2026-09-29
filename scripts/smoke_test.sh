#!/usr/bin/env sh
set -eu
python scripts/test_dify_direct.py --query "退款政策是多少？"
python scripts/test_gateway.py --query "退款政策是多少？"
python scripts/acceptance.py
