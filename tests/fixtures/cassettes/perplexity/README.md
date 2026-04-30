# Perplexity Sonar cassettes

Hermetic cassettes for `tests/test_perplexity_client.py`. The fake
session in the test module replays these JSON files in order; the live
Perplexity API is never contacted by `pytest`.

Sentinel API key (`pplx-test-sentinel-...`) is the *only* token that
appears anywhere in this directory. The validator
(`PYTEST_DISABLE_NETWORK=1 pytest tests/test_perplexity_client.py`)
greps the cassettes for `pplx-[A-Za-z0-9_-]{8,}` and refuses to ship
the worker if any real-looking key surfaces.

Re-recording cassettes against the live `api.perplexity.ai` endpoint
should be done on the VPS (where `PERPLEXITY_API_KEY` is provisioned)
by the `vps-deploy-worker`, with sentinel redaction applied at record
time.
