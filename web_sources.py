"""Enforce primary-source hosts, resolving only Google's fixed citation redirect host."""

import os
from urllib.parse import urlsplit

import requests

DEFAULT_DOMAINS = "gov.in,nic.in,gov,gov.uk,legislation.gov.uk,fao.org,un.org"
REDIRECT_HOST = "vertexaisearch.cloud.google.com"


def safe_https(value):
    if not isinstance(value, str) or any(c.isspace() or c in '<>()\\' for c in value):
        return False
    try:
        url = urlsplit(value)
        return url.scheme == "https" and bool(url.hostname) and not url.username and not url.password and url.port in (None, 443)
    except ValueError:
        return False


def allowed_domains():
    return [d.strip().lower().strip(".") for d in os.getenv("RAG_WEB_PRIMARY_DOMAINS", DEFAULT_DOMAINS).split(",") if d.strip()]


def is_primary(url):
    if not safe_https(url):
        return False
    host = urlsplit(url).hostname.lower()
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains())


def primary_url(url):
    if is_primary(url):
        return url
    if not safe_https(url):
        return None
    parsed = urlsplit(url)
    if parsed.hostname != REDIRECT_HOST or not parsed.path.startswith("/grounding-api-redirect/"):
        return None
    # Never fetch a model-selected destination, follow redirects, or resolve
    # arbitrary hosts. Read a bounded redirect response from Google's fixed host.
    try:
        with requests.get(url, allow_redirects=False, timeout=5, stream=True) as response:
            target = response.headers.get("Location")
            return target if response.status_code in (301, 302, 303, 307, 308) and is_primary(target) else None
    except requests.RequestException:
        return None
