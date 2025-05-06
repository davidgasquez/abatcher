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
