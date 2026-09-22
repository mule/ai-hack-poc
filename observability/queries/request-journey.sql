-- One run/request: high-cardinality IDs are filters on traces, never metric labels.
SELECT Timestamp, SpanName, TraceId, SpanId, ParentSpanId, Duration / 1000000.0 AS duration_ms,
       SpanAttributes, `Links.TraceId`
FROM otel_traces
WHERE ServiceName = {service:String}
  AND ResourceAttributes['deployment.environment'] = {environment:String}
  AND Timestamp >= parseDateTime64BestEffort({start:String})
  AND Timestamp < parseDateTime64BestEffort({end:String})
  AND SpanAttributes['director.run_id'] = {run:String}
  AND SpanAttributes['director.request_id'] = {request:String}
ORDER BY Timestamp, SpanId
