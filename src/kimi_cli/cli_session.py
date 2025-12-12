"""Session management commands for Kimi CLI (style: kimi session <cmd>).

This module provides Docker-style CLI commands for session management:
- kimi session list              List all sessions
- kimi session continue <id>     Resume a specific session
- kimi session clone <id>        Clone a session
- kimi session resume <file>     Resume from context.json file
- kimi session orphan            List orphaned sessions
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from kimi_cli.metadata import load_metadata, save_metadata
from kimi_cli.session import Session
from kimi_cli.utils.logging import logger

console = Console()

session_app = typer.Typer(help="Session management commands")


def _format_timestamp(timestamp: float) -> str:
    """Format timestamp for display."""
    import time
    if timestamp == 0:
        return "N/A"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except:
        return str(timestamp)


@session_app.command("list")
def session_list():
    """List all sessions across all work directories."""
    metadata = load_metadata()
    sessions = metadata.list_all_sessions()
    
    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return
    
    table = Table(title="All Sessions", border_style="wheat4")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Work Dir", style="magenta", max_width=40)
    table.add_column("Messages", justify="right", style="green")
    table.add_column("Size", justify="right", style="blue")
    table.add_column("Updated", style="yellow")
    table.add_column("Status", style="red")
    
    for session in sessions:
        work_dir_path = session.get("work_dir_path", "N/A")
        if work_dir_path and len(work_dir_path) > 40:
            work_dir_path = "..." + work_dir_path[-37:]
        
        status = "🗑️  Orphaned" if session.get("orphaned") else "✓ Active"
        
        table.add_row(
            session["session_id"],
            work_dir_path or "Unknown",
            str(session.get("message_count", 0)),
            f"{session.get('size', 0) / 1024 / 1024:.2f}MB",
            _format_timestamp(session.get("updated_at", 0)),
            status
        )
    
    console.print(table)
    console.print(f"\n[green]Total: {len(sessions)} sessions[/green]")
    
    orphaned_count = sum(1 for s in sessions if s.get("orphaned"))
    if orphaned_count > 0:
        console.print(f"[yellow]Orphaned: {orphaned_count} sessions[/yellow]")


@session_app.command("continue")
def session_continue(
    session_id: str = typer.Argument(..., help="Session ID to continue (UUID format)")
):
    """Resume a specific session by ID."""
    # Validate UUID
    try:
        uuid.UUID(session_id)
    except ValueError:
        console.print(f"[red]Error: Invalid session ID format[/red]")
        console.print("Session ID should be a UUID like: 123e4567-e89b-12d3-a456-426614174000")
        raise typer.Exit(1)
    
    async def _run():
        from kaos.path import KaosPath
        
        metadata = load_metadata()
        current_path = KaosPath(str(Path.cwd().resolve()))
        
        # Find the session
        target_work_dir = None
        target_work_dir_id = None
        
        # Check registered work directories
        for wd in metadata.work_dirs:
            session_dir = wd.sessions_dir / session_id
            context_file = session_dir / "context.jsonl"
            if context_file.exists():
                target_work_dir = wd
                target_work_dir_id = wd.id
                break
        
        # Check orphaned sessions
        if not target_work_dir:
            share_dir = Path.home() / ".kimi"
            sessions_base = share_dir / "sessions"
            if sessions_base.exists():
                for work_dir_dir in sessions_base.iterdir():
                    if not work_dir_dir.is_dir():
                        continue
                    session_dir = work_dir_dir / session_id
                    context_file = session_dir / "context.jsonl"
                    if context_file.exists():
                        target_work_dir_id = work_dir_dir.name
                        
                        # Reuse or create work directory metadata
                        for wd in metadata.work_dirs:
                            if wd.id == work_dir_dir.name:
                                target_work_dir = wd
                                wd.path = str(Path.cwd().resolve())
                                break
                        
                        if not target_work_dir:
                            target_work_dir = metadata.new_work_dir_meta(current_path, work_dir_dir.name)
                        
                        break
        
        if not target_work_dir:
            console.print(f"[red]Error: Session '{session_id}' not found[/red]")
            raise typer.Exit(1)
        
        # Update work directory's last session
        target_work_dir.last_session_id = session_id
        save_metadata(metadata)
        
        console.print(f"[green]✓ Resuming session: {session_id}[/green]")
        console.print(f"  Work directory: {target_work_dir.path}")
        
        # Create and run session
        session = await Session.find(current_path, session_id)
        if not session:
            console.print(f"[red]Error: Could not load session {session_id}[/red]")
            raise typer.Exit(1)
        
        from kimi_cli.app import KimiCLI, enable_logging
        from kimi_cli.agentspec import DEFAULT_AGENT_FILE
        
        enable_logging(False)
        
        metadata = load_metadata()
        thinking_mode = metadata.thinking
        
        instance = await KimiCLI.create(
            session,
            yolo=False,
            mcp_configs=[],
            model_name=None,
            thinking=thinking_mode,
            agent_file=DEFAULT_AGENT_FILE,
        )
        
        await instance.run_shell()
    
    asyncio.run(_run())


@session_app.command("clone")
def session_clone(
    session_id: str = typer.Argument(..., help="Session ID to clone"),
    new_name: Optional[str] = typer.Option(None, help="Optional new session name")
):
    """Create a new session from an existing one."""
    # Validate UUID
    try:
        uuid.UUID(session_id)
    except ValueError:
        console.print(f"[red]Error: Invalid session ID format[/red]")
        raise typer.Exit(1)
    
    metadata = load_metadata()
    
    # Find the session
    source_context_file = None
    source_work_dir_id = None
    
    for wd in metadata.work_dirs:
        session_dir = wd.sessions_dir / session_id
        context_file = session_dir / "context.jsonl"
        if context_file.exists():
            source_context_file = context_file
            source_work_dir_id = wd.id
            break
    
    # Check orphaned sessions
    if not source_context_file:
        share_dir = Path.home() / ".kimi"
        sessions_base = share_dir / "sessions"
        if sessions_base.exists():
            for work_dir_dir in sessions_base.iterdir():
                if not work_dir_dir.is_dir():
                    continue
                session_dir = work_dir_dir / session_id
                context_file = session_dir / "context.jsonl"
                if context_file.exists():
                    source_context_file = context_file
                    source_work_dir_id = work_dir_dir.name
                    break
    
    if not source_context_file:
        console.print(f"[red]Error: Session '{session_id}' not found[/red]")
        raise typer.Exit(1)
    
    # Get current work directory
    current_path = Path.cwd().resolve()
    
    # Find or create work directory metadata
    from kaos.path import KaosPath
    current_kaos_path = KaosPath(str(current_path))
    
    current_work_dir = None
    for wd in metadata.work_dirs:
        if wd.matches_path(current_kaos_path):
            current_work_dir = wd
            break
    
    if not current_work_dir:
        current_work_dir = metadata.new_work_dir_meta(current_kaos_path)
    
    # Create new session with new UUID
    new_session_id = str(uuid.uuid4())
    new_session_dir = current_work_dir.sessions_dir / new_session_id
    new_session_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy context file
    new_context_file = new_session_dir / "context.jsonl"
    shutil.copy2(source_context_file, new_context_file)
    
    # Update work directory's last session
    current_work_dir.last_session_id = new_session_id
    save_metadata(metadata)
    
    console.print(f"[green]✓ Created new session: {new_session_id}[/green]")
    console.print(f"  Cloned from: {session_id}")
    
    # Show session info
    try:
        if new_context_file.exists():
            message_count = sum(1 for _ in open(new_context_file))
            console.print(f"  Messages copied: {message_count}")
    except:
        pass


@session_app.command("resume")
def session_resume(
    context_file: Path = typer.Argument(..., help="Path to context.json file", exists=True)
):
    """Resume a session from a context.json file."""
    if not context_file.is_file():
        console.print(f"[red]Error: File not found: {context_file}[/red]")
        raise typer.Exit(1)
    
    if context_file.suffix != ".json" and context_file.suffix != ".jsonl":
        console.print(f"[yellow]Warning: File does not have .json or .jsonl extension[/yellow]")
    
    async def _run():
        from kaos.path import KaosPath
        
        current_path = KaosPath(str(Path.cwd().resolve()))
        
        # Create new session
        session = await Session.create(current_path)
        
        # Copy the provided context file to the new session
        target_context_file = session.context_file
        target_context_file.parent.mkdir(parents=True, exist_ok=True)
        
        import shutil
        shutil.copy2(context_file, target_context_file)
        
        console.print(f"[green]✓ Resumed session from: {context_file}[/green]")
        console.print(f"  New session ID: {session.id}")
        console.print(f"  Location: {target_context_file}")
        
        # Update metadata
        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(current_path)
        if work_dir_meta is None:
            work_dir_meta = metadata.new_work_dir_meta(current_path)
        work_dir_meta.last_session_id = session.id
        save_metadata(metadata)
        
        # Run the session
        from kimi_cli.app import KimiCLI, enable_logging
        from kimi_cli.agentspec import DEFAULT_AGENT_FILE
        
        enable_logging(False)
        instance = await KimiCLI.create(
            session,
            yolo=False,
            mcp_configs=[],
            model_name=None,
            thinking=False,
            agent_file=DEFAULT_AGENT_FILE,
        )
        
        await instance.run_shell()
    
    asyncio.run(_run())


@session_app.command("orphan")
def session_orphan():
    """List orphaned sessions (directories that don't exist)."""
    metadata = load_metadata()
    sessions = metadata.list_all_sessions()
    
    orphaned = [s for s in sessions if s.get("orphaned")]
    
    if not orphaned:
        console.print("[green]No orphaned sessions found.[/green]")
        return
    
    console.print(f"[yellow]Found {len(orphaned)} orphaned sessions:[/yellow]")
    console.print()
    
    table = Table(border_style="wheat4")
    table.add_column("Session ID", style="cyan")
    table.add_column("Original Path", style="magenta")
    table.add_column("Messages", style="green")
    table.add_column("Size", style="blue")
    table.add_column("Updated", style="yellow")
    
    for session in orphaned:
        original_path = session.get("work_dir_path", "Unknown")
        if original_path and len(original_path) > 50:
            original_path = original_path[:47] + "..."
        
        table.add_row(
            session["session_id"],
            original_path or "Unknown",
            str(session.get("message_count", 0)),
            f"{session.get('size', 0) / 1024 / 1024:.2f}MB",
            _format_timestamp(session.get("updated_at", 0))
        )
    
    console.print(table)
    console.print()
    console.print("[dim]Resume any session with: kimi session continue <session-id>[/dim]")


if __name__ == "__main__":
    session_app()
