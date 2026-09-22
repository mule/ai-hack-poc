-- Bind service, environment, start, end with ClickHouse query parameters.
SELECT SpanAttributes['director.provider'] AS provider,
       SpanAttributes['director.model'] AS model,
       SpanAttributes['director.execution_mode'] AS mode,
       count() AS samples,
       quantilesExact(0.50, 0.90, 0.95, 0.99)(Duration / 1000000.0) AS end_to_end_ms,
       countIf(mapContains(SpanAttributes, 'director.provider_latency_ms')) AS upstream_samples,
       quantilesExactIf(0.50, 0.90, 0.95, 0.99)(
         toFloat64OrZero(SpanAttributes['director.provider_latency_ms']),
         mapContains(SpanAttributes, 'director.provider_latency_ms')) AS upstream_ms
FROM otel_traces
WHERE ServiceName = {service:String}
  AND ResourceAttributes['deployment.environment'] = {environment:String}
  AND Timestamp >= parseDateTime64BestEffort({start:String})
  AND Timestamp < parseDateTime64BestEffort({end:String})
  AND SpanName = 'director.generate'
GROUP BY provider, model, mode
ORDER BY provider, model, mode
