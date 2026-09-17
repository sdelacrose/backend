#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

echo "Migrating....."
python manage.py migrate

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn....."
daphne -b 0.0.0.0 -p 8000 --http-timeout 600 ecpmpapp.asgi:application

