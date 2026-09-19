class_name GameUI
extends Control

const GameState = preload("res://src/game_state.gd")

signal dpad_pressed(direction: Vector2i)
signal wait_pressed()
signal restart_pressed()
signal hud_toggle_pressed()
signal provider_select_pressed()

@onready var hp_bar: ProgressBar = $TopBar/HBox/HPBar
@onready var hp_label: Label = $TopBar/HBox/HPLabel
@onready var stats_label: Label = $TopBar/HBox/StatsLabel
@onready var provider_label: Label = $TopBar/HBox/ProviderLabel
@onready var log_label: RichTextLabel = $LogPanel/LogLabel
@onready var death_overlay: ColorRect = $DeathOverlay
@onready var hud_btn: Button = $TouchControls/HudBtn
@onready var provider_btn: Button = $TouchControls/ProviderBtn


func setup(game_state: RefCounted) -> void:
	update_ui(game_state)

func update_ui(game_state: RefCounted) -> void:
	if not hp_bar or not hp_label or not stats_label:
		return

	hp_bar.max_value = game_state.player_max_hp
	hp_bar.value = game_state.player_hp
	hp_label.text = "HP: %d/%d" % [game_state.player_hp, game_state.player_max_hp]
	
	stats_label.text = "ATK: %d | Turn: %d | Score: %d" % [
		game_state.player_attack_power,
		game_state.player_turns,
		game_state.player_score
	]
	if game_state.world != null:
		stats_label.text += " | Rooms: %d" % game_state.world.rooms.size()
		if game_state.world.pending_count() > 0:
			stats_label.text += " | Exploring..."
		if game_state.world.counters.fallbacks > 0:
			stats_label.text += " | Fallbacks: %d" % game_state.world.counters.fallbacks
	
	if provider_label:
		var p_name: String = String(game_state.get("active_provider") if "active_provider" in game_state else "")
		var m_name: String = String(game_state.get("active_model") if "active_model" in game_state else "")
		if p_name == "":
			p_name = "rules-baseline"
		if m_name != "":
			provider_label.text = "[%s / %s]" % [p_name, m_name]
		else:
			provider_label.text = "[%s]" % p_name

	if death_overlay:
		death_overlay.visible = game_state.is_player_dead

	if log_label:
		var recent_logs: Array[String] = game_state.message_log.slice(-6)
		var text: String = ""
		for msg in recent_logs:
			if text != "":
				text += "\n"
			text += msg
		log_label.text = text

func _on_up_button_pressed() -> void:
	dpad_pressed.emit(Vector2i.UP)

func _on_down_button_pressed() -> void:
	dpad_pressed.emit(Vector2i.DOWN)

func _on_left_button_pressed() -> void:
	dpad_pressed.emit(Vector2i.LEFT)

func _on_right_button_pressed() -> void:
	dpad_pressed.emit(Vector2i.RIGHT)

func _on_wait_button_pressed() -> void:
	wait_pressed.emit()

func _on_restart_button_pressed() -> void:
	restart_pressed.emit()

func _on_hud_button_pressed() -> void:
	hud_toggle_pressed.emit()

func _on_provider_button_pressed() -> void:
	provider_select_pressed.emit()
