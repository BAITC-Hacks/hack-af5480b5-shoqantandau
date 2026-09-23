#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py migrate --noinput
printf 'Open http://127.0.0.1:8000/ - manager / manager12345\n'
exec .venv/bin/python manage.py runserver 127.0.0.1:8000
