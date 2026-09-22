SELECT SpanAttributes['director.provider'] AS provider,
       SpanAttributes['director.model'] AS model,
       SpanAttributes['director.execution_mode'] AS mode,
       SpanAttributes['director.room.type'] AS room_type,
       SpanAttributes['director.room.size'] AS room_size,
       count() AS rooms,
       avg(toFloat64OrNull(SpanAttributes['director.room.danger'])) AS mean_danger,
       avg(toFloat64OrNull(SpanAttributes['director.room.exit_count'])) AS mean_exits,
       countIf(SpanAttributes['director.room.has_secret'] IN ('true', 'True', '1')) / count() AS secret_rate,
       avg(toFloat64OrNull(SpanAttributes['director.room.enemy_density'])) AS mean_enemy_density,
       avg(toFloat64OrNull(SpanAttributes['director.room.loot_density'])) AS mean_loot_density
FROM otel_traces
WHERE ServiceName = {service:String}
  AND ResourceAttributes['deployment.environment'] = {environment:String}
  AND Timestamp >= parseDateTime64BestEffort({start:String})
  AND Timestamp < parseDateTime64BestEffort({end:String})
  AND SpanName = 'director.generate'
  AND SpanAttributes['director.status'] = 'success'
GROUP BY provider, model, mode, room_type, room_size
ORDER BY provider, model, mode, rooms DESC
