class_name SimulationMetrics
extends RefCounted
## Pure aggregate metrics over dataset rows (Issue #16). Every function takes
## plain arrays/dictionaries and returns JSON-serialisable values, rounded so
## the dataset bytes are stable.

const BUCKET_SIZE := 10
const PRECISION := 0.0001


static func r(value: float) -> float:
	return snappedf(value, PRECISION)


static func ratio(numerator: int, denominator: int) -> float:
	return 0.0 if denominator == 0 else r(float(numerator) / float(denominator))


static func histogram(values: Array) -> Dictionary:
	var counts := {}
	for value in values:
		var key := str(value)
		counts[key] = int(counts.get(key, 0)) + 1
	return counts


## Danger progression over commit order (`dangers` is one int per room).
static func danger_stats(dangers: Array) -> Dictionary:
	var n := dangers.size()
	if n == 0:
		return {"min": 0, "max": 0, "mean": 0.0, "slope": 0.0, "first_third_mean": 0.0, "last_third_mean": 0.0, "histogram": {}, "by_bucket": []}
	var total := 0
	for d in dangers:
		total += int(d)
	var third := maxi(1, n / 3)
	var buckets: Array = []
	for start in range(0, n, BUCKET_SIZE):
		var stop := mini(start + BUCKET_SIZE, n)
		buckets.append({"from": start, "to": stop - 1, "rooms": stop - start, "mean": _mean(dangers.slice(start, stop))})
	return {
		"min": dangers.min(),
		"max": dangers.max(),
		"mean": r(float(total) / n),
		"slope": slope(dangers),
		"first_third_mean": _mean(dangers.slice(0, third)),
		"last_third_mean": _mean(dangers.slice(n - third)),
		"histogram": histogram(dangers),
		"by_bucket": buckets,
	}


## Least-squares slope of value against its index (change per room).
static func slope(values: Array) -> float:
	var n := values.size()
	if n < 2:
		return 0.0
	var mean_x := float(n - 1) / 2.0
	var mean_y := 0.0
	for v in values:
		mean_y += float(v)
	mean_y /= n
	var num := 0.0
	var den := 0.0
	for i in range(n):
		num += (float(i) - mean_x) * (float(values[i]) - mean_y)
		den += (float(i) - mean_x) * (float(i) - mean_x)
	return 0.0 if den == 0.0 else r(num / den)


## Enemy/item distribution over room rows (each has `enemies`/`items` counts
## by type and `enemy_count`/`item_count`).
static func resource_stats(rooms: Array) -> Dictionary:
	var enemy_types := {}
	var item_types := {}
	var enemy_counts: Array = []
	var item_counts: Array = []
	for room in rooms:
		for t in room.enemies:
			enemy_types[t] = int(enemy_types.get(t, 0)) + int(room.enemies[t])
		for t in room.items:
			item_types[t] = int(item_types.get(t, 0)) + int(room.items[t])
		enemy_counts.append(int(room.enemy_count))
		item_counts.append(int(room.item_count))
	return {
		"enemies": {"total": _sum(enemy_counts), "by_type": enemy_types},
		"items": {"total": _sum(item_counts), "by_type": item_types},
		"per_room": {"enemies": _spread(enemy_counts), "items": _spread(item_counts)},
		"enemies_per_room_histogram": histogram(enemy_counts),
		"items_per_room_histogram": histogram(item_counts),
		"rooms_without_enemies": enemy_counts.count(0),
		"rooms_without_items": item_counts.count(0),
	}


## How repetitive the generated content is (per run, in commit order).
static func repetition_stats(rooms: Array) -> Dictionary:
	var n := rooms.size()
	var signatures := {}
	var types: Array = []
	var sizes: Array = []
	var longest := 0
	var streak := 0
	var previous_type := ""
	var previous_signature := ""
	var same_neighbour := 0
	for room in rooms:
		signatures[room.signature] = true
		types.append(room.room_type)
		sizes.append("%dx%d" % [room.width, room.height])
		streak = streak + 1 if room.room_type == previous_type else 1
		longest = maxi(longest, streak)
		previous_type = room.room_type
		if room.signature == previous_signature:
			same_neighbour += 1
		previous_signature = room.signature
	return {
		"unique_signatures": signatures.size(),
		"signature_repeat_rate": 0.0 if n == 0 else r(1.0 - float(signatures.size()) / n),
		"consecutive_signature_repeat_rate": ratio(same_neighbour, maxi(n - 1, 0)),
		"max_consecutive_same_type": longest,
		"type_histogram": histogram(types),
		"size_histogram": histogram(sizes),
	}


## Latency summary in ms, or null when no latency was measured.
static func latency_stats(values: Array) -> Variant:
	if values.is_empty():
		return null
	var sorted := values.duplicate()
	sorted.sort()
	var total := 0.0
	for v in sorted:
		total += float(v)
	return {
		"count": sorted.size(),
		"mean": r(total / sorted.size()),
		"p50": _percentile(sorted, 0.50),
		"p90": _percentile(sorted, 0.90),
		"p99": _percentile(sorted, 0.99),
		"max": sorted[sorted.size() - 1],
	}


static func _percentile(sorted: Array, fraction: float) -> Variant:
	var rank := clampi(int(ceil(fraction * sorted.size())) - 1, 0, sorted.size() - 1)
	return sorted[rank]


static func _sum(values: Array) -> int:
	var total := 0
	for v in values:
		total += int(v)
	return total


static func _mean(values: Array) -> float:
	if values.is_empty():
		return 0.0
	var total := 0.0
	for v in values:
		total += float(v)
	return r(total / values.size())


static func _spread(values: Array) -> Dictionary:
	if values.is_empty():
		return {"mean": 0.0, "stdev": 0.0, "min": 0, "max": 0}
	var mean_value := _mean(values)
	var variance := 0.0
	for v in values:
		variance += (float(v) - mean_value) * (float(v) - mean_value)
	variance /= values.size()
	return {"mean": mean_value, "stdev": r(sqrt(variance)), "min": values.min(), "max": values.max()}
