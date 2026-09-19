class_name ProviderSelector
extends Control
## Runtime Provider and Model Selector dialog / panel (Issue #12).
##
## Fetches config from GET /v1/config (via transport.fetch_config), populates
## available providers and models, handles unavailable providers gracefully,
## and allows selecting provider/model to apply to new runs or restarts.
##
## Compatible with both desktop and touch / Android interfaces.

signal selection_applied(provider: String, model: String)
signal selector_closed()

const DungeonContracts = preload("res://contracts/dungeon_contracts.gd")

@onready var panel: PanelContainer = $Panel
@onready var provider_option: OptionButton = $Panel/VBox/ProviderRow/ProviderOption
@onready var model_option: OptionButton = $Panel/VBox/ModelRow/ModelOption
@onready var status_label: Label = $Panel/VBox/StatusLabel
@onready var apply_btn: Button = $Panel/VBox/ButtonRow/ApplyBtn
@onready var cancel_btn: Button = $Panel/VBox/ButtonRow/CancelBtn
@onready var refresh_btn: Button = $Panel/VBox/ButtonRow/RefreshBtn

var config_data: DungeonContracts.DirectorConfigData = null
var current_provider := ""
var current_model := ""
var pending_provider := ""
var pending_model := ""
var is_selector_visible := false
var transport: Variant = null
var _config_refresh_id := 0


func _ready() -> void:
	if panel:
		panel.visible = is_selector_visible
	for btn in [apply_btn, cancel_btn, refresh_btn, provider_option, model_option]:
		if btn:
			btn.focus_mode = Control.FOCUS_NONE

	if provider_option and not provider_option.item_selected.is_connected(_on_provider_selected):
		provider_option.item_selected.connect(_on_provider_selected)
	if model_option and not model_option.item_selected.is_connected(_on_model_selected):
		model_option.item_selected.connect(_on_model_selected)
	if apply_btn and not apply_btn.pressed.is_connected(_on_apply_pressed):
		apply_btn.pressed.connect(_on_apply_pressed)
	if cancel_btn and not cancel_btn.pressed.is_connected(_on_cancel_pressed):
		cancel_btn.pressed.connect(_on_cancel_pressed)
	if refresh_btn and not refresh_btn.pressed.is_connected(refresh_config):
		refresh_btn.pressed.connect(refresh_config)


func open_selector(p_transport: Variant, active_provider: String, active_model: String) -> void:
	transport = p_transport
	current_provider = active_provider
	current_model = active_model
	pending_provider = active_provider
	pending_model = active_model
	set_selector_visible(true)
	refresh_config()


func set_selector_visible(p_visible: bool) -> void:
	is_selector_visible = p_visible
	if panel:
		panel.visible = is_selector_visible
	if not is_selector_visible:
		selector_closed.emit()


func refresh_config() -> void:
	_config_refresh_id += 1
	var refresh_id := _config_refresh_id
	if status_label:
		status_label.text = "Fetching director config..."
	if apply_btn:
		apply_btn.disabled = true
	if transport == null or not transport.has_method("fetch_config"):
		_on_config_loaded({"transport_ok": false, "error_kind": "offline"}, refresh_id)
		return
	transport.fetch_config(_on_config_loaded.bind(refresh_id))


func _on_config_loaded(res: Dictionary, refresh_id: int) -> void:
	if refresh_id != _config_refresh_id:
		return
	if not res.get("transport_ok", false):
		var err: String = str(res.get("error_kind", "offline"))
		_show_offline_or_error("Cannot reach director config (%s). Using offline fallback." % err)
		return

	var body := str(res.get("body", ""))
	var parsed := DungeonContracts.parse_director_config(body)
	if not parsed.ok:
		_show_offline_or_error("Invalid config from director: %s" % parsed.error)
		return

	config_data = DungeonContracts.DirectorConfigData.from_dict(parsed.config)
	_populate_ui()


func _show_offline_or_error(msg: String) -> void:
	config_data = null
	if status_label:
		status_label.text = msg
	if provider_option:
		provider_option.clear()
		provider_option.add_item("rules-baseline (offline)")
		provider_option.set_item_metadata(0, {"id": "rules-baseline", "available": true})
		provider_option.selected = 0
	if model_option:
		model_option.clear()
		model_option.add_item("builtin-v1")
		model_option.set_item_metadata(0, "builtin-v1")
		model_option.selected = 0
	pending_provider = "rules-baseline"
	pending_model = "builtin-v1"
	if apply_btn:
		apply_btn.disabled = false


func _populate_ui() -> void:
	if config_data == null or not provider_option or not model_option:
		return

	provider_option.clear()

	var selected_idx := 0
	var provider_to_select := pending_provider
	if provider_to_select == "":
		provider_to_select = config_data.default_provider

	for i in config_data.providers.size():
		var p := config_data.providers[i]
		var label := p.id
		if not p.available:
			label += " (unavailable)"
		provider_option.add_item(label)
		provider_option.set_item_metadata(i, {"id": p.id, "available": p.available})
		if p.id == provider_to_select:
			selected_idx = i

	provider_option.selected = selected_idx
	_on_provider_selected(selected_idx)


func _on_provider_selected(idx: int) -> void:
	var meta = provider_option.get_item_metadata(idx)
	var p_id: String = meta.id if meta is Dictionary else ""
	var available: bool = meta.available if meta is Dictionary else true
	pending_provider = p_id

	model_option.clear()
	var selected_model_idx := 0

	var provider_desc: DungeonContracts.ProviderDescriptorData = null
	if config_data:
		for p in config_data.providers:
			if p.id == p_id:
				provider_desc = p
				break

	if provider_desc != null:
		var target_model := pending_model
		if target_model == "" or not (target_model in provider_desc.models):
			target_model = provider_desc.default_model

		for m_idx in provider_desc.models.size():
			var m_name := provider_desc.models[m_idx]
			model_option.add_item(m_name)
			model_option.set_item_metadata(m_idx, m_name)
			if m_name == target_model:
				selected_model_idx = m_idx

		model_option.selected = selected_model_idx
		pending_model = provider_desc.models[selected_model_idx]
	else:
		model_option.add_item("default")
		model_option.set_item_metadata(0, "default")
		model_option.selected = 0
		pending_model = ""

	if status_label:
		if not available:
			status_label.text = "Provider '%s' is UNAVAILABLE and cannot be selected (no credentials or offline)." % p_id
		else:
			status_label.text = "Ready. Applies to new runs and restarts."

	if apply_btn:
		apply_btn.disabled = not available


func _on_model_selected(idx: int) -> void:
	pending_model = String(model_option.get_item_metadata(idx))


func _on_apply_pressed() -> void:
	if apply_btn and apply_btn.disabled:
		return
	current_provider = pending_provider
	current_model = pending_model
	set_selector_visible(false)
	selection_applied.emit(current_provider, current_model)


func _on_cancel_pressed() -> void:
	set_selector_visible(false)
