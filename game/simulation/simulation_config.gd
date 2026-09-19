class_name SimulationConfig
extends RefCounted
## Settings for one simulation invocation (Issue #16) and their validation.
##
## The default is the safe, offline rules baseline: no network, no credentials,
## no cost. A remote provider is only ever contacted when `--remote` is passed
## explicitly. Endpoint / provider / model / budget flags without `--remote`
## are a usage error (never silently ignored), and the director environment
## variables the game reads (DUNGEON_DIRECTOR_*) only supply defaults *after*
## `--remote` has been given, so an ambient shell variable cannot enable it.

const TRANSPORT_RULES := "rules"
const TRANSPORT_OFFLINE := "offline"
const TRANSPORT_REMOTE := "remote"

const FAULT_PROFILES := ["none", "flaky", "hang"]
const POLICIES := ["random", "nearest"]

const RULES_PROVIDER := "rules-baseline"
const RULES_MODEL := "builtin-v1"

const DEFAULT_ENDPOINT := "http://127.0.0.1:8000"
const DEFAULT_MAX_REQUESTS := 300
const DEFAULT_TIMEOUT_SEC := 5.0
const MAX_RUNS := 1000
const MAX_STEPS := 10000
const ID_MAX_LENGTH := 128

var runs := 5
## Generation steps (frontier resolutions) to drive per run. A lower bound:
## prefetched neighbours that resolve in the same iteration are counted too.
var steps := 100
var base_seed := 1
var transport := TRANSPORT_RULES
var faults := "none"
var policy := "random"
var audit_every := 1
var out_dir := ""

# Remote-only settings.
var endpoint := ""
var provider := ""
var model := ""
var max_requests := DEFAULT_MAX_REQUESTS
var timeout_sec := DEFAULT_TIMEOUT_SEC

## Test/diagnostic override (msec): declare a stall after this long instead of
## the coordinator timeout plus a grace period. 0 = derive.
var stall_after_msec := 0


func is_remote() -> bool:
	return transport == TRANSPORT_REMOTE


static func usage() -> String:
	return "\n".join([
		"Dungeon simulation harness (offline rules baseline by default).",
		"",
		"Usage: make simulate [SIM_ARGS='<flags>']",
		"   or: godot --headless --path game -s res://simulation/run_simulation.gd -- <flags>",
		"",
		"  --runs N            independent dungeon runs (default 5, max %d)" % MAX_RUNS,
		"  --steps N           generation steps per run (default 100, max %d)" % MAX_STEPS,
		"  --seed N            base seed; run i uses seed N+i (default 1)",
		"  --policy P          frontier choice: random (default) | nearest",
		"  --transport T       rules (default: offline rules baseline answers as the director)",
		"                      | offline (every request fails: pure local fallback path)",
		"  --faults F          none (default) | flaky | hang: deterministic injected provider faults",
		"                      (rules transport only)",
		"  --audit-every N     audit world invariants every N steps (default 1; always at run end)",
		"  --out DIR           output directory (must not already hold a dataset)",
		"  --help              this text",
		"",
		"Remote provider mode - COST WARNING: REAL PROVIDER CALLS MAY BE BILLED.",
		"  --remote            explicit opt-in: send requests to a director over HTTP",
		"  --endpoint URL      director base URL (default $DUNGEON_DIRECTOR_URL or %s)" % DEFAULT_ENDPOINT,
		"  --provider ID       provider id (default $DUNGEON_DIRECTOR_PROVIDER, else the director's)",
		"  --model ID          model id (default $DUNGEON_DIRECTOR_MODEL, else the provider's)",
		"  --max-requests N    hard cap on requests actually sent (default %d)" % DEFAULT_MAX_REQUESTS,
		"  --timeout SEC       per-request timeout (default %s)" % str(DEFAULT_TIMEOUT_SEC),
	])


## Parse CLI arguments. `env` supplies DUNGEON_DIRECTOR_* defaults for remote
## mode only. Returns {ok:true, config, help:bool} or {ok:false, error}.
static func parse(args: PackedStringArray, env: Dictionary = {}) -> Dictionary:
	var config: SimulationConfig = new()
	var given := {}
	var i := 0
	while i < args.size():
		var arg := args[i]
		i += 1
		if arg == "--help" or arg == "-h":
			return {"ok": true, "config": config, "help": true}
		if not arg.begins_with("--"):
			return _fail("unexpected argument '%s' (see --help)" % arg)
		var name := arg.substr(2)
		var value := ""
		var has_value := false
		var eq := name.find("=")
		if eq >= 0:
			value = name.substr(eq + 1)
			name = name.substr(0, eq)
			has_value = true
		if name == "remote":
			if has_value:
				return _fail("--remote takes no value")
			given["remote"] = true
			continue
		if not (name in ["runs", "steps", "seed", "policy", "transport", "faults", "audit-every", "out", "endpoint", "provider", "model", "max-requests", "timeout"]):
			return _fail("unknown flag '--%s' (see --help)" % name)
		if not has_value:
			if i >= args.size():
				return _fail("--%s needs a value" % name)
			value = args[i]
			i += 1
		given[name] = value

	var err := config._apply(given)
	if err != "":
		return _fail(err)
	err = config._apply_remote(given, env)
	if err != "":
		return _fail(err)
	return {"ok": true, "config": config, "help": false}


func provider_summary() -> Dictionary:
	match transport:
		TRANSPORT_REMOTE:
			return {"mode": "remote", "provider": provider if provider != "" else null, "model": model if model != "" else null, "endpoint": endpoint}
		TRANSPORT_OFFLINE:
			return {"mode": "offline-fallback", "provider": null, "model": null, "endpoint": null}
		_:
			return {"mode": "rules-baseline", "provider": RULES_PROVIDER, "model": RULES_MODEL, "endpoint": null}


## Conspicuous multi-line warning; empty unless remote calls will be made.
func cost_warning() -> String:
	if not is_remote():
		return ""
	var bar := "!".repeat(72)
	return "\n".join([
		bar,
		"!!  COST WARNING: REMOTE PROVIDER MODE                                 !!",
		bar,
		"This run will send up to %d generation requests over the network to:" % max_requests,
		"    endpoint: %s" % endpoint,
		"    provider: %s" % (provider if provider != "" else "(director default)"),
		"    model:    %s" % (model if model != "" else "(provider default)"),
		"Real providers may bill per request or token, and may rate-limit you.",
		"Requests beyond the cap are NOT sent; they are answered by the local rules",
		"baseline and recorded as 'budget_exhausted' fallbacks. Stop with Ctrl+C.",
		"Datasets record endpoint, provider, model and reported cost where given.",
		bar,
	])


# --- internals ---------------------------------------------------------------


static func _fail(message: String) -> Dictionary:
	return {"ok": false, "error": message}


func _apply(given: Dictionary) -> String:
	for entry in [["runs", 1, MAX_RUNS], ["steps", 1, MAX_STEPS], ["audit-every", 1, MAX_STEPS]]:
		if given.has(entry[0]):
			var parsed: Variant = _int_in(str(given[entry[0]]), entry[1], entry[2])
			if parsed == null:
				return "--%s must be an integer between %d and %d" % [entry[0], entry[1], entry[2]]
			match entry[0]:
				"runs":
					runs = parsed
				"steps":
					steps = parsed
				"audit-every":
					audit_every = parsed
	if given.has("seed"):
		var text := str(given.seed)
		if not text.is_valid_int():
			return "--seed must be an integer"
		base_seed = text.to_int()
	if given.has("policy"):
		if not (str(given.policy) in POLICIES):
			return "--policy must be one of %s" % ", ".join(POLICIES)
		policy = str(given.policy)
	if given.has("faults"):
		if not (str(given.faults) in FAULT_PROFILES):
			return "--faults must be one of %s" % ", ".join(FAULT_PROFILES)
		faults = str(given.faults)
	if given.has("transport"):
		if not (str(given.transport) in [TRANSPORT_RULES, TRANSPORT_OFFLINE]):
			return "--transport must be 'rules' or 'offline' (use --remote for a remote provider)"
		transport = str(given.transport)
	if given.has("out"):
		out_dir = str(given.out)
	if transport == TRANSPORT_OFFLINE and faults != "none":
		return "--faults applies to the rules transport; --transport offline already fails every request"
	return ""


func _apply_remote(given: Dictionary, env: Dictionary) -> String:
	var remote_flags: Array = []
	for name in ["endpoint", "provider", "model", "max-requests", "timeout"]:
		if given.has(name):
			remote_flags.append("--" + name)
	if not given.has("remote"):
		if not remote_flags.is_empty():
			return "%s apply only to a remote run: add --remote to opt in to real provider calls (costs may apply)" % ", ".join(remote_flags)
		return ""
	if transport != TRANSPORT_RULES:
		return "--remote cannot be combined with --transport %s" % transport
	if faults != "none":
		return "--faults simulates failures for the offline transports; it cannot be combined with --remote"
	transport = TRANSPORT_REMOTE

	endpoint = str(given.get("endpoint", env.get("DUNGEON_DIRECTOR_URL", ""))).strip_edges()
	if endpoint == "":
		endpoint = DEFAULT_ENDPOINT
	provider = str(given.get("provider", env.get("DUNGEON_DIRECTOR_PROVIDER", ""))).strip_edges()
	model = str(given.get("model", env.get("DUNGEON_DIRECTOR_MODEL", ""))).strip_edges()
	var endpoint_error := _endpoint_error(endpoint)
	if endpoint_error != "":
		return endpoint_error
	endpoint = endpoint.rstrip("/")
	for pair in [["provider", provider], ["model", model]]:
		if pair[1] != "" and not _valid_id(pair[1]):
			return "--%s must be 1..%d characters of letters, digits and . _ : / -" % [pair[0], ID_MAX_LENGTH]
	if given.has("max-requests"):
		var budget: Variant = _int_in(str(given["max-requests"]), 1, 1_000_000)
		if budget == null:
			return "--max-requests must be an integer between 1 and 1000000"
		max_requests = budget
	if given.has("timeout"):
		var text := str(given.timeout)
		if not text.is_valid_float() or text.to_float() < 0.1 or text.to_float() > 300.0:
			return "--timeout must be a number of seconds between 0.1 and 300"
		timeout_sec = text.to_float()
	return ""


static func _int_in(text: String, low: int, high: int) -> Variant:
	if not text.is_valid_int():
		return null
	var value := text.to_int()
	if value < low or value > high:
		return null
	return value


static func _endpoint_error(url: String) -> String:
	var lower := url.to_lower()
	if not (lower.begins_with("http://") or lower.begins_with("https://")):
		return "--endpoint must start with http:// or https://"
	var rest := url.substr(url.find("://") + 3)
	var authority := rest.get_slice("/", 0)
	if "@" in authority:
		return "--endpoint must not embed credentials (user:password@host); credentials belong to the director"
	if authority == "":
		return "--endpoint needs a host"
	if "?" in url or "#" in url:
		return "--endpoint must be a base URL without a query string or fragment"
	return ""


static func _valid_id(value: String) -> bool:
	if value.length() < 1 or value.length() > ID_MAX_LENGTH:
		return false
	for i in range(value.length()):
		var c := value[i]
		var ok := (c >= "a" and c <= "z") or (c >= "A" and c <= "Z") or (c >= "0" and c <= "9") or c in ["-", "_", ".", ":", "/"]
		if not ok:
			return false
	return true
