#!/bin/sh
# Точка входа для Task Scheduler (root). Вся логика в nas-update.py рядом.
#   sh /volume1/docker/nas-update/nas-update.sh [--dry-run] [app ...]
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
exec "${PYTHON:-python3}" -I "$(dirname "$0")/nas-update.py" "$@"
