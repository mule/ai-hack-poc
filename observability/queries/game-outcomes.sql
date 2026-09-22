SELECT SpanAttributes['provider'] AS provider,
       SpanAttributes['model'] AS model,
       SpanAttributes['game.event.name'] AS event,
       SpanAttributes['fallback_reason'] AS fallback_reason,
       SpanAttributes['reject_reason'] AS reject_reason,
       SpanAttributes['normalize_reason'] AS normalize_reason,
       count() AS events,
       uniqExactIf(tuple(SpanAttributes['director.run_id'], SpanAttributes['director.request_id']),
                   SpanAttributes['director.request_id'] != '') AS distinct_requests
FROM otel_traces
WHERE ServiceName = {service:String}
  AND ResourceAttributes['deployment.environment'] = {environment:String}
  AND Timestamp >= parseDateTime64BestEffort({start:String})
  AND Timestamp < parseDateTime64BestEffort({end:String})
  AND SpanName = 'game.lifecycle'
GROUP BY provider, model, event, fallback_reason, reject_reason, normalize_reason
ORDER BY provider, model, event
