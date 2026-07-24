import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeExternalURLError(ValueError):
    pass


async def validate_public_https_url(value: str) -> str:
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeExternalURLError("Use a public HTTPS calendar subscription URL.")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeExternalURLError("Local calendar URLs are not allowed.")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeExternalURLError("Private or local calendar addresses are not allowed.")
        return url
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise UnsafeExternalURLError("The calendar host could not be resolved.") from exc
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_global for address in addresses
    ):
        raise UnsafeExternalURLError("Private or local calendar addresses are not allowed.")
    return url
