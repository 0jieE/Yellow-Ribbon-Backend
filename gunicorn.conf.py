"""
Gunicorn reads this file on its own (it looks for ./gunicorn.conf.py), so
Render's start command `gunicorn config.wsgi:application` picks it up without
any change in the dashboard. The port still comes from Render's $PORT.

Threads, not the default single sync worker: an APK download from the website
is 50+ MB and can take minutes on a phone's data. On a sync worker it would
hold the only worker for all that time (the apps' API calls would wait behind
it) and be killed at the 30-second timeout. A threaded worker serves the
download on one thread and keeps answering the apps on the others.

Each thread keeps its own database connection (CONN_MAX_AGE), so 8 threads
mean at most 8 connections, well under Render Postgres's limit.
"""

import os

worker_class = "gthread"
# One process: the free instance has 512 MB of memory.
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
# How long the worker process may go silent before it is restarted. With
# threads the process keeps checking in while a download streams, so a slow
# download does not count against this.
timeout = 60
