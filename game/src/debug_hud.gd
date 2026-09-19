class_name DebugHUD
extends Control
## Toggleable Desktop and Android Debug HUD (Issue #12).
## Displays:
##   - Current provider and model
##   - Generation status ("idle", "generating", "committed", "fallback", "failed")
##   - Latency (ms) of the last generation
##   - Recent RoomPlan decisions (ring buffer / history)
##   - Generic provider metadata (including Jev probabilities, scores, confidences)
##
## Usable on Desktop (toggle via backquote ` / F3 or touch button) and Android (touch button).

signal hud_toggled(is_visible: bool)

@onready var panel: PanelContainer = $Panel if has_node("Panel") else null
@onready var content_label: RichTextLabel = $Panel/VBox/Scroll/ContentLabel if has_node("Panel/VBox/Scroll/ContentLabel") else null
@onready var close_btn: Button = $Panel/VBox/Header/CloseBtn if has_node("Panel/VBox/Header/CloseBtn") else null

var is_hud_visible := false


func _ready() -> void:
	if panel == null and has_node("Panel"):
		panel = get_node("Panel") as PanelContainer
	if content_label == null and has_node("Panel/VBox/Scroll/ContentLabel"):
		content_label = get_node("Panel/VBox/Scroll/ContentLabel") as RichTextLabel
	if close_btn == null and has_node("Panel/VBox/Header/CloseBtn"):
		close_btn = get_node("Panel/VBox/Header/CloseBtn") as Button

	if panel:
		panel.visible = is_hud_visible
	if close_btn:
		close_btn.focus_mode = Control.FOCUS_NONE
		if not close_btn.pressed.is_connected(toggle_hud):
			close_btn.pressed.connect(toggle_hud)



func toggle_hud() -> void:
	set_hud_visible(not is_hud_visible)


func set_hud_visible(p_visible: bool) -> void:
	is_hud_visible = p_visible
	if panel:
		panel.visible = is_hud_visible
	hud_toggled.emit(is_hud_visible)


func update_hud(coordinator: RefCounted, active_provider: String, active_model: String) -> void:
	if content_label == null and has_node("Panel/VBox/Scroll/ContentLabel"):
		content_label = get_node("Panel/VBox/Scroll/ContentLabel") as RichTextLabel
	if not content_label or not is_hud_visible:
		return

	var text := "[b][color=gold]DEBUG HUD & TELEMETRY[/color][/b]\n"
	text += "----------------------------------------\n"

	# 1. Current Provider & Model
	text += "[b]Configured Provider:[/b] %s\n" % (active_provider if active_provider != "" else "default (rules-baseline)")
	text += "[b]Configured Model:[/b] %s\n" % (active_model if active_model != "" else "default")

	if coordinator == null:
		text += "\n[color=gray]No active generation coordinator.[/color]\n"
		content_label.text = text
		return

	var status: String = coordinator.last_generation_status
	var status_color := "white"
	match status:
		"generating":
			status_color = "yellow"
		"committed":
			status_color = "green"
		"fallback":
			status_color = "orange"
		"failed":
			status_color = "red"
		_:
			status_color = "gray"

	text += "[b]Gen Status:[/b] [color=%s]%s[/color] (in-flight: %d)\n" % [status_color, status.to_upper(), coordinator.in_flight.size()]

	# Latency
	if coordinator.last_latency_ms >= 0.0:
		text += "[b]Last Latency:[/b] %.1f ms\n" % coordinator.last_latency_ms
	else:
		text += "[b]Last Latency:[/b] n/a\n"

	if coordinator.last_provider != "" or coordinator.last_model != "":
		text += "[b]Last Answered By:[/b] %s / %s\n" % [coordinator.last_provider, coordinator.last_model]

	# 2. Generic Provider Metadata (including Jev probabilities)
	text += "\n[b][color=aqua]PROVIDER METADATA[/color][/b]\n"
	var meta: Dictionary = coordinator.last_provider_metadata
	if meta.is_empty():
		text += "[color=gray]No metadata on last generation response.[/color]\n"
	else:
		# Format generic metadata with special care for Jev keys
		for key in meta:
			var val = meta[key]
			if key.ends_with("_probabilities") and val is Dictionary:
				text += "  [b]%s:[/b]\n" % key
				for opt in val:
					text += "    • %s: %.4f\n" % [str(opt), float(val[opt])]
			elif key == "tag_probabilities" and val is Dictionary:
				text += "  [b]tag_probabilities:[/b]\n"
				for tag in val:
					text += "    • %s: %.3f\n" % [str(tag), float(val[tag])]
			elif val is Dictionary or val is Array:
				text += "  [b]%s:[/b] %s\n" % [key, JSON.stringify(val)]
			else:
				text += "  [b]%s:[/b] %s\n" % [key, str(val)]

	# 3. Recent RoomPlan Decisions
	text += "\n[b][color=aqua]RECENT ROOMPLAN DECISIONS (%d)[/color][/b]\n" % coordinator.recent_decisions.size()
	if coordinator.recent_decisions.is_empty():
		text += "[color=gray]No decisions recorded yet.[/color]\n"
	else:
		var slice = coordinator.recent_decisions.duplicate()
		slice.reverse()
		for d in slice:
			var src_color := "green" if d.source == "director" else ("orange" if d.source == "fallback" else "gray")
			text += "• [b]%s[/b] ([color=%s]%s[/color] by %s) - type=%s, size=%s, danger=%d, exits=%d\n" % [
				d.room_id,
				src_color,
				d.source,
				d.provider,
				d.room_type,
				d.size,
				d.danger,
				d.exits.size()
			]
			if d.source == "fallback" and d.get("provider_metadata", {}).has("fallback_reason"):
				text += "  [color=orange]reason:[/color] %s\n" % str(d.provider_metadata.fallback_reason)

	content_label.text = text
