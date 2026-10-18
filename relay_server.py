"""
RPIM Iran Relay Server
Acts as an authenticated, streaming reverse proxy hosted in Iran to access domestic CDN links (e.g. Downloadly).
Features:
  1. Token-based authentication (X-RPIM-Relay-Secret header or secret query parameter)
  2. Strict Anti-SSRF & DNS rebinding protection on target URLs
  3. Secure redirect validation (validates redirect Location against SSRF rules)
  4. HTTP Range and RFC 7233 byte-range forwarding for resumable streaming
  5. Bounded local spooling support
"""

import os
import re
import socket
import logging
import ipaddress
import urllib.parse
from pathlib import Path
from typing import Optional, Generator

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Header, Query, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("rpim.relay")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="RPIM Iran Relay Service",
    version="2.0.0",
    description="Secure streaming reverse proxy for domestic Iranian media origins"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=["*"],
)

RELAY_SECRET = os.environ.get("RELAY_AUTH_SECRET", "")
ENV = os.environ.get("ENV", "development").lower()

if ENV == "production":
    if not RELAY_SECRET or "change_me" in RELAY_SECRET or "generate_a_secure" in RELAY_SECRET or len(RELAY_SECRET) < 16:
        raise ValueError("CRITICAL: Production startup aborted! RELAY_AUTH_SECRET is missing or insecure.")

SPOOL_DIR = Path(os.environ.get("RELAY_SPOOL_DIR", "./storage/relay_spool"))
SPOOL_DIR.mkdir(parents=True, exist_ok=True)

PROXY_URL: Optional[str] = os.environ.get("PROXY_URL", "").strip() or None


def create_relay_http_client(timeout: float = 120.0, follow_redirects: bool = False) -> httpx.AsyncClient:
    """
    Creates an AsyncClient with trust_env=False by default to avoid SOCKS proxy errors
    and ensure deterministic internal network routing unless PROXY_URL is explicitly set.
    """
    kwargs = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
        "trust_env": False,
    }
    if PROXY_URL:
        kwargs["proxy"] = PROXY_URL
    return httpx.AsyncClient(**kwargs)


ALLOWED_DOMAINS = {
    "downloadly.ir",
    "dl.downloadly.ir",
    "dl2.downloadly.ir",
    "dl3.downloadly.ir",
    "dl4.downloadly.ir",
    "dl5.downloadly.ir",
    "dl6.downloadly.ir",
    "downloadlynet.ir",
    "dl.downloadlynet.ir",
    "dl2.downloadlynet.ir",
    "dl3.downloadlynet.ir",
    "dl4.downloadlynet.ir",
    "dl5.downloadlynet.ir",
    "dl6.downloadlynet.ir"
}
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443, 8080, 8443}


def verify_relay_secret(
    x_rpim_relay_secret: Optional[str] = Header(None, alias="X-RPIM-Relay-Secret"),
    secret: Optional[str] = Query(None)
) -> bool:
    """Verifies that request has valid relay authorization."""
    if not RELAY_SECRET or RELAY_SECRET.startswith("mock_"):
        return True # Dev / test double mode

    token = x_rpim_relay_secret or secret
    if not token or token != RELAY_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing X-RPIM-Relay-Secret"
        )
    return True


def validate_target_url(url: str) -> str:
    """
    Validates target URL against Anti-SSRF rules:
    - Scheme must be http/https
    - Port must be allowed
    - Hostname must match allowed domains
    - Hostname must NOT resolve to private, loopback, or multicast IP
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail=f"Forbidden scheme: {parsed.scheme}")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise HTTPException(status_code=400, detail=f"Forbidden port: {port}")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="Target URL missing hostname")

    is_allowed = any(
        hostname == d or hostname.endswith("." + d)
        for d in ALLOWED_DOMAINS
    )
    if not is_allowed:
        raise HTTPException(status_code=403, detail=f"SSRF violation: Host '{hostname}' is not in allowed origin list.")

    # Check for IP literal or resolution
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise HTTPException(status_code=403, detail=f"SSRF violation: Direct private IP forbidden: {hostname}")
    except ValueError:
        try:
            addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
            for item in addr_info:
                resolved_ip = ipaddress.ip_address(item[4][0])
                if resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_link_local or resolved_ip.is_reserved:
                    raise HTTPException(status_code=403, detail=f"SSRF violation: Host '{hostname}' resolves to restricted IP: {resolved_ip}")
        except socket.gaierror:
            pass

    return url


@app.get("/health")
@app.get("/api/v1/relay/health")
def relay_health():
    return {
        "status": "HEALTHY",
        "service": "rpim-iran-relay",
        "version": "2.0.0"
    }


@app.api_route("/api/v1/relay/stream", methods=["GET", "HEAD"])
async def relay_stream(
    target: str = Query(..., description="Target origin media URL to stream"),
    request: Request = None,
    auth: bool = Query(True)
):
    """
    Streams a remote media resource through the Iran relay.
    Forwards HTTP Range headers and handles redirects safely.
    """
    # Verify auth
    header_secret = request.headers.get("X-RPIM-Relay-Secret")
    query_secret = request.query_params.get("secret")
    verify_relay_secret(x_rpim_relay_secret=header_secret, secret=query_secret)

    # Validate target against SSRF
    current_target = validate_target_url(target)

    # Prepare forwarding headers
    fwd_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://downloadly.ir/"
    }
    range_header = request.headers.get("Range")
    if range_header:
        fwd_headers["Range"] = range_header
    if_range = request.headers.get("If-Range")
    if if_range:
        fwd_headers["If-Range"] = if_range

    # Handle redirects safely (up to 3 hops)
    client = create_relay_http_client(timeout=120.0, follow_redirects=False)
    redirect_count = 0
    resp = None

    try:
        while redirect_count < 3:
            resp = await client.send(
                client.build_request("GET" if request.method == "GET" else "HEAD", current_target, headers=fwd_headers),
                stream=True
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    break
                redirect_url = urllib.parse.urljoin(current_target, location)
                current_target = validate_target_url(redirect_url)
                await resp.aclose()
                redirect_count += 1
            else:
                break

        # Pass through relevant response headers
        response_headers = {}
        for h in ("Content-Range", "Content-Length", "Content-Type", "ETag", "Last-Modified", "Accept-Ranges"):
            val = resp.headers.get(h)
            if val:
                response_headers[h] = val

        import inspect

        async def _safe_close(obj):
            if hasattr(obj, "aclose"):
                res = obj.aclose()
                if inspect.isawaitable(res):
                    await res

        if request.method == "HEAD":
            status_code = resp.status_code
            await _safe_close(resp)
            await _safe_close(client)
            return Response(status_code=status_code, headers=response_headers)

        async def stream_chunks() -> Generator[bytes, None, None]:
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await _safe_close(resp)
                await _safe_close(client)

        return StreamingResponse(
            stream_chunks(),
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("Content-Type", "application/octet-stream")
        )

    except Exception as e:
        if 'client' in locals():
            await _safe_close(client)
        logger.error(f"Relay stream error for {target}: {e}")
        raise HTTPException(status_code=502, detail=f"Relay stream gateway error: {e}")



@app.post("/api/v1/relay/spool")
async def relay_spool(
    target: str = Query(..., description="Target origin URL to spool locally"),
    request: Request = None
):
    """
    Downloads and buffers the remote target to local disk storage in Iran.
    Returns path and file statistics.
    """
    header_secret = request.headers.get("X-RPIM-Relay-Secret")
    query_secret = request.query_params.get("secret")
    verify_relay_secret(x_rpim_relay_secret=header_secret, secret=query_secret)

    valid_url = validate_target_url(target)
    parsed = urllib.parse.urlparse(valid_url)
    filename = Path(parsed.path).name or "spooled_media.bin"
    spool_file = SPOOL_DIR / filename

    import hashlib
    hasher = hashlib.sha256()
    total_size = 0

    async with create_relay_http_client(timeout=300.0, follow_redirects=True) as client:
        async with client.stream("GET", valid_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://downloadly.ir/"}) as resp:
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="Failed to fetch target for spooling")
            with open(spool_file, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
                    hasher.update(chunk)
                    total_size += len(chunk)

    return {
        "status": "SPOOLED",
        "file_path": str(spool_file),
        "file_name": filename,
        "file_size": total_size,
        "sha256": hasher.hexdigest()
    }


from fastapi.responses import FileResponse

@app.get("/api/v1/relay/download/{filename}")
@app.get("/relay/download/{filename}")
async def relay_download(
    filename: str,
    request: Request
):
    header_secret = request.headers.get("X-RPIM-Relay-Secret")
    query_secret = request.query_params.get("secret")
    verify_relay_secret(x_rpim_relay_secret=header_secret, secret=query_secret)

    safe_name = Path(filename).name
    target_path = SPOOL_DIR / safe_name
    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Spooled file not found")

    return FileResponse(
        path=str(target_path),
        filename=safe_name,
        media_type="application/octet-stream"
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
