# `abatcher`

Simple parallel HTTP request batcher with rate limiting, retries, connection pooling, and more. The entire API is only 1 function.

## 🛠️ Usage

Using abatcher should be as simple as:

```python
import abatcher

requests = [
    # Simple URL GET request
    "https://httpbin.org/anything",
    # Custom request
    {
        "url": "https://httpbin.org/post",
        "method": "POST",
        "params": {"name": "Test"},
        "headers": {"X-Custom": "value"},
    },
]

results = abatcher.run(requests)

print(f"Batch requests results: {results}")
```

If you need more control, you can also send a custom client.

```python
import httpx
import abatcher

custom_client = httpx.AsyncClient(
    auth=("user", "pass"),
    timeout=httpx.Timeout(45.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
)

results = abatcher.run(
    requests,
    client=custom_client,
    max_concurrent=20,
    max_per_second=10,
    cache=True,
    cache_dir="custom_cache",
    cache_ttl=3600,
)

print(f"Batch requests results: {results}")

```
