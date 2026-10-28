#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

case "${LINKS_RUN_MIGRATIONS:-1}" in
    1)
        echo "Migrating....."
        python manage.py migrate
        ;;
    0)
        echo "Skipping migrations (LINKS_RUN_MIGRATIONS=0). Schema must be checked before deployment."
        ;;
    *)
        echo "LINKS_RUN_MIGRATIONS must be 0 or 1." >&2
        exit 2
        ;;
esac

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn....."
daphne -b 0.0.0.0 -p 8000 --http-timeout 600 ecpmpapp.asgi:application

