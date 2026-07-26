"""
Lambda entrypoint. Wraps the same FastAPI app used for EC2/local dev
with Mangum, which translates API Gateway/Lambda Function URL events
into ASGI calls. This is why the FastAPI app itself has zero Lambda-
specific code — the adaptation lives entirely in this one file.

Set RESOLVER_DEPLOYMENT=lambda in the Lambda's environment variables so
api/main.py knows not to start an in-process background sweeper loop
(see core/expiry_sweeper.py's module docstring for why that would be
wrong on Lambda).

For the separate scheduled expiry-sweep Lambda, see
lambda_sweep_handler.py — a distinct, much smaller handler invoked on a
cron via EventBridge, not through this one.
"""
from mangum import Mangum

from api.main import app

handler = Mangum(app, lifespan="on")
