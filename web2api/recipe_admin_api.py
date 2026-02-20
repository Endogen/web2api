"""Recipe management API routes."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from web2api.recipe_manager import (
    build_entry_payload,
    disable_recipe,
    discover_recipe_entries,
    enable_recipe,
    find_recipe_entry,
    get_manifest_record,
    install_recipe_from_source,
    load_manifest,
    remove_manifest_record,
    resolve_catalog_recipes,
    resolve_managed_recipe_source,
    resolve_recipe_folder,
)
from web2api.registry import RecipeRegistry


def _reload_registry(app: FastAPI, *, app_version: str) -> None:
    registry = RecipeRegistry(
        app_version=app_version,
        enforce_plugin_compatibility=app.state.enforce_plugin_compatibility,
    )
    registry.discover(app.state.recipes_dir)
    app.state.registry = registry


def register_recipe_admin_routes(app: FastAPI, *, app_version: str) -> None:
    """Register recipe-management API endpoints on the application."""

    @app.get("/api/recipes/manage")
    async def recipes_manage(request: Request) -> JSONResponse:
        """Return installable catalog recipes and currently installed entries."""
        recipes_dir: Path = request.app.state.recipes_dir
        catalog_source: str = request.app.state.catalog_source
        catalog_ref: str | None = request.app.state.catalog_ref
        catalog_path: str | None = request.app.state.catalog_path

        installed_entries = discover_recipe_entries(recipes_dir)
        installed_payload = [
            build_entry_payload(entry, app_version=app_version) for entry in installed_entries
        ]
        installed_by_slug = {str(item["slug"]): item for item in installed_payload}

        try:
            catalog = resolve_catalog_recipes(
                catalog_source=catalog_source,
                catalog_ref=catalog_ref,
                catalog_path=catalog_path,
            )
        except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "catalog_source": {
                        "source": catalog_source,
                        "ref": catalog_ref,
                        "path": catalog_path,
                    },
                    "catalog_error": str(exc),
                    "catalog": [],
                    "installed": installed_payload,
                },
            )

        catalog_payload: list[dict[str, Any]] = []
        for name, spec in sorted(catalog.items()):
            installed = installed_by_slug.get(spec.slug)
            catalog_payload.append(
                {
                    "name": name,
                    "slug": spec.slug,
                    "description": spec.description,
                    "trusted": bool(spec.trusted),
                    "source": spec.source,
                    "source_ref": spec.source_ref,
                    "source_subdir": spec.source_subdir,
                    "docs_url": spec.docs_url,
                    "requires_env": spec.requires_env,
                    "installed": installed is not None and bool(installed.get("has_recipe")),
                    "enabled": installed.get("enabled") if installed is not None else None,
                    "managed": installed.get("managed") if installed is not None else False,
                    "plugin": installed.get("plugin") if installed is not None else None,
                    "origin": (
                        str(installed.get("origin", "unmanaged"))
                        if installed is not None and bool(installed.get("has_recipe"))
                        else "catalog"
                    ),
                }
            )

        return JSONResponse(
            content={
                "catalog_source": {
                    "source": catalog_source,
                    "ref": catalog_ref,
                    "path": catalog_path,
                },
                "catalog_error": None,
                "catalog": catalog_payload,
                "installed": installed_payload,
            }
        )

    @app.post("/api/recipes/manage/install/{name}")
    async def recipes_manage_install(name: str, request: Request) -> JSONResponse:
        """Install a recipe from the configured recipe catalog."""
        lock: asyncio.Lock = request.app.state.recipe_admin_lock
        async with lock:
            try:
                catalog = resolve_catalog_recipes(
                    catalog_source=request.app.state.catalog_source,
                    catalog_ref=request.app.state.catalog_ref,
                    catalog_path=request.app.state.catalog_path,
                )
            except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            spec = catalog.get(name)
            if spec is None:
                raise HTTPException(status_code=404, detail=f"catalog entry '{name}' not found")

            try:
                slug, source_type = install_recipe_from_source(
                    source=spec.source,
                    recipes_dir=request.app.state.recipes_dir,
                    source_ref=spec.source_ref,
                    source_subdir=spec.source_subdir,
                    trusted=bool(spec.trusted),
                    overwrite=False,
                    record_source_type="catalog",
                )
            except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            _reload_registry(request.app, app_version=app_version)

        return JSONResponse(
            content={
                "ok": True,
                "action": "install",
                "name": name,
                "slug": slug,
                "source_type": source_type,
            }
        )

    @app.post("/api/recipes/manage/update/{slug}")
    async def recipes_manage_update(slug: str, request: Request) -> JSONResponse:
        """Update a managed recipe from its recorded source."""
        lock: asyncio.Lock = request.app.state.recipe_admin_lock
        async with lock:
            recipes_dir: Path = request.app.state.recipes_dir
            entries = discover_recipe_entries(recipes_dir)
            entry = find_recipe_entry(entries, slug)
            manifest = load_manifest(recipes_dir)
            manifest_record = get_manifest_record(manifest, slug)
            if manifest_record is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"recipe '{slug}' is not tracked in manifest",
                )

            try:
                managed_source = resolve_managed_recipe_source(manifest_record, slug=slug)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            was_disabled = entry is not None and not entry.enabled
            try:
                updated_slug, source_type = install_recipe_from_source(
                    source=managed_source.source,
                    recipes_dir=recipes_dir,
                    source_ref=managed_source.source_ref,
                    source_subdir=managed_source.source_subdir,
                    trusted=managed_source.trusted,
                    overwrite=True,
                    record_source_type=managed_source.source_type,
                    expected_slug=slug,
                )
            except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if was_disabled:
                disable_recipe(recipes_dir / updated_slug)
            _reload_registry(request.app, app_version=app_version)

        return JSONResponse(
            content={
                "ok": True,
                "action": "update",
                "slug": updated_slug,
                "source_type": source_type,
            }
        )

    @app.post("/api/recipes/manage/uninstall/{slug}")
    async def recipes_manage_uninstall(
        slug: str,
        request: Request,
        force: bool = Query(default=False),
    ) -> JSONResponse:
        """Uninstall a recipe and optionally force-remove unmanaged folders."""
        lock: asyncio.Lock = request.app.state.recipe_admin_lock
        async with lock:
            recipes_dir: Path = request.app.state.recipes_dir
            entries = discover_recipe_entries(recipes_dir)
            entry = find_recipe_entry(entries, slug)
            manifest = load_manifest(recipes_dir)
            manifest_record = get_manifest_record(manifest, slug)

            if entry is None and manifest_record is None:
                raise HTTPException(status_code=404, detail=f"recipe '{slug}' was not found")
            if manifest_record is None and not force:
                raise HTTPException(
                    status_code=400,
                    detail=f"recipe '{slug}' is not tracked in manifest (pass force=true)",
                )

            recipe_path = recipes_dir / resolve_recipe_folder(
                slug=slug,
                entry=entry,
                manifest_record=manifest_record,
            )
            if recipe_path.exists():
                shutil.rmtree(recipe_path)
            removed = remove_manifest_record(recipes_dir, slug)
            _reload_registry(request.app, app_version=app_version)

        return JSONResponse(
            content={
                "ok": True,
                "action": "uninstall",
                "slug": slug,
                "forced": force,
                "removed_manifest_record": removed,
            }
        )

    @app.post("/api/recipes/manage/enable/{slug}")
    async def recipes_manage_enable(slug: str, request: Request) -> JSONResponse:
        """Enable an installed recipe."""
        lock: asyncio.Lock = request.app.state.recipe_admin_lock
        async with lock:
            entries = discover_recipe_entries(request.app.state.recipes_dir)
            entry = find_recipe_entry(entries, slug)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"recipe '{slug}' was not found")
            if not entry.enabled:
                enable_recipe(entry.path)
                _reload_registry(request.app, app_version=app_version)
        return JSONResponse(content={"ok": True, "action": "enable", "slug": slug})

    @app.post("/api/recipes/manage/disable/{slug}")
    async def recipes_manage_disable(slug: str, request: Request) -> JSONResponse:
        """Disable an installed recipe."""
        lock: asyncio.Lock = request.app.state.recipe_admin_lock
        async with lock:
            entries = discover_recipe_entries(request.app.state.recipes_dir)
            entry = find_recipe_entry(entries, slug)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"recipe '{slug}' was not found")
            if entry.enabled:
                disable_recipe(entry.path)
                _reload_registry(request.app, app_version=app_version)
        return JSONResponse(content={"ok": True, "action": "disable", "slug": slug})
