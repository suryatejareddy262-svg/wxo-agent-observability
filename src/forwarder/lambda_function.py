"""
forwarder/lambda_function.py
============================
AWS Lambda handler for the WXO OTLP Trace Forwarder.

Compatible with:
  - AWS API Gateway (HTTP API payload format v1.0 and v2.0)
  - AWS API Gateway (REST API)
  - AWS Lambda Function URLs
  - Direct Lambda invocation

Environment Variables:
  - WXO_DEPLOYMENT_MODE: cloud (default) | cpd
  cloud mode:
  - API_KEY: WXO IAM API Key (or WXO_API_KEY)
  - TOKEN_URL: WXO IAM Token URL
  - WXO_TENANT_ID: Default WXO Tenant ID
  - WXO_OTEL_EXPORT_URL: Default WXO Trace Ingest URL
  cpd mode (export URL is derived from these):
  - CPD_HOST_URL, CPD_USERNAME, CPD_PASSWORD, CPD_INSTANCE_ID, CPD_NAMESPACE
  - CPD_VERIFY_TLS: "true" to verify the CPD certificate (default: skipped)
  both:
  - ENVIRONMENT_NAME: draft | live
  - WXO_AGENT_MAP: JSON mapping of platform agent IDs to WXO agents
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys

_curr_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_curr_dir)
for p in (_curr_dir, _repo_root):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from forwarder.forwarder import OtlpForwarder
except ImportError:
    from forwarder import OtlpForwarder

logger = logging.getLogger()
logger.setLevel(logging.INFO)

forwarder = OtlpForwarder()


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entrypoint."""
    logger.info("Received invocation event")

    # Handle health checks or warmup pings
    if event.get("rawPath") in ("/health", "/healthz") or event.get("path") in ("/health", "/healthz"):
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({"status": "healthy", "service": "wxo-otlp-forwarder"}),
        }
    # Extract headers (case-insensitive normalization)
    raw_headers = event.get("headers", {}) or {}
    headers = {k.lower(): v for k, v in raw_headers.items()}

    content_type = headers.get("content-type", "application/json")
    content_encoding = headers.get("content-encoding", "")

    # Extract body bytes (handling Base64 encoding from API Gateway)
    raw_body = event.get("body", "")
    is_b64 = event.get("isBase64Encoded", False)

    if isinstance(raw_body, bytes):
        body_bytes = raw_body
    elif is_b64 and isinstance(raw_body, str):
        try:
            body_bytes = base64.b64decode(raw_body)
        except Exception as exc:
            logger.error(f"Failed to decode base64 body: {exc}")
            return _response(400, {"error": "Invalid base64 encoding"})
    elif isinstance(raw_body, str):
        body_bytes = raw_body.encode("utf-8")
    elif isinstance(raw_body, dict):
        body_bytes = json.dumps(raw_body).encode("utf-8")
    else:
        body_bytes = b""

    # Extract path parameters / agent_id
    path_parameters = event.get("pathParameters") or {}
    path_agent_id = path_parameters.get("agent_id") or path_parameters.get("proxy", "")
    if not path_agent_id:
        path = event.get("rawPath") or event.get("path") or ""
        parts = [p for p in path.split("/") if p and p not in ("v1", "traces", "ingest")]
        if parts:
            path_agent_id = parts[0]

    # Extract query parameters
    query_params = event.get("queryStringParameters") or {}

    # refresh settings/map -> auth -> process (shared by every platform adapter)
    status, body = forwarder.handle(
        body_bytes=body_bytes,
        content_type=content_type,
        content_encoding=content_encoding,
        headers=headers,
        query_params=query_params,
        path_agent_id=path_agent_id,
    )
    return _response(status, body)


def _response(status_code: int, body_dict: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        },
        "body": json.dumps(body_dict),
    }
