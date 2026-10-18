#!/bin/sh
set -eu
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
flask --app wsgi init-db
exec gunicorn --bind 0.0.0.0:8000 --workers "${WEB_WORKERS:-2}" --threads "${WEB_THREADS:-4}" --timeout 60 --access-logfile - --error-logfile - wsgi:app
