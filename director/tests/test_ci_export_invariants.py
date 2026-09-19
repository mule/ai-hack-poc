"""Validation and assertion tests for CI, export presets, and build configurations."""

from __future__ import annotations

import configparser
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_export_presets_structure_and_safety():
    presets_path = REPO_ROOT / "game" / "export_presets.cfg"
    assert presets_path.exists(), "game/export_presets.cfg must exist"

    config = configparser.ConfigParser()
    config.read(presets_path)

    sections = set(config.sections())
    assert "preset.0" in sections, "preset.0 must be defined"
    assert "preset.0.options" in sections, "preset.0.options must be defined"
    assert "preset.1" in sections, "preset.1 must be defined"
    assert "preset.1.options" in sections, "preset.1.options must be defined"

    # Named invariant assertions
    assert config.get("preset.0", "name") == '"Linux Desktop"', (
        "Preset 0 must be named 'Linux Desktop'"
    )
    assert config.get("preset.1", "name") == '"Android Debug"', (
        "Preset 1 must be named 'Android Debug'"
    )
    assert config.get("preset.0", "platform") == '"Linux"', "Preset 0 platform must be Linux"
    assert config.get("preset.1", "platform") == '"Android"', "Preset 1 platform must be Android"
    assert config.get("preset.0", "export_path") == ('"builds/linux/ai-hack-poc.x86_64"')
    assert config.get("preset.1", "export_path") == ('"builds/android/ai-hack-poc-debug.apk"')

    # Exclude test files invariant
    assert config.get("preset.0", "exclude_filter") == '"tests/*"', (
        "Preset 0 must exclude tests via exclude_filter"
    )
    assert config.get("preset.1", "exclude_filter") == '"tests/*"', (
        "Preset 1 must exclude tests via exclude_filter"
    )

    # Ensure no local machine paths or credentials leaked
    for section in config.sections():
        for key, value in config.items(section):
            assert "/home" not in value, f"Local path leak in {section}.{key}: {value}"
            if "keystore" in key:
                assert value == '""', f"Keystore credential leak in {section}.{key}: {value}"


def test_github_ci_workflow_invariants():
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.exists(), ".github/workflows/ci.yml must exist"

    workflow = yaml.safe_load(ci_path.read_text())

    # Least-privilege permissions invariant
    assert workflow.get("permissions") == {"contents": "read"}, (
        "CI workflow must enforce least-privilege permissions: contents: read"
    )

    # Concurrency invariants
    concurrency = workflow.get("concurrency", {})
    assert concurrency.get("cancel-in-progress") == "${{ github.ref != 'refs/heads/main' }}", (
        "Workflow concurrency must cancel in-progress runs on non-main branches"
    )

    # Required job invariants & timeouts
    jobs = workflow.get("jobs", {})
    assert "backend-checks" in jobs, "CI must contain backend-checks job"
    assert "godot-checks" in jobs, "CI must contain godot-checks job"
    assert "build-artifacts" in jobs, "CI must contain build-artifacts job"

    assert jobs["backend-checks"].get("timeout-minutes") == 15
    assert jobs["godot-checks"].get("timeout-minutes") == 15
    assert jobs["build-artifacts"].get("timeout-minutes") == 30
    assert "if" not in jobs["build-artifacts"], "PRs must exercise both export presets"

    # Verification of SHA-512 check in build-artifacts job steps
    steps = jobs["build-artifacts"].get("steps", [])
    step_names = [s.get("name") for s in steps]
    assert "Install Godot Engine" in step_names
    assert "Install Godot export templates" in step_names

    install_godot = next(s for s in steps if s.get("name") == "Install Godot Engine")
    godot_install_script = install_godot.get("run", "")
    assert "curl -fsSL --retry 3" in godot_install_script
    assert "sha512sum -c" in godot_install_script, (
        "Godot engine download must be verified with sha512sum"
    )

    install_templates = next(s for s in steps if s.get("name") == "Install Godot export templates")
    template_install_script = install_templates.get("run", "")
    assert "curl -fsSL --retry 3" in template_install_script
    assert "sha512sum -c" in template_install_script, (
        "Godot export templates download must be verified with sha512sum"
    )

    export_linux = next(s for s in steps if s.get("name") == "Export Linux desktop package")
    export_android = next(s for s in steps if s.get("name") == "Export Android debug APK")
    assert 'make export-linux GODOT="$HOME/.local/bin/godot"' in export_linux.get("run", "")
    assert 'make export-android GODOT="$HOME/.local/bin/godot"' in export_android.get("run", "")

    # Artifact upload condition and packaging invariants
    upload_linux = next(s for s in steps if s.get("name") == "Upload Linux desktop artifact")
    assert (
        upload_linux.get("if")
        == "github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'"
    ), "Linux artifact upload must be restricted to main or workflow_dispatch"
    assert upload_linux.get("with", {}).get("path") == (
        "game/builds/linux/ai-hack-poc-linux-x86_64.tar.gz"
    ), "Linux artifact must upload the .tar.gz package"

    upload_android = next(s for s in steps if s.get("name") == "Upload Android debug artifact")
    assert (
        upload_android.get("if")
        == "github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'"
    ), "Android artifact upload must be restricted to main or workflow_dispatch"
    assert upload_android.get("with", {}).get("path") == (
        "game/builds/android/ai-hack-poc-debug.apk"
    ), "Android artifact must upload the debug apk"

    makefile = (REPO_ROOT / "Makefile").read_text()
    assert (
        "tar -czf game/builds/linux/ai-hack-poc-linux-x86_64.tar.gz "
        "-C game/builds/linux ai-hack-poc.x86_64 ai-hack-poc.pck ai-hack-poc.sh"
    ) in makefile
    assert '--export-debug "Linux Desktop" builds/linux/ai-hack-poc.x86_64' in makefile
    assert '--export-debug "Android Debug" builds/android/ai-hack-poc-debug.apk' in makefile
    assert 'find game -name "*.uid"' not in makefile


def test_project_godot_texture_compression_invariant():
    project_path = REPO_ROOT / "game" / "project.godot"
    assert project_path.exists(), "game/project.godot must exist"

    content = project_path.read_text()
    assert "textures/vram_compression/import_etc2_astc=true" in content, (
        "game/project.godot must enable ETC2/ASTC texture compression for Android export"
    )
    assert 'config/icon="res://icon.svg"' in content
    assert (REPO_ROOT / "game" / "icon.svg").exists()
    assert (REPO_ROOT / "game" / "icon.svg.import").exists()
