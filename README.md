# `abatcher`

Async HTTP request batcher with connection pooling and rate limiting.

## 🛠️ Usage

```python
from abatcher import AsyncHttpBatcher

# Create a batcher with a base URL and optional configuration
api = AsyncHttpBatcher(
    base_url="https://httpbin.org",
    max_concurrent=10,
    max_per_second=5,
    max_connections=50,
    timeout=30,
    retry_attempts=5,
)

# Simple GET request
result = api.get("/get")

print(f"Single request result: {result}")

# Batch of mixed requests
requests = [
    # Simple URL
    "/anything",
    # URL with params
    ("/anything", {"query": "test"}),
    # Full configuration
    {
        "url": "/post",
        "method": "POST",
        "params": {"name": "Test"},
        "headers": {"X-Custom": "value"},
    },
]

results = api.process_batch(requests)

print(f"Batch requests results: {results}")
```
