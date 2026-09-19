# Sanitized Jev samples (issue #8)

`sample_request.json` is the exact JSON body the `cloudflare-jev` adapter
sends to `POST /accounts/<account id>/ai/run`, recorded by running
`build_jev_state`/`build_jev_questions` on `contracts/fixtures/generation_request.json`.
It contains no secrets: credentials travel only in the
`Authorization: Bearer <token>` header, which is never recorded here.

`sample_response.json` shows the documented success shape: the Jev payload
(`model`, `answers`, `usage`) inside the standard Cloudflare v4 REST envelope
(`result`, `success`, `errors`, `messages`). The answer values are modeled on
the official examples at
<https://developers.cloudflare.com/ai/models/typesafe/jev/> (see
`director/docs/cloudflare-jev.md` for sources and assumptions). This is a
**doc-derived sample, not a live capture**; live verification requires
credentials and is covered by the skipped-by-default live tests
(`director/tests/test_cloudflare_jev_live.py`).
