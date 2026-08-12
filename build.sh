#!/usr/bin/env bash
# Render build command. Runs on every deploy, before the web service starts.
#
# set -o errexit matters: without it a failed migration would still hand
# Render a "successful" build and the service would come up against a
# half-migrated database.
set -o errexit
set -o nounset
set -o pipefail

echo "--> Installing dependencies"
pip install --no-cache-dir -r requirements.txt

echo "--> Collecting static files"
python manage.py collectstatic --no-input

echo "--> Applying migrations"
python manage.py migrate --no-input

echo "--> Build complete"
