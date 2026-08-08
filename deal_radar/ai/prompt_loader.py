"""Загрузка версионированных промптов (раздел 8 ТЗ).

Промпты не живут строками внутри бизнес-логики: каждая версия — это каталог
``<name>/<version>/`` с ``system.md``, ``task.md``, ``schema.json`` и
``meta.json``. Смена смысла промпта означает новый каталог, а не правку
существующего, поэтому сохранённый результат всегда можно сопоставить с той
версией, которая его породила.

Модуль называется ``prompt_loader``, а не ``prompts``, чтобы не затенять собой
каталог данных ``deal_radar/ai/prompts/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROMPTS_ROOT = Path(__file__).resolve().parent / "prompts"


class PromptNotFound(RuntimeError):
    """Каталог промпта отсутствует или неполон."""


@dataclass(slots=True, frozen=True)
class PromptBundle:
    name: str
    version: str
    system: str
    task: str
    schema: dict[str, Any] = field(default_factory=dict)
    schema_version: str = ""
    schema_name: str = ""
    max_output_tokens: int = 2000

    @property
    def label(self) -> str:
        """Идентификатор вида ``listing-analysis-v1.0.0`` для логов и кэша."""

        return f"{self.name}-{self.version}"

    def build_user_message(self, payload: dict[str, Any]) -> str:
        """``task.md`` заканчивается на ``DATA:``; сюда дописывается сам объект."""

        return self.task + json.dumps(payload, ensure_ascii=False, sort_keys=True)


_CACHE: dict[Path, PromptBundle] = {}


def resolve_root(base_path: str = "") -> Path:
    return Path(base_path).resolve() if base_path else PROMPTS_ROOT


def load_prompt(name: str, version: str, base_path: str = "") -> PromptBundle:
    directory = resolve_root(base_path) / name / version
    cached = _CACHE.get(directory)
    if cached is not None:
        return cached
    if not directory.is_dir():
        raise PromptNotFound(f"Prompt directory not found: {directory}")
    try:
        system = (directory / "system.md").read_text(encoding="utf-8").strip()
        task = (directory / "task.md").read_text(encoding="utf-8")
        schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PromptNotFound(f"Prompt {name}/{version} is incomplete: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PromptNotFound(f"Prompt {name}/{version} has invalid JSON: {exc}") from exc
    if not system or not task:
        raise PromptNotFound(f"Prompt {name}/{version} has an empty system or task file")
    bundle = PromptBundle(
        name=name,
        version=version,
        system=system,
        task=task,
        schema=schema,
        schema_version=str(meta.get("schema_version", "")),
        schema_name=str(meta.get("schema_name", name.replace("-", "_"))),
        max_output_tokens=int(meta.get("max_output_tokens", 2000)),
    )
    _CACHE[directory] = bundle
    return bundle


def clear_cache() -> None:
    _CACHE.clear()
