# Google Gemini 2.5 Pro Cassettes

Hermetic fixture cassettes for `tests/test_gemini_client.py`.

Each cassette is a JSON file describing the canned responses a fake
Google GenAI SDK client returns. The fake client mimics the
`google.genai.Client` surface that `GeminiClient` calls into:

* `client.models.generate_content(...)` returns a `GenerateContentResponse`
  with `text` (concatenated string), `usage_metadata`, `model_version`,
  and `response_id` attributes.

Cassette schema (`type` is required on every interaction):

```jsonc
{
  "interactions": [
    {
      "type": "create",                         // success path
      "result": {
        "id": "resp_xxx",                        // surfaced as response_id
        "model": "gemini-2.5-pro",
        "content_text": "{\"science_profile\": {...}, ...}",
        "usage": {"input_tokens": 320, "output_tokens": 410}
      }
    },
    {
      "type": "error",                          // exception path
      "error": {
        "class": "ClientError",                  // or ServerError
        "code": 401,                             // HTTP status code
        "message": "API key not valid"
      }
    }
  ]
}
```

The format is a deliberate minimal subset of VCR — enough for
deterministic replay, free of any HTTP / SSE machinery (the SDK
abstracts those away).

The cassettes here contain no secret material — the fake client never
inspects request headers, and the model id / response id values are
scrubbed by construction.

**Recording:** these cassettes are hand-crafted because the Gemini
endpoint requires a real `GEMINI_API_KEY` (which is not yet provisioned
on the VPS at the time this feature lands). To record against the live
API once the key is supplied, run:

```
ssh root@199.247.25.111 'cd /root/alpha_sniper/repo && \
    .venv/bin/python -m biotech_sniper.llm.gemini_client \
        --record tests/fixtures/cassettes/gemini/<name>.json'
```

…and scrub the `x-goog-api-key` header before committing. The fake-client
cassette replay path here never echoes headers, so the cassettes in this
directory contain no secret material.
