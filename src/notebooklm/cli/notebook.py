"""Notebook management CLI commands.

Commands:
    list       List all notebooks
    create     Create a new notebook
    delete     Delete a notebook
    rename     Rename a notebook
    summary    Get notebook summary with AI-generated insights
    metadata   Export notebook metadata with sources list

Note: Sharing commands moved to 'share' command group.
"""

import json
from pathlib import Path

import click
from rich.table import Table

from ..client import NotebookLMClient
from .helpers import (
    clear_context,
    console,
    get_current_notebook,
    json_output_response,
    require_notebook,
    resolve_notebook_id,
    set_current_notebook,
    with_client,
)

_ALLOWED_BOOTSTRAP_SOURCE_TYPES = {"url", "text", "file", "youtube"}
_ALLOWED_BOOTSTRAP_IF_EXISTS = {"reuse", "error", "create"}


def _load_bootstrap_manifest(manifest_path: Path) -> dict:
    path = Path(manifest_path).expanduser().resolve()
    if path.suffix.lower() != ".json":
        raise click.ClickException("Bootstrap manifest must be a .json file")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise click.ClickException(f"Manifest not found: {path}") from e
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON in manifest: {e}") from e

    if not isinstance(data, dict):
        raise click.ClickException("Bootstrap manifest root must be a JSON object")

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise click.ClickException("Bootstrap manifest requires a non-empty string field: title")

    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list):
        raise click.ClickException("Bootstrap manifest field 'sources' must be a list")

    if_exists = data.get("if_exists", "reuse")
    if if_exists not in _ALLOWED_BOOTSTRAP_IF_EXISTS:
        raise click.ClickException(
            "Bootstrap manifest field 'if_exists' must be one of: create, error, reuse"
        )

    use_context = data.get("use", False)
    if not isinstance(use_context, bool):
        raise click.ClickException("Bootstrap manifest field 'use' must be true or false")

    sources = []
    for index, raw_source in enumerate(raw_sources, 1):
        if not isinstance(raw_source, dict):
            raise click.ClickException(f"Bootstrap source #{index} must be an object")

        content = raw_source.get("content")
        if not isinstance(content, str) or not content.strip():
            raise click.ClickException(
                f"Bootstrap source #{index} requires a non-empty string field: content"
            )

        source_type = raw_source.get("type")
        if source_type is not None and source_type not in _ALLOWED_BOOTSTRAP_SOURCE_TYPES:
            raise click.ClickException(
                f"Bootstrap source #{index} has invalid type '{source_type}'. "
                "Expected one of: file, text, url, youtube"
            )

        source_title = raw_source.get("title")
        if source_title is not None and not isinstance(source_title, str):
            raise click.ClickException(f"Bootstrap source #{index} field 'title' must be a string")

        mime_type = raw_source.get("mime_type")
        if mime_type is not None and not isinstance(mime_type, str):
            raise click.ClickException(
                f"Bootstrap source #{index} field 'mime_type' must be a string"
            )

        sources.append(
            {
                "type": source_type,
                "content": content,
                "title": source_title,
                "mime_type": mime_type,
            }
        )

    return {
        "path": str(path),
        "title": title.strip(),
        "if_exists": if_exists,
        "use": use_context,
        "sources": sources,
    }


def _detect_bootstrap_source_type(source: dict) -> str:
    source_type = source.get("type")
    if source_type is not None:
        return source_type

    content = source["content"]
    if content.startswith(("http://", "https://")):
        lowered = content.lower()
        if "youtube.com/" in lowered or "youtu.be/" in lowered:
            return "youtube"
        return "url"

    if Path(content).expanduser().exists():
        return "file"

    return "text"


def _find_notebook_by_title(notebooks: list, title: str):
    for notebook in notebooks:
        if getattr(notebook, "title", None) == title:
            return notebook
    return None


def register_notebook_commands(cli):
    """Register notebook commands on the main CLI group."""

    @cli.command("list")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @with_client
    def list_cmd(ctx, json_output, client_auth):
        """List all notebooks."""

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                notebooks = await client.notebooks.list()

                if json_output:
                    data = {
                        "notebooks": [
                            {
                                "index": i,
                                "id": nb.id,
                                "title": nb.title,
                                "is_owner": nb.is_owner,
                                "created_at": nb.created_at.isoformat() if nb.created_at else None,
                            }
                            for i, nb in enumerate(notebooks, 1)
                        ],
                        "count": len(notebooks),
                    }
                    json_output_response(data)
                    return

                table = Table(title="Notebooks")
                table.add_column("ID", style="cyan")
                table.add_column("Title", style="green")
                table.add_column("Owner")
                table.add_column("Created", style="dim")

                for nb in notebooks:
                    created = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else "-"
                    owner_status = "Owner" if nb.is_owner else "Shared"
                    table.add_row(nb.id, nb.title, owner_status, created)

                console.print(table)

        return _run()

    @cli.command("create")
    @click.argument("title")
    @click.option(
        "--use",
        "-u",
        "switch_context",
        is_flag=True,
        help="Set the new notebook as the current context (like 'notebooklm use <id>').",
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @with_client
    def create_cmd(ctx, title, switch_context, json_output, client_auth):
        """Create a new notebook.

        By default, creates the notebook without changing the active context.
        Pass --use (or -u) to make the new notebook the current context, so
        subsequent commands like 'source add' target it.
        """

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                nb = await client.notebooks.create(title)

                if switch_context:
                    created_str = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else None
                    set_current_notebook(nb.id, nb.title, nb.is_owner, created_str)

                if json_output:
                    data = {
                        "notebook": {
                            "id": nb.id,
                            "title": nb.title,
                            "created_at": nb.created_at.isoformat() if nb.created_at else None,
                        }
                    }
                    json_output_response(data)
                    return

                console.print(f"[green]Created notebook:[/green] {nb.id} - {nb.title}")
                if switch_context:
                    console.print("[dim]Context set to new notebook[/dim]")
                else:
                    console.print(
                        f"[dim]Tip: pass --use next time, or run 'notebooklm use {nb.id}'.[/dim]"
                    )

        return _run()

    @cli.command("bootstrap")
    @click.argument("manifest_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @with_client
    def bootstrap_cmd(ctx, manifest_path, json_output, client_auth):
        """Create or reuse a notebook from a JSON manifest, then import sources.

        Manifest schema:
          {
            "title": "Notebook title",
            "if_exists": "reuse|error|create",
            "use": true,
            "sources": [
              {"type": "file", "content": "./docs/a.md"},
              {"type": "url", "content": "https://example.com"},
              {"type": "text", "title": "Notes", "content": "hello"}
            ]
          }

        Source type may be omitted and will be auto-detected like `source add`.
        """
        manifest = _load_bootstrap_manifest(manifest_path)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                notebooks = await client.notebooks.list()
                notebook = None
                notebook_action = "created"

                if manifest["if_exists"] in {"reuse", "error"}:
                    notebook = _find_notebook_by_title(notebooks, manifest["title"])
                    if notebook is not None:
                        if manifest["if_exists"] == "error":
                            raise click.ClickException(
                                f"Notebook already exists with title: {manifest['title']}"
                            )
                        notebook_action = "reused"

                if notebook is None:
                    notebook = await client.notebooks.create(manifest["title"])

                if manifest["use"]:
                    created_str = (
                        notebook.created_at.strftime("%Y-%m-%d") if notebook.created_at else None
                    )
                    set_current_notebook(
                        notebook.id,
                        notebook.title,
                        getattr(notebook, "is_owner", True),
                        created_str,
                    )

                added_sources = []
                for source in manifest["sources"]:
                    detected_type = _detect_bootstrap_source_type(source)
                    content = source["content"]
                    title = source.get("title")
                    mime_type = source.get("mime_type")

                    if detected_type in {"url", "youtube"}:
                        src = await client.sources.add_url(notebook.id, content)
                    elif detected_type == "text":
                        src = await client.sources.add_text(notebook.id, title or "Untitled", content)
                    else:
                        src = await client.sources.add_file(
                            notebook.id,
                            content,
                            mime_type,
                            title=title,
                        )

                    added_sources.append(
                        {
                            "id": src.id,
                            "title": src.title,
                            "type": str(src.kind),
                            "url": src.url,
                            "detected_type": detected_type,
                        }
                    )

                if json_output:
                    json_output_response(
                        {
                            "manifest_path": manifest["path"],
                            "notebook": {
                                "id": notebook.id,
                                "title": notebook.title,
                                "action": notebook_action,
                            },
                            "use": manifest["use"],
                            "sources_added": added_sources,
                            "source_count": len(added_sources),
                        }
                    )
                    return

                console.print(
                    f"[green]{notebook_action.title()} notebook:[/green] {notebook.id} - {notebook.title}"
                )
                console.print(
                    f"[green]Imported {len(added_sources)} source(s)[/green] from {manifest['path']}"
                )
                if manifest["use"]:
                    console.print("[dim]Context set to bootstrapped notebook[/dim]")

        if not json_output:
            with console.status("Bootstrapping notebook from manifest..."):
                return _run()
        return _run()

    @cli.command("delete")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set). Supports partial IDs.",
    )
    @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
    @with_client
    def delete_cmd(ctx, notebook_id, yes, client_auth):
        """Delete a notebook.

        Supports partial IDs - 'notebooklm delete -n abc' matches 'abc123...'
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                # Resolve partial ID to full ID
                resolved_id = await resolve_notebook_id(client, notebook_id)

                # Confirm after resolution so user sees the full ID
                if not yes and not click.confirm(f"Delete notebook {resolved_id}?"):
                    return

                success = await client.notebooks.delete(resolved_id)
                if success:
                    console.print(f"[green]Deleted notebook:[/green] {resolved_id}")
                    # Clear context if we deleted the current notebook
                    if get_current_notebook() == resolved_id:
                        clear_context()
                        console.print("[dim]Cleared current notebook context[/dim]")
                else:
                    console.print("[yellow]Delete may have failed[/yellow]")

        return _run()

    @cli.command("rename")
    @click.argument("new_title")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set). Supports partial IDs.",
    )
    @with_client
    def rename_cmd(ctx, new_title, notebook_id, client_auth):
        """Rename a notebook.

        NOTEBOOK_ID supports partial matching (e.g., 'abc' matches 'abc123...').
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                resolved_id = await resolve_notebook_id(client, notebook_id)
                await client.notebooks.rename(resolved_id, new_title)
                console.print(f"[green]Renamed notebook:[/green] {resolved_id}")
                console.print(f"[bold]New title:[/bold] {new_title}")

        return _run()

    @cli.command("summary")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set). Supports partial IDs.",
    )
    @click.option("--topics", is_flag=True, help="Include suggested topics")
    @with_client
    def summary_cmd(ctx, notebook_id, topics, client_auth):
        """Get notebook summary with AI-generated insights.

        NOTEBOOK_ID supports partial matching (e.g., 'abc' matches 'abc123...').

        \b
        Examples:
          notebooklm summary              # Summary only
          notebooklm summary --topics     # With suggested topics
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                resolved_id = await resolve_notebook_id(client, notebook_id)
                description = await client.notebooks.get_description(resolved_id)
                if description and description.summary:
                    console.print("[bold cyan]Summary:[/bold cyan]")
                    console.print(description.summary)

                    if topics and description.suggested_topics:
                        console.print("\n[bold cyan]Suggested Topics:[/bold cyan]")
                        for i, topic in enumerate(description.suggested_topics, 1):
                            console.print(f"  {i}. {topic.question}")
                else:
                    console.print("[yellow]No summary available[/yellow]")

        return _run()

    @cli.command("metadata")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set). Supports partial IDs.",
    )
    @click.option(
        "--json",
        "json_output",
        is_flag=True,
        help="Output as JSON (default: human-readable)",
    )
    @with_client
    def metadata_cmd(ctx, notebook_id, json_output, client_auth):
        """Export notebook metadata with sources list.

        Outputs notebook details (id, title, created_at, is_owner) along with
        a simplified list of sources (type, title, url).

        By default, outputs in human-readable format. Use --json for machine parsing.

        NOTEBOOK_ID supports partial matching (e.g., 'abc' matches 'abc123...').

        \b
        Examples:
          notebooklm metadata              # Human-readable for current notebook
          notebooklm metadata -n abc       # Human-readable for notebook starting with 'abc'
          notebooklm metadata --json       # JSON output
          notebooklm metadata -n abc --json  # JSON for specific notebook
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                # Resolve partial ID
                resolved_id = await resolve_notebook_id(
                    client, notebook_id, json_output=json_output
                )

                # Get metadata (use notebooks.get_metadata)
                metadata = await client.notebooks.get_metadata(resolved_id)

                if json_output:
                    # JSON output
                    data = metadata.to_dict()
                    json_output_response(data)
                else:
                    # Human-readable output (default)
                    console.print(f"[bold cyan]Notebook:[/bold cyan] {metadata.title}")
                    console.print(f"[dim]ID:[/dim] {metadata.id}")
                    if metadata.created_at:
                        console.print(
                            f"[dim]Created:[/dim] {metadata.created_at.strftime('%Y-%m-%d %H:%M')}"
                        )
                    owner_status = "Owner" if metadata.is_owner else "Shared"
                    console.print(f"[dim]Access:[/dim] {owner_status}")

                    console.print(f"\n[bold]Sources ({len(metadata.sources)}):[/bold]")
                    if not metadata.sources:
                        console.print("[dim]  No sources[/dim]")
                    else:
                        for i, source in enumerate(metadata.sources, 1):
                            source_type = source.kind.value
                            title = source.title or "(untitled)"

                            # Always print the source line (use Text to avoid Rich markup interpretation)
                            from rich.text import Text

                            console.print(
                                Text(f"  {i}. "),
                                Text(f"[{source_type}]", style="default"),
                                Text(f" {title}"),
                            )
                            if source.url:
                                console.print(f"     {source.url}")

        return _run()
