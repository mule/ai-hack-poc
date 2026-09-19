# Cerebras samples (issue #10)

Both files are **hand-written illustrations of the documented OpenAI-compatible
Chat Completions shapes. They are not captures of a live Cerebras call** (no
Cerebras credentials were available when the adapter was written). Offline tests
keep them honest; live tests (`director/tests/test_cerebras_live.py`, skipped
unless `RUN_LIVE_CEREBRAS=1` and `CEREBRAS_API_KEY` are set) are the only thing
that proves the adapter against the real service.

- `sample_request.json`: the exact JSON body the adapter sends for
  `contracts/fixtures/generation_request.json`. It is generated from the adapter
  and compared byte-for-byte by `test_sample_request_fixture_matches_what_the_adapter_sends`,
  so it cannot drift. Contains no secrets: the credential travels only in the
  `Authorization: Bearer <key>` header.
- `sample_response.json`: an illustrative completion whose message content is a
  valid `RoomPlan` for that request. `test_sample_response_fixture_yields_a_valid_room_through_the_service`
  pushes it through the real `DirectorService`.
