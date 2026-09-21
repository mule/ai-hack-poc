extends SceneTree
## Comprehensive headless test suite for RoomGenerator (Issue #6).
## Validates determinism, geometry connectivity, exit placement, collision safety,
## archetype variety, and normalization/rejection of invalid plans.

const DungeonContracts := preload("../contracts/dungeon_contracts.gd")
const GeneratedRoom := preload("../generation/generated_room.gd")
const RoomGenerator := preload("../generation/room_generator.gd")

var _checks := 0
var _failures := PackedStringArray()
var _active_marker := ""

func _initialize() -> void:
	print("=== Running test_room_generator.gd ===")
	_run_all_tests()
	_finish()

func _begin(test_name: String) -> void:
	_active_marker = "FAIL: %s aborted mid-run (runtime script error?)" % test_name
	_failures.append(_active_marker)

func _end() -> void:
	var idx := _failures.rfind(_active_marker)
	if idx >= 0:
		_failures.remove_at(idx)
	_active_marker = ""

func _assert_true(condition: bool, message: String) -> void:
	_checks += 1
	if not condition:
		_failures.append("Check %d FAILED: %s" % [_checks, message])
		print("  [FAIL] %s" % message)
	else:
		print("  [PASS] %s" % message)

func _assert_eq(actual: Variant, expected: Variant, message: String) -> void:
	_assert_true(actual == expected, "%s (expected %s, got %s)" % [message, str(expected), str(actual)])

func _finish() -> void:
	print("\n--- Test Results: %d Passed, %d Failed ---" % [_checks - _failures.size(), _failures.size()])
	if _failures.is_empty():
		print("SUCCESS: All room generator checks passed!")
		quit(0)
	else:
		for f in _failures:
			push_error(f)
		quit(1)

func _run_all_tests() -> void:
	_test_deterministic_reproduction()
	_test_fixture_room_generation()
	_test_canonical_repo_fixture_loading()
	_test_parsed_room_plan_data_path()
	_test_all_room_sizes()
	_test_all_room_archetypes()
	_test_archetype_shapes_are_distinct()
	_test_standard_room_shape_varies_by_seed()
	_test_exit_connectivity_and_direction()
	_test_simultaneous_up_and_down_stairs()
	_test_collision_and_spawn_safety()
	_test_seed_variation()
	_test_invalid_and_boundary_plans()
	_test_malformed_types_normalization_regression()
	_test_property_stress_loop()

# 1. Determinism: Same input + same seed = identical geometry and spawns
func _test_deterministic_reproduction() -> void:
	_begin("Deterministic reproduction test")
	var plan := {
		"room_id": "det-test-01",
		"depth": 2,
		"room_type": "room",
		"size": "medium",
		"danger": 2,
		"exits": [
			{"direction": "north", "kind": "door", "locked": false},
			{"direction": "east", "kind": "passage", "locked": false}
		],
		"enemy_density": 0.2,
		"loot_density": 0.3,
		"secret_probability": 0.5
	}
	var seed_val := 424242
	var room1 := RoomGenerator.generate(plan, seed_val)
	var room2 := RoomGenerator.generate(plan, seed_val)

	_assert_eq(room1.width, room2.width, "Widths match")
	_assert_eq(room1.height, room2.height, "Heights match")
	_assert_eq(room1.player_spawn, room2.player_spawn, "Player spawn matches")
	_assert_eq(room1.tiles, room2.tiles, "Tile layout is bit-for-bit identical")
	_assert_eq(room1.enemies, room2.enemies, "Enemy placements match")
	_assert_eq(room1.items, room2.items, "Item placements match")
	_assert_eq(room1.secrets, room2.secrets, "Secrets match")
	_assert_eq(room1.exits, room2.exits, "Exits match")
	_end()

# 2. Fixture plans: Valid connected rooms from standard fixture data
func _test_fixture_room_generation() -> void:
	_begin("Fixture room generation")
	var fixture_plan := {
		"room_id": "r-004",
		"depth": 3,
		"room_type": "room",
		"size": "small",
		"danger": 2,
		"exits": [
			{"direction": "south", "kind": "door", "locked": false},
			{"direction": "east", "kind": "passage", "locked": false}
		],
		"enemy_density": 0.15,
		"loot_density": 0.45,
		"secret_probability": 0.2,
		"has_secret": false,
		"environmental_tags": ["dark"],
		"description": "A dusty larder; rations may be stashed here."
	}
	var room := RoomGenerator.generate(fixture_plan, 12345)
	_assert_true(room.is_valid(), "Fixture room is valid")
	_assert_eq(room.room_id, "r-004", "Room ID preserved")
	_assert_true(_verify_connectivity(room), "All floor and exit tiles are reachable from player spawn")
	_end()

# 3. Canonical repo fixture loading
func _test_canonical_repo_fixture_loading() -> void:
	_begin("Canonical repo fixture loading")
	var fixtures_dir := _find_fixtures_dir()
	_assert_true(fixtures_dir != "", "Found fixtures directory: %s" % fixtures_dir)
	if fixtures_dir == "":
		_end()
		return

	var file_path := fixtures_dir.path_join("generation_response.json")
	var file := FileAccess.open(file_path, FileAccess.READ)
	_assert_true(file != null, "Can read generation_response.json from canonical fixtures")
	if file == null:
		_end()
		return

	var json_text := file.get_as_text()
	var parsed := DungeonContracts.parse_generation_response(json_text)
	_assert_true(parsed.ok, "Parsed generation response from fixture file")
	if not parsed.ok or not parsed.response.has("room"):
		_end()
		return

	var room_dict: Dictionary = parsed.response.room
	var room := RoomGenerator.generate(room_dict, 55555)
	_assert_true(room.is_valid(), "Room generated from repo fixture is valid")
	_assert_eq(room.room_id, "r-004", "Room ID matches fixture r-004")
	_assert_true(_verify_connectivity(room), "Room from repo fixture is fully connected")
	_end()

# 4. Parsed DungeonContracts.RoomPlanData path
func _test_parsed_room_plan_data_path() -> void:
	_begin("DungeonContracts.RoomPlanData path")
	var raw_plan := {
		"room_id": "r-typed-plan",
		"depth": 4,
		"room_type": "chamber",
		"size": "medium",
		"danger": 3,
		"exits": [
			{"direction": "north", "kind": "door", "locked": false},
			{"direction": "south", "kind": "stairs", "locked": false}
		],
		"enemy_density": 0.25,
		"loot_density": 0.35,
		"secret_probability": 0.4,
		"has_secret": false,
		"environmental_tags": ["fungal"],
		"description": "A damp chamber with creeping spores."
	}
	var plan_data: DungeonContracts.RoomPlanData = DungeonContracts.RoomPlanData.from_room_plan(raw_plan)
	_assert_true(plan_data is DungeonContracts.RoomPlanData, "Created RoomPlanData instance")
	_assert_eq(plan_data.room_id, "r-typed-plan", "RoomPlanData room_id is correct")

	var room := RoomGenerator.generate(plan_data, 78910)
	_assert_true(room.is_valid(), "Room generated from RoomPlanData is valid")
	_assert_eq(room.room_id, "r-typed-plan", "Room ID matches RoomPlanData")
	_assert_true(_verify_connectivity(room), "Room generated from RoomPlanData is fully connected")
	_end()

# 5. Sizes: Test all contract sizes (tiny, small, medium, large, huge)
func _test_all_room_sizes() -> void:
	_begin("All room sizes test")
	for size_name in DungeonContracts.ROOM_SIZES:
		var plan := {
			"room_id": "size-" + size_name,
			"depth": 1,
			"room_type": "room",
			"size": size_name,
			"exits": [{"direction": "north", "kind": "door"}]
		}
		var room := RoomGenerator.generate(plan, 777)
		_assert_true(room.is_valid(), "Room with size %s generated successfully" % size_name)
		_assert_true(_verify_connectivity(room), "Room %s has fully connected interior" % size_name)
	_end()

# 6. Archetypes: Test every room_type defined in contracts
func _test_all_room_archetypes() -> void:
	_begin("All room archetypes test")
	for rtype in DungeonContracts.ROOM_TYPES:
		var plan := {
			"room_id": "arch-" + rtype,
			"depth": 1,
			"room_type": rtype,
			"size": "medium",
			"exits": [
				{"direction": "north", "kind": "door"},
				{"direction": "south", "kind": "passage"}
			],
			"enemy_density": 0.2,
			"loot_density": 0.3
		}
		var room := RoomGenerator.generate(plan, 888)
		_assert_true(room.is_valid(), "Archetype %s produces valid room" % rtype)
		_assert_true(_verify_connectivity(room), "Archetype %s is fully navigable" % rtype)
		if rtype in ["entrance", "shop", "stairs_up", "shrine"]:
			_assert_eq(room.enemies.size(), 0, "Peaceful archetype %s has no enemies" % rtype)
	_end()


func _test_archetype_shapes_are_distinct() -> void:
	_begin("Archetype silhouette variety")
	var seen: Dictionary = {}
	for rtype in ["room", "chamber", "vault", "shrine", "treasure", "shop", "stairs_down"]:
		var plan := {
			"room_id": "shape-" + rtype,
			"depth": 2,
			"room_type": rtype,
			"size": "medium",
			"exits": [
				{"direction": "north", "kind": "door"},
				{"direction": "south", "kind": "door"}
			]
		}
		var room := RoomGenerator.generate(plan, 888)
		var signature := _walkable_shape_signature(room)
		_assert_true(not seen.has(signature), "%s has a distinct walkable silhouette" % rtype)
		seen[signature] = rtype
	_end()


func _test_standard_room_shape_varies_by_seed() -> void:
	_begin("Standard room seed variety")
	var plan := {
		"room_id": "seeded-room-shape",
		"depth": 2,
		"room_type": "room",
		"size": "small",
		"exits": [{"direction": "north", "kind": "door"}]
	}
	var signatures: Dictionary = {}
	for seed_value in range(1, 17):
		var room := RoomGenerator.generate(plan, seed_value)
		signatures[_walkable_shape_signature(room)] = true
	_assert_true(signatures.size() > 1, "Small standard rooms vary their geometry across seeds")
	_end()

# 7. Exit connectivity & direction
func _test_exit_connectivity_and_direction() -> void:
	_begin("Exit connectivity and direction test")
	var plan := {
		"room_id": "exits-all",
		"depth": 2,
		"room_type": "room",
		"size": "large",
		"exits": [
			{"direction": "north", "kind": "door"},
			{"direction": "south", "kind": "passage"},
			{"direction": "east", "kind": "secret"},
			{"direction": "west", "kind": "door"},
			{"direction": "up", "kind": "stairs"},
			{"direction": "down", "kind": "stairs"}
		]
	}
	var room := RoomGenerator.generate(plan, 999)
	_assert_eq(room.exits.size(), 6, "All 6 requested exits are generated")
	for ex in room.exits:
		var pos: Vector2i = ex.pos
		_assert_true(room.tiles.has(pos), "Exit at %s exists in tiles" % str(pos))
		_assert_true(room.is_traversable(pos), "Exit at %s is traversable" % str(pos))
	_assert_true(_verify_connectivity(room), "All 6 exits connect to the traversable geometry")
	_end()

# 8. Simultaneous up and down exits preserve distinct positions and correct tile identity
func _test_simultaneous_up_and_down_stairs() -> void:
	_begin("Simultaneous up and down stairs test")
	var plan := {
		"room_id": "dual-stairs",
		"depth": 3,
		"room_type": "chamber",
		"size": "medium",
		"exits": [
			{"direction": "up", "kind": "stairs"},
			{"direction": "down", "kind": "stairs"}
		]
	}
	var room := RoomGenerator.generate(plan, 12345)
	var up_exit: Dictionary = {}
	var down_exit: Dictionary = {}
	for ex in room.exits:
		if ex.direction == "up":
			up_exit = ex
		elif ex.direction == "down":
			down_exit = ex

	_assert_true(not up_exit.is_empty(), "Up exit exists")
	_assert_true(not down_exit.is_empty(), "Down exit exists")
	_assert_true(up_exit.pos != down_exit.pos, "Up and down stairs have distinct coordinates")
	_assert_eq(room.get_tile(up_exit.pos), GeneratedRoom.TileType.STAIRS_UP, "Up stairs position has STAIRS_UP tile")
	_assert_eq(room.get_tile(down_exit.pos), GeneratedRoom.TileType.STAIRS_DOWN, "Down stairs position has STAIRS_DOWN tile")
	_assert_true(_verify_connectivity(room), "Both stairs are fully connected and reachable")
	_end()

# 9. Collision & Spawn Safety
func _test_collision_and_spawn_safety() -> void:
	_begin("Collision and spawn safety test")
	var plan := {
		"room_id": "safety-test",
		"depth": 3,
		"room_type": "cavern",
		"size": "medium",
		"danger": 4,
		"enemy_density": 0.5,
		"loot_density": 0.5,
		"exits": [{"direction": "south", "kind": "door"}]
	}
	var room := RoomGenerator.generate(plan, 54321)

	# Player spawn must be FLOOR and not on a wall or door
	_assert_eq(room.get_tile(room.player_spawn), GeneratedRoom.TileType.FLOOR, "Player spawn is on FLOOR")

	# No enemies on walls or doors or outside bounds
	for e in room.enemies:
		var pos: Vector2i = e.pos
		_assert_eq(room.get_tile(pos), GeneratedRoom.TileType.FLOOR, "Enemy %s spawn at %s is on FLOOR" % [e.type, str(pos)])
		_assert_true(pos != room.player_spawn, "Enemy does not spawn on player spawn")

	# No items on walls or doors
	for it in room.items:
		var pos: Vector2i = it.pos
		_assert_eq(room.get_tile(pos), GeneratedRoom.TileType.FLOOR, "Item %s spawn at %s is on FLOOR" % [it.type, str(pos)])

	_end()

# 10. Seed variation produces distinct layouts
func _test_seed_variation() -> void:
	_begin("Seed variation test")
	var plan := {
		"room_id": "seed-var",
		"depth": 2,
		"room_type": "cavern",
		"size": "large",
		"danger": 3,
		"enemy_density": 0.3,
		"loot_density": 0.3,
		"exits": [{"direction": "north", "kind": "door"}]
	}
	var room_a := RoomGenerator.generate(plan, 1111)
	var room_b := RoomGenerator.generate(plan, 2222)
	# Spawns or tiles should differ between different seeds
	var differs := (room_a.tiles != room_b.tiles) or (room_a.enemies != room_b.enemies) or (room_a.items != room_b.items)
	_assert_true(differs, "Different seeds produce different layouts or entity distributions")
	_end()

# 11. Invalid and boundary plans are normalized or safely handled with diagnostics
func _test_invalid_and_boundary_plans() -> void:
	_begin("Invalid and boundary plans handling")

	# Odd/invalid plan: negative depth, invalid room_type, invalid size, duplicate exits
	var odd_plan := {
		"room_id": "odd_plan_1",
		"depth": -5,
		"room_type": "flying_spaceship",
		"size": "colossal_galactic",
		"danger": 999,
		"enemy_density": 15.0,
		"loot_density": -2.0,
		"exits": [
			{"direction": "north", "kind": "door"},
			{"direction": "north", "kind": "door"}, # duplicate
			{"direction": "outer_space", "kind": "teleport"}
		]
	}
	var room := RoomGenerator.generate(odd_plan, 9999)
	_assert_true(room.is_valid(), "Odd plan generates a valid playable room")
	_assert_true(room.diagnostics.size() > 0, "Diagnostics record normalization steps")
	_assert_true(_verify_connectivity(room), "Odd plan room is fully connected")

	# Non-dictionary input
	var corrupt_input = "not a dict"
	var fallback_room := RoomGenerator.generate(corrupt_input, 1)
	_assert_true(fallback_room.is_valid(), "Corrupt input safely generates fallback room")
	_assert_true(fallback_room.diagnostics.size() > 0, "Diagnostics record invalid input")

	_end()

# 12. Regression coverage for malformed exits and wrong-typed values (e.g. {"exits": "north"})
func _test_malformed_types_normalization_regression() -> void:
	_begin("Malformed types normalization regression")

	# Direct probe that caused the reported SCRIPT ERROR: String assigned to Array
	var string_exits_plan := {
		"room_id": "malformed_exits_str",
		"exits": "north"
	}
	var room_str := RoomGenerator.generate(string_exits_plan, 123)
	_assert_true(room_str != null, "String exits plan returns non-null GeneratedRoom")
	_assert_true(room_str.is_valid(), "String exits plan produces valid room")
	_assert_true(room_str.diagnostics.size() > 0, "Diagnostics record non-array exits normalization")

	# Wrong-type fields: int/float/string/dict/array in hostile combinations
	var hostile_types_plan := {
		"room_id": 12345, # non-string id
		"depth": "deep_abyss", # non-int depth
		"room_type": ["not", "a", "string"], # array room_type
		"size": {"width": 10}, # dict size
		"danger": "lethal", # string danger
		"enemy_density": "high", # string density
		"loot_density": null, # null density
		"secret_probability": [0.5], # array secret_probability
		"has_secret": "true_string", # non-bool has_secret
		"exits": [
			"north", # non-dict exit item
			{"direction": 42}, # non-string direction
			{"direction": "north", "kind": 999, "locked": "not_bool"}, # malformed fields
			null # null exit item
		],
		"environmental_tags": "flooded", # non-array tags
		"description": {"text": "lore"} # non-string description
	}
	var room_hostile := RoomGenerator.generate(hostile_types_plan, 456)
	_assert_true(room_hostile != null, "Hostile types plan returns non-null GeneratedRoom")
	_assert_true(room_hostile.is_valid(), "Hostile types plan produces valid room")
	_assert_true(_verify_connectivity(room_hostile), "Hostile types plan produces connected room")
	_assert_true(room_hostile.diagnostics.size() > 0, "Diagnostics record hostile fields normalization")
	_end()

# 13. Property / Stress Loop across >= 256 varied seeds and plans
func _test_property_stress_loop() -> void:
	_begin("Property stress loop (256 varied plans/seeds)")
	var rng := RandomNumberGenerator.new()
	rng.seed = 987654321

	var sizes: PackedStringArray = DungeonContracts.ROOM_SIZES
	var types: PackedStringArray = DungeonContracts.ROOM_TYPES
	var dirs: PackedStringArray = DungeonContracts.EXIT_DIRECTIONS

	var pass_count := 0
	var num_iterations := 256

	for iter in range(num_iterations):
		var seed_val := rng.randi()
		var chosen_size := sizes[rng.randi() % sizes.size()]
		var chosen_type := types[rng.randi() % types.size()]
		var chosen_danger := rng.randi_range(1, 5)
		var enemy_d := rng.randf()
		var loot_d := rng.randf()
		var secret_p := rng.randf()

		# Generate 0 to 4 unique exits
		var num_exits := rng.randi_range(0, 4)
		var exit_dirs := dirs.duplicate()
		_shuffle_packed_strings(exit_dirs, rng)
		var exits_arr: Array = []
		for e in range(num_exits):
			exits_arr.append({
				"direction": exit_dirs[e],
				"kind": "door" if rng.randf() > 0.3 else ("stairs" if (exit_dirs[e] in ["up", "down"]) else "passage"),
				"locked": rng.randf() < 0.2
			})

		var plan := {
			"room_id": "stress-%d" % iter,
			"depth": rng.randi_range(1, 10),
			"room_type": chosen_type,
			"size": chosen_size,
			"danger": chosen_danger,
			"enemy_density": enemy_d,
			"loot_density": loot_d,
			"secret_probability": secret_p,
			"exits": exits_arr
		}

		var room := RoomGenerator.generate(plan, seed_val)

		# Invariants to verify:
		# 1. Valid room
		if not room.is_valid():
			_failures.append("Stress iter %d: room is not valid" % iter)
			break

		# 2. Dimensions match size and tiles.size() == width * height
		var expected_dims: Vector2i = RoomGenerator.SIZE_DIMENSIONS[chosen_size]
		if room.width != expected_dims.x or room.height != expected_dims.y:
			_failures.append("Stress iter %d: dimensions mismatch" % iter)
			break
		if room.tiles.size() != (room.width * room.height):
			_failures.append("Stress iter %d: tiles count %d != %d (%dx%d)" % [iter, room.tiles.size(), room.width * room.height, room.width, room.height])
			break

		# 3. Every tile coordinate is in bounds [0..width-1, 0..height-1]
		var tiles_in_bounds := true
		for pos: Vector2i in room.tiles.keys():
			if pos.x < 0 or pos.x >= room.width or pos.y < 0 or pos.y >= room.height:
				tiles_in_bounds = false
				break
		if not tiles_in_bounds:
			_failures.append("Stress iter %d: out of bounds tile coordinate" % iter)
			break

		# 4. Connectivity
		if not _verify_connectivity(room):
			_failures.append("Stress iter %d: room is not fully connected" % iter)
			break

		# 5. Generated exit count and direction set exactly match requested set
		if room.exits.size() != exits_arr.size():
			_failures.append("Stress iter %d: exit count mismatch (expected %d, got %d)" % [iter, exits_arr.size(), room.exits.size()])
			break
		var req_dirs: Dictionary = {}
		for ex_req in exits_arr:
			req_dirs[ex_req["direction"]] = true
		var gen_dirs: Dictionary = {}
		for ex_gen in room.exits:
			gen_dirs[ex_gen["direction"]] = true
		if req_dirs != gen_dirs:
			_failures.append("Stress iter %d: exit directions mismatch" % iter)
			break

		# 6. Exits tile identity & distinct positions
		var exit_positions := {}
		var exits_ok := true
		for ex in room.exits:
			var ex_pos: Vector2i = ex.pos
			if exit_positions.has(ex_pos):
				exits_ok = false
				break
			exit_positions[ex_pos] = true
			var t: int = room.get_tile(ex_pos)
			if ex.direction == "up" and t != GeneratedRoom.TileType.STAIRS_UP:
				exits_ok = false
				break
			elif ex.direction == "down" and t != GeneratedRoom.TileType.STAIRS_DOWN:
				exits_ok = false
				break
			elif not room.is_traversable(ex_pos):
				exits_ok = false
				break

		if not exits_ok:
			_failures.append("Stress iter %d: exit tile identity or overlap error" % iter)
			break

		# 7. Player spawn must be FLOOR and within bounds
		if room.get_tile(room.player_spawn) != GeneratedRoom.TileType.FLOOR:
			_failures.append("Stress iter %d: player spawn is not on FLOOR" % iter)
			break
		if room.player_spawn.x < 1 or room.player_spawn.x >= room.width - 1 or room.player_spawn.y < 1 or room.player_spawn.y >= room.height - 1:
			_failures.append("Stress iter %d: player spawn is out of interior bounds" % iter)
			break

		# 8. Enemy positions: all on FLOOR, bounded count, no overlap with player or other enemies
		if room.enemies.size() > 8:
			_failures.append("Stress iter %d: enemy count %d exceeds cap 8" % [iter, room.enemies.size()])
			break
		var enemy_positions := {}
		var enemies_ok := true
		for en in room.enemies:
			var epos: Vector2i = en.pos
			if room.get_tile(epos) != GeneratedRoom.TileType.FLOOR:
				enemies_ok = false
				break
			if epos == room.player_spawn or enemy_positions.has(epos):
				enemies_ok = false
				break
			enemy_positions[epos] = true
		if not enemies_ok:
			_failures.append("Stress iter %d: enemy spawn invalid or overlapping player/enemy" % iter)
			break

		# 9. Secret positions: on FLOOR, bounded, do not overlap player or enemies
		if room.secrets.size() > 1:
			_failures.append("Stress iter %d: secrets count exceeds cap" % iter)
			break
		var secret_positions := {}
		var secrets_ok := true
		for sc in room.secrets:
			var spos: Vector2i = sc.pos
			if room.get_tile(spos) != GeneratedRoom.TileType.FLOOR:
				secrets_ok = false
				break
			if spos == room.player_spawn or enemy_positions.has(spos) or secret_positions.has(spos):
				secrets_ok = false
				break
			secret_positions[spos] = true
		if not secrets_ok:
			_failures.append("Stress iter %d: secret spawn invalid or overlapping player/enemy" % iter)
			break

		# 10. Item positions: on FLOOR, bounded count, do not overlap player or enemies,
		# no unintended overlap among items (except intentional secret cache + bonus item co-location)
		if room.items.size() > 10:
			_failures.append("Stress iter %d: items count %d exceeds cap" % [iter, room.items.size()])
			break
		var item_positions := {}
		var items_ok := true
		for it in room.items:
			var ipos: Vector2i = it.pos
			if room.get_tile(ipos) != GeneratedRoom.TileType.FLOOR:
				items_ok = false
				break
			if ipos == room.player_spawn or enemy_positions.has(ipos):
				items_ok = false
				break
			if item_positions.has(ipos):
				# Only allowable item co-location is if it matches a secret cache position
				if not secret_positions.has(ipos):
					items_ok = false
					break
			item_positions[ipos] = true
		if not items_ok:
			_failures.append("Stress iter %d: item spawn invalid or overlapping player/enemy/item" % iter)
			break

		# 11. Peaceful room archetypes have 0 enemies
		if chosen_type in ["entrance", "shop", "stairs_up", "shrine"]:
			if room.enemies.size() != 0:
				_failures.append("Stress iter %d: peaceful archetype %s has enemies" % [iter, chosen_type])
				break

		# 12. Same input + seed reproduces exact same room
		var room_repro := RoomGenerator.generate(plan, seed_val)
		if room.tiles != room_repro.tiles or room.player_spawn != room_repro.player_spawn or room.enemies != room_repro.enemies or room.items != room_repro.items or room.secrets != room_repro.secrets or room.exits != room_repro.exits:
			_failures.append("Stress iter %d: failed deterministic reproduction" % iter)
			break

		pass_count += 1

	_assert_eq(pass_count, num_iterations, "All 256 stress property iterations passed successfully")
	_end()


func _shuffle_packed_strings(arr: PackedStringArray, rng: RandomNumberGenerator) -> void:
	for i in range(arr.size() - 1, 0, -1):
		var j := rng.randi_range(0, i)
		var tmp := arr[i]
		arr[i] = arr[j]
		arr[j] = tmp


func _walkable_shape_signature(room: GeneratedRoom) -> String:
	var signature := ""
	for y in range(room.height):
		for x in range(room.width):
			signature += "#" if room.get_tile(Vector2i(x, y)) == GeneratedRoom.TileType.WALL else "."
	return signature


## Utility: Verifies that all FLOOR and EXIT tiles form a single connected component
## containing the player spawn via BFS.
func _verify_connectivity(room: GeneratedRoom) -> bool:
	if room.player_spawn == Vector2i(-1, -1):
		return false
	if not room.is_traversable(room.player_spawn):
		return false

	var visited := {}
	var queue: Array[Vector2i] = [room.player_spawn]
	visited[room.player_spawn] = true

	var dirs = [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]

	while not queue.is_empty():
		var curr: Vector2i = queue.pop_front()
		for d: Vector2i in dirs:
			var n: Vector2i = curr + d
			if room.tiles.has(n) and not visited.has(n):
				if room.is_traversable(n):
					visited[n] = true
					queue.append(n)

	# Check all floor tiles are visited
	for pos: Vector2i in room.tiles.keys():
		var t: int = room.tiles[pos]
		if t == GeneratedRoom.TileType.FLOOR and not visited.has(pos):
			print("Unreachable floor tile found at: %s" % str(pos))
			return false

	# Check all exits are visited
	for ex in room.exits:
		var epos: Vector2i = ex.pos
		if not visited.has(epos):
			print("Unreachable exit found at: %s" % str(epos))
			return false

	return true


func _find_fixtures_dir() -> String:
	var env_dir := OS.get_environment("DUNGEON_CONTRACTS_DIR")
	if not env_dir.is_empty() and DirAccess.dir_exists_absolute(env_dir):
		return env_dir
	var script_path := String(get_script().get_path())
	if script_path.begins_with("res://"):
		script_path = ProjectSettings.globalize_path(script_path)
	var script_dir := script_path.get_base_dir()
	var pwd := OS.get_environment("PWD")
	var candidates: PackedStringArray = [
		script_dir.path_join("../../contracts/fixtures"),
		script_dir.path_join("../../../contracts/fixtures"),
		"res://contracts/fixtures",
		pwd.path_join("contracts/fixtures"),
		pwd.path_join("../contracts/fixtures"),
	]
	for candidate: String in candidates:
		var path: String = candidate
		if path.begins_with("res://"):
			path = ProjectSettings.globalize_path(path)
		if DirAccess.dir_exists_absolute(path):
			return path
	return ""
