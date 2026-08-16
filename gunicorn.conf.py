"""Gunicorn config. A file rather than CLI flags because distroless has no
shell to expand ${PORT} in an exec-form CMD."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1  # job state is a process-local dict; more workers would break it
timeout = 3600  # uploads are slow
accesslog = "-"
errorlog = "-"
