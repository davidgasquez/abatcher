import asyncio
import hashlib
import json
import logging
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import aiometer
import anyio
import httpx
from tenacity import (
    RetryError,
    retry,
    stop_after_attempt,
    wait_exponential,
)

# Default cache directory relative to the system's temporary directory
DEFAULT_CACHE_DIR = Path(tempfile.gettempdir()) / "abatcher_cache"

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class RequestDict(TypedDict):
    url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    params: Mapping[str, str | int | float | bool] | None
    headers: Mapping[str, str] | None
    json: Any | None
    data: Mapping[str, str | int | float | bool] | None
    content: bytes | None


class CacheMetadata(TypedDict):
    expires_at: float
    status_code: int
    headers: dict[str, str]
    encoding: str | None


@dataclass(frozen=True)
class BatchConfig:
    max_concurrent: int
    max_per_second: float
    cache_enabled: bool
    cache_dir: Path
    cache_ttl: int
    retry_attempts: int
    retry_wait_multiplier: int
    retry_wait_max: int


def _normalize_request(request: str | RequestDict) -> RequestDict:
    """Normalize string URLs or dicts into a standard RequestDict."""
    if isinstance(request, str):
        return {
            "url": request,
            "method": "GET",
            "params": None,
            "headers": None,
            "json": None,
            "data": None,
            "content": None,
        }

    # Ensure method is uppercase and valid, default to GET
    method = request.get("method", "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        logger.warning(f"Invalid HTTP method '{method}', defaulting to GET.")
        method = "GET"

    # Create a mutable copy and update the method
    normalized: RequestDict = cast(RequestDict, request.copy())
    normalized["method"] = cast(
        Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], method
    )
    return normalized


def _generate_cache_key(request: RequestDict) -> str:
    """Generate a stable cache key for a request."""
    hasher = hashlib.sha256()
    hasher.update(request["method"].encode())
    hasher.update(request["url"].encode())

    if params := request.get("params"):
        hasher.update(json.dumps(params, sort_keys=True).encode())
    if headers := request.get("headers"):
        # Filter out potentially dynamic headers if needed, but keep it simple for now
        hasher.update(json.dumps(headers, sort_keys=True).encode())
    if json_data := request.get("json"):
        hasher.update(json.dumps(json_data, sort_keys=True).encode())
    if data := request.get("data"):
        hasher.update(json.dumps(data, sort_keys=True).encode())
    if content := request.get("content"):
        hasher.update(content)

    return hasher.hexdigest()


def _remove_cache_entry(cache_key: str, cache_dir: Path) -> None:
    """Remove cache files safely."""
    meta_path = cache_dir / f"{cache_key}.meta.json"
    data_path = cache_dir / f"{cache_key}.data"
    logger.debug(f"Removing cache entry: {cache_key}")
    try:
        if meta_path.exists():
            meta_path.unlink()
            logger.debug(f"Removed cache metadata file: {meta_path}")
        if data_path.exists():
            data_path.unlink()
            logger.debug(f"Removed cache data file: {data_path}")
    except OSError as e:
        logger.warning(f"Failed to remove cache entry {cache_key}: {e}")


def _load_from_cache(
    cache_key: str, cache_dir: Path
) -> tuple[CacheMetadata | None, bytes | None]:
    """Load metadata and content from cache, checking validity and expiration."""
    meta_path = cache_dir / f"{cache_key}.meta.json"
    data_path = cache_dir / f"{cache_key}.data"

    if not meta_path.exists() or not data_path.exists():
        logger.debug(f"Cache miss (files not found) for key {cache_key}")
        return None, None

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            metadata: CacheMetadata = json.load(f)
        # Check essential keys before proceeding
        if not all(k in metadata for k in ("expires_at", "status_code", "headers")):
            raise KeyError("Missing essential keys in cache metadata")

        if time.time() > metadata["expires_at"]:
            logger.debug(f"Cache expired for key {cache_key}")
            _remove_cache_entry(cache_key, cache_dir)
            return None, None

        with open(data_path, "rb") as f:
            content = f.read()

        logger.debug(f"Cache hit for key {cache_key}")
        return metadata, content

    except (IOError, json.JSONDecodeError, KeyError) as e:
        logger.warning(
            f"Failed to read or validate cache files for key {cache_key}: {e}"
        )
        _remove_cache_entry(cache_key, cache_dir)  # Clean up corrupted/invalid entry
        return None, None


def _save_to_cache(
    cache_key: str,
    cache_dir: Path,
    response: httpx.Response,
    cache_ttl: int,
) -> None:
    """Save response metadata and content to cache files."""
    metadata: CacheMetadata = {
        "expires_at": time.time() + cache_ttl,
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "encoding": response.encoding,
    }
    content = response.content

    meta_path = cache_dir / f"{cache_key}.meta.json"
    data_path = cache_dir / f"{cache_key}.data"

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f)
        with open(data_path, "wb") as f:
            f.write(content)
        logger.debug(f"Cached response for key {cache_key}")
    except IOError as e:
        logger.warning(f"Failed to write cache files for key {cache_key}: {e}")
        # Attempt cleanup of potentially partial write
        _remove_cache_entry(cache_key, cache_dir)


async def _get_cached_response(
    cache_key: str, cache_dir: Path, request: RequestDict, client: httpx.AsyncClient
) -> httpx.Response | None:
    """Attempt to retrieve a valid response from the cache."""
    # Use the refactored loading function
    metadata, content = _load_from_cache(cache_key, cache_dir)
    if metadata and content is not None:
        response = httpx.Response(
            status_code=metadata["status_code"],
            headers=metadata["headers"],
            content=content,
            request=client.build_request(
                method=request["method"],
                url=request["url"],
                params=request.get("params"),
                headers=request.get("headers"),
                json=request.get("json"),
                data=request.get("data"),
                content=request.get("content"),
            ),
        )
        # Restore encoding if it was saved
        if encoding := metadata.get("encoding"):
            try:
                response.encoding = encoding
            except LookupError:
                logger.warning(
                    f"Unknown encoding '{encoding}' found in cache, ignoring."
                )

        return response
    return None


async def _perform_request_with_retries(
    request: RequestDict,
    client: httpx.AsyncClient,
    retry_attempts: int,
    retry_wait_multiplier: int,
    retry_wait_max: int,
) -> httpx.Response:
    """Perform the HTTP request with configured retry logic."""

    async def _do_request() -> httpx.Response:  # noqa: WPS430
        logger.debug(f"{request['method']} {request['url']}")
        response = await client.request(
            method=request["method"],
            url=request["url"],
            params=request.get("params"),
            headers=request.get("headers"),
            json=request.get("json"),
            data=request.get("data"),
            content=request.get("content"),
        )
        # Trigger retry on server errors only
        if response.status_code >= 500:
            response.raise_for_status()
        return response

    send_request = retry(
        stop=stop_after_attempt(retry_attempts),
        wait=wait_exponential(multiplier=retry_wait_multiplier, max=retry_wait_max),
        reraise=True,
        retry_error_callback=lambda rs: logger.warning(
            f"Request failed after {rs.attempt_number} attempts: {request['url']}"
        ),
    )(_do_request)

    return await send_request()


async def _process_request_async(
    request: RequestDict,
    client: httpx.AsyncClient,
    config: BatchConfig,
) -> httpx.Response | Exception | None:
    """Return HTTP response or exception for *one* request, honouring cache & retries."""
    cache_key = _generate_cache_key(request) if config.cache_enabled else ""

    # Try cache first
    if config.cache_enabled:
        cached_response = await _get_cached_response(
            cache_key, config.cache_dir, request, client
        )
        if cached_response:
            return cached_response

    # Perform request & handle caching / errors
    try:
        response = await _perform_request_with_retries(
            request,
            client,
            config.retry_attempts,
            config.retry_wait_multiplier,
            config.retry_wait_max,
        )
        # Cache successful responses (2xx, 3xx). Avoid caching client/server errors.
        if config.cache_enabled and response.status_code < 400:
            _save_to_cache(cache_key, config.cache_dir, response, config.cache_ttl)
        return response
    except (httpx.HTTPError, RetryError, asyncio.TimeoutError) as exc:
        logger.error(f"Request failed for {request['url']}: {exc}")
        return exc
    except Exception as exc:  # noqa: BLE001 - broad except to log and surface
        logger.exception(f"Unexpected error processing request {request['url']}: {exc}")
        return exc


async def _run_async_batch(
    requests: Sequence[RequestDict],
    client: httpx.AsyncClient | None,
    config: BatchConfig,
) -> list[httpx.Response | Exception | None]:
    """Internal async helper executed via anyio.run."""

    async def _process_all(client_to_use: httpx.AsyncClient):  # noqa: WPS430
        tasks = [
            partial(
                _process_request_async,
                req,
                client_to_use,
                config,
            )
            for req in requests
        ]
        return await aiometer.run_all(
            tasks,
            max_at_once=config.max_concurrent,
            max_per_second=config.max_per_second,
        )

    # Respect caller-supplied client lifecycle
    if client is not None:
        return await _process_all(client)

    async with httpx.AsyncClient() as owned_client:
        return await _process_all(owned_client)


def run(
    requests: Sequence[str | RequestDict],
    *,
    client: httpx.AsyncClient | None = None,
    max_concurrent: int = 100,
    max_per_second: float = 50.0,
    cache: bool = True,
    cache_dir: str | Path | None = None,
    cache_ttl: int = 3600,  # Default: 1 hour
    retry_attempts: int = 3,
    retry_wait_exponential_multiplier: int = 1,
    retry_wait_exponential_max: int = 10,
) -> list[httpx.Response | Exception | None]:
    """
    Runs a batch of HTTP requests concurrently with rate limiting, retries, and caching.

    Args:
        requests: A sequence of requests. Each request can be a URL string (for GET)
                  or a dictionary specifying 'url', 'method', 'params', 'headers',
                  'json', 'data', or 'content'.
        client: An optional pre-configured httpx.AsyncClient instance. If None, a
                default client is created.
        max_concurrent: Maximum number of concurrent requests. Defaults to 100.
        max_per_second: Maximum requests per second. Defaults to 50.
        cache: Whether to enable file-based caching. Defaults to False.
        cache_dir: Directory for cache files. Defaults to a temporary directory if None.
        cache_ttl: Cache time-to-live in seconds. Defaults to 3600 (1 hour).
        retry_attempts: Number of retry attempts for failed requests. Defaults to 3.
        retry_wait_exponential_multiplier: Multiplier for exponential backoff delay
                                           between retries (in seconds). Defaults to 1.
        retry_wait_exponential_max: Maximum wait time between retries (in seconds).
                                    Defaults to 10.

    Returns:
        A list containing the results for each request, in the original order.
        Each result is either an httpx.Response object on success, or an Exception
        object if the request failed after all retries. Returns None if processing
        failed unexpectedly before/after the request itself.
    """
    if not requests:
        return []

    _cache_dir_path = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    if cache and not isinstance(_cache_dir_path, Path):
        raise ValueError("cache_dir must be a string or Path when cache is True")

    if cache and not _cache_dir_path.exists():
        try:
            _cache_dir_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created cache directory: {_cache_dir_path}")
        except OSError as e:
            logger.error(f"Failed to create cache directory {_cache_dir_path}: {e}")
            # Decide if this should be a fatal error or just disable caching
            cache = False  # Disable caching if directory creation fails
            logger.warning("Caching disabled due to directory creation failure.")
    elif cache and not _cache_dir_path.is_dir():
        logger.error(f"Cache path {_cache_dir_path} exists but is not a directory.")
        cache = False  # Disable caching if path is not a directory
        logger.warning("Caching disabled because cache path is not a directory.")

    # Create config object
    config = BatchConfig(
        max_concurrent=max_concurrent,
        max_per_second=max_per_second,
        cache_enabled=cache,
        cache_dir=_cache_dir_path,
        cache_ttl=cache_ttl,
        retry_attempts=retry_attempts,
        retry_wait_multiplier=retry_wait_exponential_multiplier,
        retry_wait_max=retry_wait_exponential_max,
    )

    try:
        normalized_requests = [_normalize_request(req) for req in requests]
    except ValueError as e:
        logger.error(f"Invalid request format: {e}")
        return [e] * len(requests)

    return anyio.run(
        _run_async_batch,
        normalized_requests,
        client,
        config,
    )
