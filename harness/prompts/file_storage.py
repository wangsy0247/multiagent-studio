"""File-system prompt storage implementation."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.prompts.storage import PromptStorage, PromptTemplate

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, content: str) -> None:
    """Write text atomically using a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


class FilePromptStorage(PromptStorage):
    """Store prompt templates as files under ``prompts_root``.

    Layout::

        prompts_root/
            versions/{agent_type}/{name}/vX.Y.Z_{role}.md
            versions/{agent_type}/{name}/vX.Y.Z_{role}.json
            active.json
            registry.json
    """

    def __init__(self, prompts_root: str = "./prompts"):
        self.root = Path(prompts_root)
        self.versions_dir = self.root / "versions"
        self.active_path = self.root / "active.json"
        self.registry_path = self.root / "registry.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def _parse_id(self, template_id: str) -> tuple[str, str]:
        parts = template_id.split(":", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return "default", template_id

    def _version_dir(self, agent_type: str, name: str) -> Path:
        return self.versions_dir / agent_type / name

    def _paths_for(
        self,
        agent_type: str,
        name: str,
        version: str,
        role: str,
    ) -> tuple[Path, Path]:
        d = self._version_dir(agent_type, name)
        base = d / f"{version}_{role}"
        return Path(f"{base}.md"), Path(f"{base}.json")

    async def save(self, template: PromptTemplate) -> None:
        agent_type, name = self._parse_id(template.id)
        md_path, json_path = self._paths_for(
            agent_type, name, template.version, template.role
        )

        await asyncio.to_thread(_atomic_write, md_path, template.content)
        await asyncio.to_thread(
            _atomic_write,
            json_path,
            template.model_dump_json(indent=2),
        )

        await self._upsert_registry(template)

        active = await self._load_active()
        if template.id not in active:
            active[template.id] = template.version
            await self._save_active(active)

    async def get(
        self,
        template_id: str,
        version: str | None = None,
    ) -> PromptTemplate | None:
        agent_type, name = self._parse_id(template_id)
        if version is None:
            version = await self.get_active_version(template_id)
            if version is None:
                return None

        d = self._version_dir(agent_type, name)
        md_path = None
        json_path = None
        if d.exists():
            for f in d.iterdir():
                if f.name.startswith(version) and f.suffix == ".md":
                    md_path = f
                    json_path = f.with_suffix(".json")
                    break

        if md_path is None or not md_path.exists():
            return None

        content = await asyncio.to_thread(md_path.read_text, encoding="utf-8")
        if json_path and json_path.exists():
            meta = json.loads(
                await asyncio.to_thread(json_path.read_text, encoding="utf-8")
            )
            meta["content"] = content
            return PromptTemplate(**meta)

        return PromptTemplate(
            id=template_id,
            name=name,
            version=version,
            agent_type=agent_type,
            role="system",
            content=content,
        )

    async def list(
        self,
        agent_type: str | None = None,
        role: str | None = None,
        tags: list[str] | None = None,
    ) -> list[PromptTemplate]:
        registry = await self._load_registry()
        results: list[PromptTemplate] = []
        for meta in registry.values():
            template = PromptTemplate(**meta)
            if agent_type and template.agent_type != agent_type:
                continue
            if role and template.role != role:
                continue
            if tags and not any(t in template.tags for t in tags):
                continue
            results.append(template)
        return results

    async def activate(self, template_id: str, version: str) -> None:
        active = await self._load_active()
        active[template_id] = version
        await self._save_active(active)

    async def update_state(
        self,
        template_id: str,
        version: str,
        new_state: str,
    ) -> None:
        template = await self.get(template_id, version)
        if template is None:
            raise ValueError(f"Template {template_id}@{version} not found")
        template.state = new_state
        template.updated_at = datetime.now()
        await self.save(template)

    async def get_active_version(self, template_id: str) -> str | None:
        active = await self._load_active()
        return active.get(template_id)

    async def _load_active(self) -> dict[str, str]:
        if not self.active_path.exists():
            return {}
        return json.loads(
            await asyncio.to_thread(self.active_path.read_text, encoding="utf-8")
        )

    async def _save_active(self, active: dict[str, str]) -> None:
        await asyncio.to_thread(
            _atomic_write,
            self.active_path,
            json.dumps(active, ensure_ascii=False, indent=2),
        )

    async def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {}
        return json.loads(
            await asyncio.to_thread(self.registry_path.read_text, encoding="utf-8")
        )

    async def _upsert_registry(self, template: PromptTemplate) -> None:
        registry = await self._load_registry()
        registry[template.id] = json.loads(template.model_dump_json())
        await asyncio.to_thread(
            _atomic_write,
            self.registry_path,
            json.dumps(registry, ensure_ascii=False, indent=2),
        )

    async def bootstrap_from_templates(
        self,
        templates_dir: str | None = None,
    ) -> list[PromptTemplate]:
        """Import templates from a source templates directory into storage.

        Expected layout::

            templates/{agent_type}/{name}/vX.Y.Z_{role}.md
            templates/{agent_type}/vX.Y.Z_{role}.md
        """
        if templates_dir is None:
            templates_dir = str(Path(__file__).parent / "templates")
        base = Path(templates_dir)
        if not base.exists():
            return []

        loaded: list[PromptTemplate] = []
        active = await self._load_active()
        registry = await self._load_registry()

        for agent_type_dir in base.iterdir():
            if not agent_type_dir.is_dir():
                continue
            agent_type = agent_type_dir.name
            for item in agent_type_dir.iterdir():
                if item.is_file() and item.suffix == ".md":
                    loaded.extend(
                        await self._import_template_file(
                            item, agent_type, None, active, registry
                        )
                    )
                elif item.is_dir():
                    name = item.name
                    for md_file in item.iterdir():
                        if md_file.is_file() and md_file.suffix == ".md":
                            loaded.extend(
                                await self._import_template_file(
                                    md_file, agent_type, name, active, registry
                                )
                            )

        await self._save_registry_dict(registry)
        await self._save_active(active)
        return loaded

    async def _import_template_file(
        self,
        path: Path,
        agent_type: str,
        name: str | None,
        active: dict[str, str],
        registry: dict[str, Any],
    ) -> list[PromptTemplate]:
        match = re.match(r"^(v\d+\.\d+\.\d+)_(.+)\.md$", path.name)
        if not match:
            return []
        version, role = match.groups()
        template_id = f"{agent_type}:{name or role}"
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")

        agent_type_parsed, name_parsed = self._parse_id(template_id)
        md_path, json_path = self._paths_for(
            agent_type_parsed, name_parsed, version, role
        )
        if md_path.exists():
            return []

        template = PromptTemplate(
            id=template_id,
            name=name or f"{agent_type} {role}",
            version=version,
            agent_type=agent_type,
            role=role,
            content=content,
            description=f"Bootstrapped from {path}",
        )

        await asyncio.to_thread(_atomic_write, md_path, content)
        await asyncio.to_thread(
            _atomic_write, json_path, template.model_dump_json(indent=2)
        )

        registry[template_id] = json.loads(template.model_dump_json())
        versions = set(registry[template_id].get("versions", []))
        versions.add(version)
        registry[template_id]["versions"] = sorted(versions)
        if template_id not in active:
            active[template_id] = version

        return [template]

    async def _save_registry_dict(self, registry: dict[str, Any]) -> None:
        await asyncio.to_thread(
            _atomic_write,
            self.registry_path,
            json.dumps(registry, ensure_ascii=False, indent=2),
        )
