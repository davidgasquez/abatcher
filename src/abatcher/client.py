import httpx
import aiometer
import functools
import asyncio
from typing import Dict, Any


class AsyncHttpBatcher:
    def __init__(
        self,
        base_url: str | None = None,
        max_concurrent: int | None = None,
        max_per_second: int | None = None,
        max_connections: int | None = None,
        timeout: float | None = None,
        retry_attempts: int | None = None,
        http2: bool = False,
    ):
        """Initialize an async HTTP request batcher with connection pooling and rate limiting."""
        self.base_url = base_url
        self.max_concurrent = max_concurrent
        self.max_per_second = max_per_second

        client_kwargs = {}
        if http2:
            client_kwargs["http2"] = http2
        if base_url:
            client_kwargs["base_url"] = base_url
        if max_connections:
            client_kwargs["limits"] = httpx.Limits(max_connections=max_connections)
        if timeout:
            client_kwargs["timeout"] = timeout
        if retry_attempts:
            client_kwargs["transport"] = httpx.AsyncHTTPTransport(
                retries=retry_attempts
            )

        client_kwargs["follow_redirects"] = True
        self.client_kwargs = client_kwargs
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

    async def _fetch(
        self, client: httpx.AsyncClient, request: httpx.Request
    ) -> Dict[Any, Any]:
        """Execute a single request and return structured response."""
        try:
            response = await client.send(request)
            response.raise_for_status()
            return {
                "success": True,
                "data": response.json(),
                "url": str(request.url),
                "params": request.content.decode() if request.content else None,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "url": str(request.url),
                "params": request.content.decode() if request.content else None,
            }

    def process_batch(
        self,
        requests_data,
        method: str = "GET",
        timeout: float | None = None,
        raise_for_status: bool = False,
    ):
        """Process a batch of requests with rate limiting."""

        async def _process():
            # Override timeout if specified
            client_kwargs = self.client_kwargs.copy()
            if timeout:
                client_kwargs["timeout"] = timeout

            requests = []
            async with httpx.AsyncClient(**client_kwargs) as client:
                # Build request objects
                for item in requests_data:
                    if isinstance(item, dict):
                        url = item.get("url")
                        if not url:
                            raise ValueError("Request dict must contain 'url' key")
                        requests.append(
                            client.build_request(
                                method=item.get("method", method),
                                url=url,
                                json=item.get("params"),
                                headers=item.get("headers"),
                            )
                        )
                    elif isinstance(item, tuple):
                        url, params = item
                        requests.append(client.build_request(method, url, json=params))
                    else:
                        requests.append(client.build_request(method, item))

                async with aiometer.amap(
                    functools.partial(self._fetch, client),
                    requests,
                    max_at_once=self.max_concurrent,
                    max_per_second=self.max_per_second,
                ) as responses:
                    results = []
                    async for result in responses:
                        if raise_for_status and not result["success"]:
                            raise Exception(f"Request failed: {result['error']}")
                        results.append(result)

            return results

        return self.loop.run_until_complete(_process())

    def get(self, url: str, **kwargs) -> Dict[Any, Any]:
        """Convenience method for single GET request."""
        return self.process_batch([(url, kwargs)])[0]

    def post(
        self, url: str, data: Dict[Any, Any] | None = None, **kwargs
    ) -> Dict[Any, Any]:
        """Convenience method for single POST request."""
        kwargs["params"] = data
        return self.process_batch([{"url": url, "method": "POST", **kwargs}])[0]
