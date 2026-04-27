# xAI / Grok-4 Cassettes

Hermetic fixture cassettes for `tests/test_xai_client.py`.

Each cassette is a JSON file with an ordered list of `interactions`. Each
interaction records:

* `request.method` / `request.url` — what the client posts.
* `response.status_code` — HTTP status to return.
* `response.json` — JSON body (already parsed; the fake session
  re-serialises on demand).
* `response.text` — optional override when the body is not valid JSON
  (used by the malformed-body test).

The format is a minimal subset of VCR (vcrpy) cassettes — sufficient for
deterministic replay through the fake session in `tests/conftest_xai.py`
without taking on a hard dependency on vcrpy's request-matching internals.

**Recording:** these cassettes are hand-crafted because the Grok-4
endpoint requires an authenticated paper sandbox key that is only
available on the VPS. To record against the live endpoint, run:

```
ssh root@199.247.25.111 'cd /root/alpha_sniper/repo && \
    .venv/bin/python -m biotech_sniper.llm.xai_client \
        --record tests/fixtures/cassettes/xai/<name>.json'
```

…and scrub the `Authorization` header before committing. (The fake
session never echoes headers, so cassettes here never contain secrets.)
