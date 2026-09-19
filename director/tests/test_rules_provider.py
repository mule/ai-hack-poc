"""The offline rules baseline: valid, deterministic, frontier-aware plans."""

from __future__ import annotations

import asyncio

import pytest
from fakes import make_request, request_for_exit, request_payload

from dungeon_director.contracts import (
    EnvironmentalTag,
    ExitDirection,
    ExitKind,
    GenerationRequest,
    RoomPlan,
    RoomType,
)
from dungeon_director.rules import RulesProvider

OPPOSITE = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
    "up": "down",
    "down": "up",
}


def plan_for(request: GenerationRequest) -> RoomPlan:
    result = asyncio.run(RulesProvider().generate(request, model="builtin-v1"))
    assert isinstance(result.payload, RoomPlan), "rules provider must return a validated RoomPlan"
    return result.payload


def test_identity_is_stable():
    provider = RulesProvider()

    assert provider.provider_id == "rules-baseline"
    assert provider.default_model == "builtin-v1"
    assert provider.models == ("builtin-v1",)
    assert provider.availability.available is True


def test_returns_valid_room_plan_for_fixture_request():
    plan = plan_for(make_request())

    assert RoomPlan.model_validate(plan.model_dump(mode="json")) == plan


def test_same_request_gives_identical_plan():
    assert plan_for(make_request()) == plan_for(make_request())


def test_plan_ignores_request_id_so_replays_compare_equal():
    first = plan_for(make_request(request_id="req-a"))
    replay = plan_for(make_request(request_id="req-b"))

    assert first == replay


def test_different_frontiers_give_different_rooms():
    north = plan_for(request_for_exit("north"))
    east = plan_for(request_for_exit("east"))

    assert north.room_id != east.room_id


def test_room_depth_matches_request_depth():
    for depth in (1, 3, 17, 128):
        assert plan_for(request_for_exit("north", depth=depth)).depth == depth


@pytest.mark.parametrize("direction", list(OPPOSITE))
def test_room_links_back_through_the_opposite_exit(direction):
    plan = plan_for(request_for_exit(direction))

    back = [e for e in plan.exits if e.direction.value == OPPOSITE[direction]]
    assert len(back) == 1, f"expected exactly one back-link exit, got {plan.exits}"
    assert back[0].locked is False
    if direction in ("up", "down"):
        assert back[0].kind is ExitKind.STAIRS, "vertical frontiers must link back by stairs"


@pytest.mark.parametrize("direction", ["north", "south", "east", "west", "up", "down"])
def test_exit_directions_are_unique_and_only_back_link_may_be_vertical(direction):
    plan = plan_for(request_for_exit(direction))

    directions = [e.direction for e in plan.exits]
    assert len(directions) == len(set(directions))
    vertical = {ExitDirection.UP, ExitDirection.DOWN}
    assert {d for d in directions if d in vertical} <= {ExitDirection(OPPOSITE[direction])}


def test_never_generates_entrance_or_upward_stairs_for_a_frontier():
    for turn in range(40):
        plan = plan_for(request_for_exit("north", turn=turn))
        assert plan.room_type not in (RoomType.ENTRANCE, RoomType.STAIRS_UP)


def test_forbidden_room_types_are_never_chosen():
    forbidden = [t.value for t in RoomType if t not in (RoomType.CAVERN, RoomType.STAIRS_UP)]
    forbidden = forbidden[:8]
    options = {"forbidden_room_types": forbidden}

    for turn in range(30):
        request = make_request(state={**request_payload()["state"], "turn": turn}, options=options)
        assert plan_for(request).room_type.value not in forbidden


def test_forbidding_all_but_one_candidate_still_yields_that_type():
    everything_else = [
        "entrance",
        "room",
        "corridor",
        "chamber",
        "vault",
        "shrine",
        "shop",
        "treasure",
    ]
    plan = plan_for(make_request(options={"forbidden_room_types": everything_else}))

    assert plan.room_type in (RoomType.CAVERN, RoomType.STAIRS_DOWN)


@pytest.mark.parametrize("max_danger", [1, 2, 3])
def test_danger_never_exceeds_max_danger_option(max_danger):
    for depth in (1, 6, 12, 40, 128):
        request = request_for_exit("north", depth=depth, options={"max_danger": max_danger})
        assert plan_for(request).danger <= max_danger


def test_danger_grows_with_depth_when_uncapped():
    shallow = plan_for(request_for_exit("north", depth=1)).danger
    deep = plan_for(request_for_exit("north", depth=30)).danger

    assert deep > shallow


def test_hurt_player_gets_a_gentler_room_than_a_healthy_one():
    healthy = request_for_exit(
        "north", depth=12, player={"hp": 30, "max_hp": 30, "level": 4}, pacing=None
    )
    hurt = request_for_exit(
        "north", depth=12, player={"hp": 4, "max_hp": 30, "level": 4}, pacing=None
    )

    assert plan_for(hurt).danger < plan_for(healthy).danger


def test_secrets_are_suppressed_when_not_allowed():
    for turn in range(40):
        request = make_request(
            state={**request_payload()["state"], "turn": turn},
            options={"allow_secrets": False},
        )
        plan = plan_for(request)
        assert plan.secret_probability == 0.0
        assert plan.has_secret is not True
        assert all(e.kind is not ExitKind.SECRET for e in plan.exits)


def test_density_targets_are_honoured_for_hostile_rooms():
    for turn in range(40):
        request = make_request(
            state={**request_payload()["state"], "turn": turn},
            options={"target_enemy_density": 0.6, "target_loot_density": 0.1},
        )
        plan = plan_for(request)
        if plan.room_type in (RoomType.SHRINE, RoomType.SHOP):
            assert plan.enemy_density == 0.0
        else:
            assert plan.enemy_density == 0.6
            assert plan.loot_density == 0.1


def test_downward_stairs_wait_until_the_level_has_been_explored():
    for turn in range(60):
        early = request_for_exit("north", turn=turn, pacing={"rooms_on_depth": 1})
        assert plan_for(early).room_type is not RoomType.STAIRS_DOWN


def test_output_is_valid_across_many_varied_requests():
    directions = ["north", "south", "east", "west", "up", "down"]
    for i in range(120):
        request = request_for_exit(directions[i % 6], depth=1 + i % 128, turn=i)
        plan = plan_for(request)
        assert plan.depth == request.state.depth
        RoomPlan.model_validate(plan.model_dump(mode="json"))


def test_environmental_tags_are_unique_and_never_contradict_each_other():
    for i in range(300):
        plan = plan_for(request_for_exit("north", turn=i))
        tags = set(plan.environmental_tags)
        assert len(tags) == len(plan.environmental_tags)
        assert not {EnvironmentalTag.ICY, EnvironmentalTag.HOT} <= tags, plan.description
