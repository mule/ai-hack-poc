"""Offline, deterministic rules baseline provider.

The default provider and the benchmark control: no network, no credentials, no
model. Given the same semantic request it always returns the same plan, so a
recorded state replays to an identical decision.

Determinism: the RNG is seeded from a hash of the request minus ``request_id``
and ``prompt_hint`` (ids differ between replays; the hint is free text this
provider ignores). Only ``random.Random.random()`` is used, and choices are
made by index arithmetic, so results do not depend on stdlib algorithm changes.

Frontier semantics (what the game relies on):

* ``room.depth == request.state.depth``.
* The room connects back to the frontier: it has exactly one exit facing the
  opposite of ``target_exit.direction`` (``stairs`` for up/down), unlocked.
* Only that back-link may be vertical; every other exit is a distinct
  cardinal direction, so each new exit is a fresh unresolved frontier.
* ``options`` are honoured: forbidden room types, ``max_danger``,
  ``allow_secrets`` and density targets.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from dungeon_director.contracts import (
    EnvironmentalTag,
    Exit,
    ExitDirection,
    ExitKind,
    GenerationRequest,
    RoomPlan,
    RoomSize,
    RoomType,
    UsageStats,
)
from dungeon_director.providers import DungeonDirectorProvider, ProviderResult

__all__ = ["RULES_MODEL", "RULES_PROVIDER_ID", "RulesProvider"]

RULES_PROVIDER_ID = "rules-baseline"
RULES_MODEL = "builtin-v1"

_OPPOSITE = {
    ExitDirection.NORTH: ExitDirection.SOUTH,
    ExitDirection.SOUTH: ExitDirection.NORTH,
    ExitDirection.EAST: ExitDirection.WEST,
    ExitDirection.WEST: ExitDirection.EAST,
    ExitDirection.UP: ExitDirection.DOWN,
    ExitDirection.DOWN: ExitDirection.UP,
}
_CARDINALS = (
    ExitDirection.NORTH,
    ExitDirection.SOUTH,
    ExitDirection.EAST,
    ExitDirection.WEST,
)
_VERTICAL = (ExitDirection.UP, ExitDirection.DOWN)

#: Below this HP fraction the player is "hurt": danger drops, shrines are favoured.
_HURT_HP_RATIO = 0.4
#: Rooms explored on a depth before a way down may appear.
_ROOMS_BEFORE_STAIRS = 6
#: Depth from which vaults and treasure rooms may appear.
_TREASURE_MIN_DEPTH = 2

_GENERIC_TAGS = (
    EnvironmentalTag.DARK,
    EnvironmentalTag.FLOODED,
    EnvironmentalTag.FUNGAL,
    EnvironmentalTag.ICY,
    EnvironmentalTag.HOT,
    EnvironmentalTag.RUINED,
    EnvironmentalTag.OVERGROWN,
    EnvironmentalTag.NOISY,
)

_CONTRADICTIONS = {
    EnvironmentalTag.ICY: EnvironmentalTag.HOT,
    EnvironmentalTag.HOT: EnvironmentalTag.ICY,
}


@dataclass(frozen=True, slots=True)
class _Profile:
    weight: float
    sizes: tuple[RoomSize, ...]
    extra_exits: tuple[int, int]
    enemy_scale: float = 1.0
    loot_scale: float = 1.0
    secret_base: float = 0.05
    peaceful: bool = False


_PROFILES: dict[RoomType, _Profile] = {
    RoomType.ROOM: _Profile(30, (RoomSize.SMALL, RoomSize.MEDIUM), (1, 2)),
    RoomType.CORRIDOR: _Profile(20, (RoomSize.TINY, RoomSize.SMALL), (1, 1), loot_scale=0.4),
    RoomType.CAVERN: _Profile(
        10, (RoomSize.LARGE, RoomSize.HUGE), (1, 3), enemy_scale=1.3, loot_scale=0.8
    ),
    RoomType.CHAMBER: _Profile(15, (RoomSize.MEDIUM, RoomSize.LARGE), (1, 2), enemy_scale=1.1),
    RoomType.VAULT: _Profile(
        4,
        (RoomSize.SMALL, RoomSize.MEDIUM),
        (0, 0),
        enemy_scale=1.5,
        loot_scale=2.0,
        secret_base=0.3,
    ),
    RoomType.SHRINE: _Profile(5, (RoomSize.TINY, RoomSize.SMALL), (0, 1), peaceful=True),
    RoomType.SHOP: _Profile(4, (RoomSize.SMALL, RoomSize.MEDIUM), (0, 1), peaceful=True),
    RoomType.TREASURE: _Profile(
        5,
        (RoomSize.SMALL, RoomSize.MEDIUM),
        (0, 1),
        enemy_scale=0.8,
        loot_scale=2.0,
        secret_base=0.25,
    ),
    RoomType.STAIRS_DOWN: _Profile(
        6, (RoomSize.SMALL, RoomSize.MEDIUM), (0, 1), enemy_scale=0.5, loot_scale=0.5
    ),
}


T = TypeVar("T")


def _pick(rng: random.Random, items: Sequence[T]) -> T:
    return items[int(rng.random() * len(items))]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class RulesProvider(DungeonDirectorProvider):
    provider_id = RULES_PROVIDER_ID
    models = (RULES_MODEL,)
    default_model = RULES_MODEL

    async def generate(self, request: GenerationRequest, *, model: str) -> ProviderResult:
        return ProviderResult(
            payload=_plan_room(request),
            usage=UsageStats(input_tokens=0, output_tokens=0, estimated_cost_usd=0.0),
        )


def _plan_room(request: GenerationRequest) -> RoomPlan:
    digest = hashlib.sha256(
        request.model_dump_json(exclude={"request_id", "prompt_hint"}).encode("utf-8")
    )
    rng = random.Random(int.from_bytes(digest.digest()[:8], "big"))
    state = request.state
    options = request.options
    allow_secrets = options.allow_secrets if options else True

    hp_ratio = state.player.hp / state.player.max_hp
    hurt = hp_ratio <= _HURT_HP_RATIO
    rooms_on_depth = state.pacing.rooms_on_depth if state.pacing else 0

    room_type = _choose_room_type(rng, request, hurt=hurt, rooms_on_depth=rooms_on_depth)
    profile = _PROFILES[room_type]
    danger = _choose_danger(request, hurt=hurt, peaceful=profile.peaceful)

    secret_probability = 0.0
    has_secret = False
    if allow_secrets:
        secret_probability = round(profile.secret_base + rng.random() * 0.1, 2)
        has_secret = rng.random() < secret_probability

    exits = _choose_exits(
        rng,
        request,
        room_type,
        profile,
        secret_exit=has_secret,
    )
    tags = _choose_tags(rng, room_type, danger)
    size = _pick(rng, profile.sizes)

    enemy_target = options.target_enemy_density if options else None
    loot_target = options.target_loot_density if options else None
    enemy_density = _density(rng, enemy_target, 0.05 + 0.08 * (danger - 1), profile.enemy_scale)
    loot_density = _density(rng, loot_target, 0.2, profile.loot_scale)
    if profile.peaceful:
        enemy_density = 0.0

    return RoomPlan(
        room_id=f"rules-{digest.hexdigest()[:10]}",
        depth=state.depth,
        room_type=room_type,
        size=size,
        danger=danger,
        exits=exits,
        enemy_density=enemy_density,
        loot_density=loot_density,
        secret_probability=secret_probability,
        has_secret=has_secret,
        environmental_tags=tags,
        description=_describe(size, room_type, tags),
    )


def _choose_room_type(
    rng: random.Random, request: GenerationRequest, *, hurt: bool, rooms_on_depth: int
) -> RoomType:
    forbidden = set(request.options.forbidden_room_types) if request.options else set()
    eligible = [t for t in _PROFILES if t not in forbidden]

    def allowed_now(room_type: RoomType) -> bool:
        if room_type is RoomType.STAIRS_DOWN:
            return rooms_on_depth >= _ROOMS_BEFORE_STAIRS
        if room_type in (RoomType.VAULT, RoomType.TREASURE):
            return request.state.depth >= _TREASURE_MIN_DEPTH
        return True

    # The pacing gates are preferences: if the caller's forbidden list leaves
    # only gated types, fall back to those rather than fail the frontier.
    pool = [t for t in eligible if allowed_now(t)] or eligible
    weights = [_PROFILES[t].weight * (4.0 if hurt and t is RoomType.SHRINE else 1.0) for t in pool]
    roll = rng.random() * sum(weights)
    for room_type, weight in zip(pool, weights, strict=True):
        roll -= weight
        if roll < 0:
            return room_type
    return pool[-1]


def _choose_danger(request: GenerationRequest, *, hurt: bool, peaceful: bool) -> int:
    state = request.state
    danger = 1 + (state.depth - 1) // 3
    average = state.pacing.average_recent_danger if state.pacing else None
    if hurt:
        danger -= 1
    elif average is not None and average >= 3.5:
        danger -= 1  # give the player a breather after a run of hard rooms
    elif average is not None and average <= 1.5 and state.player.hp == state.player.max_hp:
        danger += 1
    if peaceful:
        danger = 1
    if request.options and request.options.max_danger is not None:
        danger = min(danger, request.options.max_danger)
    return int(_clamp(danger, 1, 5))


def _choose_exits(
    rng: random.Random,
    request: GenerationRequest,
    room_type: RoomType,
    profile: _Profile,
    *,
    secret_exit: bool,
) -> list[Exit]:
    frontier = request.target_exit.direction
    back_kind = ExitKind.STAIRS if frontier in _VERTICAL else ExitKind.DOOR
    exits = [Exit(direction=_OPPOSITE[frontier], kind=back_kind, locked=False)]

    free = [d for d in _CARDINALS if d != exits[0].direction]
    low, high = profile.extra_exits
    count = min(low + int(rng.random() * (high - low + 1)), len(free))
    extras: list[ExitDirection] = []
    if room_type is RoomType.CORRIDOR and frontier in free and count:
        extras.append(frontier)  # a corridor carries straight on
        free.remove(frontier)
    while len(extras) < count:
        choice = _pick(rng, free)
        free.remove(choice)
        extras.append(choice)

    for index, direction in enumerate(extras):
        if room_type is RoomType.CORRIDOR:
            kind = ExitKind.PASSAGE
        else:
            kind = ExitKind.DOOR if rng.random() < 0.6 else ExitKind.PASSAGE
        if secret_exit and index == len(extras) - 1:
            kind = ExitKind.SECRET
        exits.append(Exit(direction=direction, kind=kind, locked=False))
    return exits


def _choose_tags(rng: random.Random, room_type: RoomType, danger: int) -> list[EnvironmentalTag]:
    if room_type is RoomType.SHRINE:
        return [EnvironmentalTag.HALLOWED]
    pool = list(_GENERIC_TAGS)
    if danger >= 2:
        pool.append(EnvironmentalTag.TRAPPED)
    roll = rng.random()
    count = 0 if roll < 0.45 else 1 if roll < 0.85 else 2
    tags: list[EnvironmentalTag] = []
    for _ in range(count):
        tag = _pick(rng, pool)
        pool.remove(tag)
        if tag in _CONTRADICTIONS and _CONTRADICTIONS[tag] in pool:
            pool.remove(_CONTRADICTIONS[tag])
        tags.append(tag)
    return tags


def _density(rng: random.Random, target: float | None, base: float, scale: float) -> float:
    jitter = (rng.random() - 0.5) * 0.06  # always drawn so the RNG stream is stable
    if target is not None:
        return round(target, 2)
    return round(_clamp(base * scale + jitter, 0.0, 1.0), 2)


def _describe(size: RoomSize, room_type: RoomType, tags: list[EnvironmentalTag]) -> str:
    text = f"{size.value.capitalize()} {room_type.value.replace('_', ' ')}"
    if tags:
        text += ", " + " and ".join(tag.value for tag in tags)
    return text + "."
