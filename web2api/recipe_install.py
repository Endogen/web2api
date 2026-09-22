"""Recipe dependency, update, source checkout, and installation services."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from web2api.plugin import PluginConfig, build_plugin_payload
from web2api.recipe_loader import (
    load_plugin_config,
    load_recipe_config,
    validate_scraper_source,
)
from web2api.recipe_store import (
    DISABLED_MARKER,
    SourceType,
    load_manifest,
    record_recipe_install,
)

logger = logging.getLogger(__name__)

def build_install_commands(
    plugin: PluginConfig,
    *,
    include_apt: bool = True,
    include_npm: bool = True,
    include_python: bool = True,
) -> list[list[str]]:
    """Build install commands from recipe metadata."""
    commands: list[list[str]] = []
    if include_apt and plugin.dependencies.apt_packages:
        commands.append(["apt-get", "update"])
        commands.append(["apt-get", "install", "-y", *plugin.dependencies.apt_packages])
    if include_npm and plugin.dependencies.npm_packages:
        commands.append(["npm", "install", "-g", *plugin.dependencies.npm_packages])
    if include_python and plugin.dependencies.python_packages:
        commands.append(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                *plugin.dependencies.python_packages,
            ]
        )
    return commands


def build_dockerfile_snippet(commands: list[list[str]]) -> str:
    """Render Dockerfile RUN lines for install commands."""
    if not commands:
        return "# No recipe dependency install steps."
    rendered = ["# Add these lines to your Dockerfile for recipe dependencies:"]
    for command in commands:
        rendered.append(f"RUN {shlex.join(command)}")
    return "\n".join(rendered)


def run_commands(
    commands: list[list[str]],
    *,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Execute commands sequentially with optional dry-run mode."""
    executor = runner or subprocess.run
    for command in commands:
        logger.info("Executing: %s", " ".join(command))
        if dry_run:
            continue
        executor(command, check=True, text=True)


def metadata_status_payload(plugin: PluginConfig, *, app_version: str) -> dict[str, object]:
    """Build metadata payload with computed readiness status."""
    return build_plugin_payload(plugin, current_web2api_version=app_version)


def run_healthcheck(
    plugin: PluginConfig,
    *,
    timeout_seconds: float = 15.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run recipe metadata healthcheck command and return structured status."""
    healthcheck = plugin.healthcheck
    if healthcheck is None:
        return {"defined": False, "ran": False, "ok": None}

    command = healthcheck.command
    result_payload: dict[str, Any] = {
        "defined": True,
        "ran": not dry_run,
        "ok": None if dry_run else False,
        "command": command,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }

    if dry_run:
        return result_payload

    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        result_payload["stderr"] = f"command not found: {exc.filename}"
        return result_payload
    except subprocess.TimeoutExpired:
        result_payload["stderr"] = f"healthcheck timed out after {timeout_seconds}s"
        return result_payload

    result_payload["exit_code"] = proc.returncode
    result_payload["stdout"] = proc.stdout.strip()
    result_payload["stderr"] = proc.stderr.strip()
    result_payload["ok"] = proc.returncode == 0
    return result_payload


def compute_tree_hash(repo_dir: Path, subdir: str | None = None) -> str | None:
    """Compute git tree hash for a directory within a repo.

    Uses ``git rev-parse HEAD:<subdir>`` (or ``HEAD^{tree}`` for root).
    Returns ``None`` if *repo_dir* is not a git repo or the command fails.
    """
    try:
        ref = "HEAD^{tree}"
        if subdir and subdir not in (".", ""):
            cleaned = subdir.strip("/")
            if cleaned and cleaned != ".":
                ref = f"HEAD:{cleaned}"
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", ref],
            check=True,
            text=True,
            capture_output=True,
        )
        stdout = result.stdout
        if not isinstance(stdout, str):
            return None
        return stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def check_recipe_updates(recipes_dir: Path) -> dict[str, bool | None]:
    """Check all managed git-sourced recipes for updates.

    Returns ``{slug: update_available}`` where:

    * ``True`` – remote tree hash differs from installed
    * ``False`` – hashes match, no update
    * ``None`` – could not determine (non-git source, fetch failed, no stored hash)

    Recipes are grouped by ``(source_url, source_ref)`` so we only fetch each
    remote once, then resolve multiple subdirs from the same fetch.
    """
    manifest = load_manifest(recipes_dir)
    recipes = manifest.get("recipes", {})
    if not isinstance(recipes, dict):
        return {}

    results: dict[str, bool | None] = {}

    # Group by (source, source_ref) for efficient fetching.
    groups: dict[tuple[str, str | None], list[tuple[str, str | None, str | None]]] = {}
    for slug, record in recipes.items():
        if not isinstance(record, dict):
            results[slug] = None
            continue
        source_type = record.get("source_type")
        if source_type not in ("git", "catalog"):
            results[slug] = None
            continue
        installed_hash = record.get("installed_tree_hash")
        if not isinstance(installed_hash, str) or not installed_hash:
            results[slug] = None
            continue
        source = record.get("source")
        if not isinstance(source, str) or not source.strip():
            results[slug] = None
            continue
        source_ref = record.get("source_ref")
        if not isinstance(source_ref, str):
            source_ref = None
        source_subdir = record.get("source_subdir")
        if not isinstance(source_subdir, str):
            source_subdir = None

        key = (source.strip(), source_ref)
        groups.setdefault(key, []).append((slug, source_subdir, installed_hash))

    for (source, source_ref), entries in groups.items():
        with tempfile.TemporaryDirectory(prefix="web2api-hash-check-") as tmp_dir:
            target = Path(tmp_dir) / "repo"
            try:
                subprocess.run(
                    ["git", "init", "--quiet", str(target)],
                    check=True, text=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "remote", "add", "origin", source],
                    check=True, text=True, capture_output=True,
                )
                fetch_ref = source_ref or "HEAD"
                subprocess.run(
                    [
                        "git", "-C", str(target),
                        "fetch", "--quiet", "--depth", "1", "--filter=blob:none",
                        "origin", fetch_ref,
                    ],
                    check=True, text=True, capture_output=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                for slug, _, _ in entries:
                    results[slug] = None
                continue

            for slug, subdir, installed_hash in entries:
                try:
                    ref = "FETCH_HEAD^{tree}"
                    if subdir and subdir not in (".", ""):
                        cleaned = subdir.strip("/")
                        if cleaned and cleaned != ".":
                            ref = f"FETCH_HEAD:{cleaned}"
                    result = subprocess.run(
                        ["git", "-C", str(target), "rev-parse", ref],
                        check=True, text=True, capture_output=True,
                    )
                    remote_hash = result.stdout.strip()
                    results[slug] = remote_hash != installed_hash
                except (subprocess.CalledProcessError, FileNotFoundError):
                    results[slug] = None

    return results


def resolve_source_type(source: str) -> SourceType:
    """Resolve recipe source type from source value."""
    if Path(source).expanduser().exists():
        return "local"
    return "git"


@contextmanager
def checkout_source(
    source: str,
    *,
    source_ref: str | None = None,
    source_type: SourceType | None = None,
    sparse_paths: list[str] | None = None,
) -> Path:
    """Yield a local checkout path for a source value."""
    resolved_type = source_type or resolve_source_type(source)
    if resolved_type == "local":
        yield Path(source).expanduser().resolve()
        return

    normalized_sparse_paths: list[str] = []
    if sparse_paths is not None:
        normalized_sparse_paths = [
            path.strip()
            for path in sparse_paths
            if isinstance(path, str) and path.strip()
        ]

    with tempfile.TemporaryDirectory(prefix="web2api-recipe-src-") as tmp_dir:
        target = Path(tmp_dir) / "repo"
        if normalized_sparse_paths:
            try:
                subprocess.run(
                    ["git", "init", "--quiet", str(target)],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "remote", "add", "origin", source],
                    check=True,
                    text=True,
                )
                fetch_ref = source_ref or "HEAD"
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "fetch",
                        "--quiet",
                        "--depth",
                        "1",
                        "--filter=blob:none",
                        "origin",
                        fetch_ref,
                    ],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "sparse-checkout", "init", "--cone"],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "sparse-checkout",
                        "set",
                        *normalized_sparse_paths,
                    ],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "checkout", "--quiet", "FETCH_HEAD"],
                    check=True,
                    text=True,
                )
            except subprocess.CalledProcessError:
                logger.info(
                    "Sparse checkout failed for %s; falling back to full clone.",
                    source,
                )
                if target.exists():
                    shutil.rmtree(target)
                clone_cmd = ["git", "clone", "--quiet", source, str(target)]
                subprocess.run(clone_cmd, check=True, text=True)
                if source_ref is not None:
                    subprocess.run(
                        ["git", "-C", str(target), "checkout", "--quiet", source_ref],
                        check=True,
                        text=True,
                    )
        else:
            clone_cmd = ["git", "clone", "--quiet", source, str(target)]
            subprocess.run(clone_cmd, check=True, text=True)
            if source_ref is not None:
                subprocess.run(
                    ["git", "-C", str(target), "checkout", "--quiet", source_ref],
                    check=True,
                    text=True,
                )
        yield target


def resolve_recipe_source_dir(source_root: Path, subdir: str | None = None) -> Path:
    """Resolve recipe directory inside source root."""
    resolved_root = source_root.resolve()
    if subdir is not None:
        recipe_dir = (resolved_root / subdir).resolve()
        if recipe_dir != resolved_root and resolved_root not in recipe_dir.parents:
            raise ValueError(f"source subdir escapes source root: {subdir!r}")
        if not recipe_dir.exists() or not recipe_dir.is_dir():
            raise ValueError(f"source subdir does not exist or is not a directory: {recipe_dir}")
        if not (recipe_dir / "recipe.yaml").exists():
            raise ValueError(f"source subdir does not contain recipe.yaml: {recipe_dir}")
        return recipe_dir

    if (resolved_root / "recipe.yaml").exists():
        return resolved_root

    candidates = [
        child
        for child in sorted(resolved_root.iterdir())
        if child.is_dir() and (child / "recipe.yaml").exists()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f"no recipe.yaml found in source: {resolved_root}")
    candidate_names = ", ".join(c.name for c in candidates)
    raise ValueError(
        f"source contains multiple recipes; pass --subdir. Candidates: {candidate_names}"
    )


def load_source_recipe_slug(source_recipe_dir: Path) -> str:
    """Load and validate a source recipe slug."""
    return load_recipe_config(source_recipe_dir, require_matching_folder=False).slug


def validate_source_recipe_dir(source_recipe_dir: Path, *, trusted: bool) -> str:
    """Validate recipe metadata and trusted scraper structure before installation."""
    config = load_recipe_config(source_recipe_dir, require_matching_folder=False)
    load_plugin_config(source_recipe_dir)
    if trusted:
        validate_scraper_source(source_recipe_dir)
    elif (source_recipe_dir / "scraper.py").exists():
        logger.warning(
            "Skipping custom scraper validation for untrusted recipe '%s'; "
            "the scraper will remain disabled until the recipe is trusted",
            config.slug,
        )
    return config.slug


def copy_recipe_into_recipes_dir(
    source_recipe_dir: Path,
    recipes_dir: Path,
    *,
    overwrite: bool = False,
) -> tuple[str, Path]:
    """Atomically copy a recipe into place using its slug as the folder name."""
    slug = load_source_recipe_slug(source_recipe_dir)
    recipes_dir.mkdir(parents=True, exist_ok=True)
    destination = recipes_dir / slug

    if destination.exists() and not overwrite:
        raise ValueError(f"destination recipe already exists: {destination}")

    staging_root = Path(tempfile.mkdtemp(prefix=".web2api-install-", dir=recipes_dir))
    staged_recipe = staging_root / slug
    backup = staging_root / "previous"
    try:
        shutil.copytree(source_recipe_dir, staged_recipe)
        (staged_recipe / DISABLED_MARKER).unlink(missing_ok=True)

        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staged_recipe, destination)
        except Exception:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return slug, destination


def install_recipe_from_source(
    *,
    source: str,
    recipes_dir: Path,
    source_ref: str | None = None,
    source_subdir: str | None = None,
    trusted: bool,
    overwrite: bool = False,
    record_source_type: SourceType | None = None,
    expected_slug: str | None = None,
) -> tuple[str, SourceType]:
    """Install a recipe from source path/git and persist manifest record."""
    resolved_source_type = resolve_source_type(source)
    manifest_source_type = record_source_type or resolved_source_type
    sparse_paths = (
        [source_subdir]
        if resolved_source_type == "git" and source_subdir
        else None
    )

    installed_tree_hash: str | None = None
    with checkout_source(
        source,
        source_ref=source_ref,
        source_type=resolved_source_type,
        sparse_paths=sparse_paths,
    ) as source_root:
        source_recipe_dir = resolve_recipe_source_dir(source_root, source_subdir)
        source_slug = validate_source_recipe_dir(source_recipe_dir, trusted=trusted)
        if expected_slug is not None and source_slug != expected_slug:
            raise ValueError(
                f"source recipe slug '{source_slug}' does not match expected slug '{expected_slug}'"
            )
        # Compute tree hash for the recipe subdirectory within the checkout.
        try:
            rel = source_recipe_dir.relative_to(source_root)
            tree_subdir = str(rel) if str(rel) != "." else None
        except ValueError:
            tree_subdir = None
        installed_tree_hash = compute_tree_hash(source_root, tree_subdir)

        recipes_dir.mkdir(parents=True, exist_ok=True)
        rollback_root = Path(tempfile.mkdtemp(prefix=".web2api-rollback-", dir=recipes_dir))
        rollback_recipe = rollback_root / source_slug
        existing_destination = recipes_dir / source_slug
        had_existing = existing_destination.exists()
        if had_existing:
            shutil.copytree(existing_destination, rollback_recipe)
        try:
            slug, destination = copy_recipe_into_recipes_dir(
                source_recipe_dir,
                recipes_dir,
                overwrite=overwrite,
            )
            record_recipe_install(
                recipes_dir,
                slug=slug,
                folder=destination.name,
                source_type=manifest_source_type,
                source=source,
                source_ref=source_ref,
                source_subdir=source_subdir,
                trusted=trusted,
                installed_tree_hash=installed_tree_hash,
            )
        except Exception:
            if existing_destination.exists():
                shutil.rmtree(existing_destination)
            if had_existing and rollback_recipe.exists():
                os.replace(rollback_recipe, existing_destination)
            raise
        finally:
            shutil.rmtree(rollback_root, ignore_errors=True)
    return slug, manifest_source_type
