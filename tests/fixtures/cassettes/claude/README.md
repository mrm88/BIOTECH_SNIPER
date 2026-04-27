# Anthropic / Claude Opus Cassettes

Hermetic fixture cassettes for `tests/test_claude_client.py`.

Each cassette is a JSON file describing the canned responses a fake
Anthropic SDK client returns. The fake client mimics the
`anthropic.Anthropic` surface that `ClaudeClient` calls into:

* `messages.create(...)` returns a `Message` with `content`, `usage`,
  `id`, `model` attributes.
* `messages.stream(...)` returns a context manager exposing
  `text_stream` (iterator of partial text deltas) and
  `get_final_message()` (returns the assembled `Message`).

Cassette schema (`type` is required on every interaction):

```jsonc
{
  "interactions": [
    {
      "type": "create",                         // or "stream"
      "result": {
        "id": "msg_xxx",
        "model": "claude-opus-4-1-20250805",
        "content_text": "{\"science_profile\": {...}, ...}",
        "usage": {"input_tokens": 320, "output_tokens": 410}
      }
    },
    {
      "type": "stream",
      "result": {
        "id": "msg_yyy",
        "model": "claude-opus-4-1-20250805",
        "content_chunks": ["{\"sci", "ence_profile\": ", "..."],
        "usage": {"input_tokens": 320, "output_tokens": 410}
      }
    },
    {
      "type": "error",                          // exception path
      "error": {
        "class": "AuthenticationError",         // or RateLimitError, etc.
        "message": "Invalid x-api-key"
      }
    }
  ]
}
```

The format is a deliberate minimal subset of VCR — enough for
deterministic replay, free of any HTTP / SSE machinery (the SDK abstracts
those away).

**Recording:** these cassettes are hand-crafted because the Anthropic
endpoint requires a real key. To record against the live API, run:

```
ssh root@199.247.25.111 'cd /root/alpha_sniper/repo && \
    .venv/bin/python -m biotech_sniper.llm.claude_client \
        --record tests/fixtures/cassettes/claude/<name>.json'
```

…and scrub the `Authorization` / `x-api-key` headers before committing.
The fake-client cassette replay path here never echoes headers, so the
cassettes in this directory contain no secret material.
