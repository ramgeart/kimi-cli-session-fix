"""Session management meta commands for Kimi CLI."""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from kimi_cli.metadata import load_metadata, save_metadata
from kimi_cli.session import Session
from kimi_cli.ui.shell.console import console
from kimi_cli.ui.shell.metacmd import meta_command
from kimi_cli.ui.shell.prompt import toast

# TYPE_CHECKING
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kimi_cli.ui.shell import Shell


def _format_timestamp(timestamp: float) -> str:
    """Format timestamp for display."""
    if timestamp == 0:
        return "N/A"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except:
        return str(timestamp)


def _truncate_text(text: str, max_length: int) -> str:
    """Truncate text to max length."""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."


@meta_command(name="session", aliases=["sessions"])
async def session_cmd(app: Shell, args: list[str]):
    """Session management commands.
    
    Usage:
      /session list              - List all sessions
      /session continue <id>     - Resume a specific session by ID
      /session clone <id>        - Create a new session from an existing one
      /session orphan            - Show orphaned sessions (directories that don't exist)
    """
    if not args:
        console.print(
            Panel(
                "Session management commands:\n\n"
                "• /session list              - List all sessions\n"
                "• /session continue <id>     - Resume a specific session by ID\n"
                "• /session clone <id>        - Create a new session from an existing one\n"
                "• /session orphan            - Show orphaned sessions\n",
                title="Session Commands",
                border_style="wheat4"
            )
        )
        return
    
    subcommand = args[0].lower()
    
    if subcommand == "list":
        await _session_list(app, args[1:])
    elif subcommand == "continue":
        await _session_continue(app, args[1:])
    elif subcommand == "clone":
        await _session_clone(app, args[1:])
    elif subcommand == "orphan":
        await _session_orphan(app, args[1:])
    else:
        console.print(f"[red]Unknown session command: {subcommand}[/red]")
        console.print("Use '/session' to see available commands.")


async def _session_list(app: Shell, args: list[str]) -> None:
    """List all sessions."""
    try:
        from kaos.path import KaosPath
        
        metadata = load_metadata()
        sessions = metadata.list_all_sessions()
        
        if not sessions:
            console.print("[yellow]No sessions found.[/yellow]")
            return
        
        # Create table
        table = Table(title="All Sessions", border_style="wheat4")
        table.add_column("ID", style="cyan", no_wrap=True, max_width=36)
        table.add_column("Work Dir", style="magenta", max_width=40)
        table.add_column("Messages", justify="right", style="green")
        table.add_column("Size", justify="right", style="blue")
        table.add_column("Last Updated", style="yellow")
        table.add_column("Status", style="red")
        
        for session in sessions:
            work_dir_path = session.get("work_dir_path", "N/A")
            if work_dir_path and len(work_dir_path) > 40:
                work_dir_path = "..." + work_dir_path[-37:]
            
            status = "🗑️  Orphaned" if session.get("orphaned") else "✓ Active"
            if session.get("orphaned") and work_dir_path and work_dir_path != "N/A":
                # Try to find if directory exists elsewhere
                if Path(work_dir_path).exists():
                    status = "⚠️  Path exists but not tracked"
            
            table.add_row(
                session["session_id"],
                work_dir_path or "Unknown",
                str(session.get("message_count", 0)),
                f"{session.get('size', 0) / 1024:.1f}KB",
                _format_timestamp(session.get("updated_at", 0)),
                status
            )
        
        console.print(table)
        console.print(f"\n[green]Total: {len(sessions)} sessions[/green]")
        
        orphaned_count = sum(1 for s in sessions if s.get("orphaned"))
        if orphaned_count > 0:
            console.print(f"\n[yellow]Orphaned: {orphaned_count} sessions[/yellow]")
            console.print("[dim]Tip: Use '/session continue <id>' to resume any session[/dim]")
    except Exception as e:
        console.print(f"[red]Error listing sessions: {e}[/red]")
        logger.exception("Error in _session_list")


async def _session_continue(app: Shell, args: list[str]) -> None:
    """Resume a specific session by ID."""
    if not args:
        console.print("[red]Error: Session ID required[/red]")
        console.print("Usage: /session continue <session-id>")
        console.print("\nUse '/session list' to see available sessions.")
        return
    
    session_id = args[0]
    
    # Validate UUID format
    try:
        uuid.UUID(session_id)
    except ValueError:
        console.print(f"[red]Error: Invalid session ID format[/red]")
        console.print("Session ID should be a UUID like: 123e4567-e89b-12d3-a456-426614174000")
        return
    
    metadata = load_metadata()
    
    # Find the session
    target_session = None
    target_work_dir = None
    target_work_dir_id = None
    
    # First, check registered work directories
    for wd in metadata.work_dirs:
        session_dir = wd.sessions_dir / session_id
        context_file = session_dir / "context.jsonl"
        if context_file.exists():
            target_session = session_id
            target_work_dir = wd
            target_work_dir_id = wd.id
            break
    
    # If not found, check orphaned sessions
    if not target_session:
        share_dir = Path.home() / ".kimi"
        sessions_base = share_dir / "sessions"
        if sessions_base.exists():
            for work_dir_dir in sessions_base.iterdir():
                if not work_dir_dir.is_dir():
                    continue
                session_dir = work_dir_dir / session_id
                context_file = session_dir / "context.jsonl"
                if context_file.exists():
                    target_session = session_id
                    target_work_dir_id = work_dir_dir.name
                    
                    # Create new work directory metadata for current path
                    from kaos.path import KaosPath
                    current_path = KaosPath(str(Path.cwd().resolve()))
                    
                    # Check if we can reuse existing work_dir metadata
                    for wd in metadata.work_dirs:
                        if wd.id == work_dir_dir.name:
                            target_work_dir = wd
                            wd.path = str(Path.cwd().resolve())
                            break
                    
                    if not target_work_dir:
                        # Create new metadata reusing the old work_dir ID
                        target_work_dir = metadata.new_work_dir_meta(current_path, work_dir_dir.name)
                    
                    break
    
    if not target_session:
        console.print(f"[red]Error: Session '{session_id}' not found[/red]")
        console.print("\nUse '/session list' to see available sessions.")
        return
    
    # Update the work directory's last session if we have one
    if target_work_dir:
        target_work_dir.last_session_id = session_id
        save_metadata(metadata)
        console.print(f"[green]✓[/green] Found session: {session_id}")
        console.print(f"  Work directory: {target_work_dir.path}")
    else:
        console.print(f"[green]✓[/green] Found orphaned session: {session_id}")
    
    # Show some info about the session
    session_dir = Path.home() / ".kimi" / "sessions" / target_work_dir_id / session_id
    context_file = session_dir / "context.jsonl"
    
    try:
        message_count = 0
        first_message = ""
        if context_file.exists():
            with open(context_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if '"role":"user"' in line:
                        message_count += 1
                        if not first_message and '"content"' in line:
                            try:
                                import json
                                msg = json.loads(line)
                                content = msg.get('content', '')
                                if isinstance(content, str):
                                    first_message = content[:60]
                                elif isinstance(content, list):
                                    first_message = str(content)[:60]
                            except:
                                pass
        
        if message_count > 0:
            console.print(f"  Messages: {message_count}")
            if first_message:
                console.print(f"  Preview: {_truncate_text(first_message, 50)}")
    except Exception as e:
        logger.debug("Could not read session preview: {e}", e=e)
    
    console.print("\n[green]Resuming session...[/green]")
    
    # Trigger session reload
    from kimi_cli.cli import Reload
    raise Reload()


async def _session_clone(app: Shell, args: list[str]) -> None:
    """Create a new session from an existing one."""
    if not args:
        console.print("[red]Error: Session ID required[/red]")
        console.print("Usage: /session clone <session-id>")
        return
    
    session_id = args[0]
    
    # Validate UUID format
    try:
        uuid.UUID(session_id)
    except ValueError:
        console.print(f"[red]Error: Invalid session ID format[/red]")
        return
    
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
        console.print("Use '/session list' to see available sessions.")
        return
    
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
    import shutil
    new_session_id = str(uuid.uuid4())
    new_session_dir = current_work_dir.sessions_dir / new_session_id
    new_session_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy context file (this copies the entire history)
    new_context_file = new_session_dir / "context.jsonl"
    shutil.copy2(source_context_file, new_context_file)
    
    # Update work directory's last session
    current_work_dir.last_session_id = new_session_id
    save_metadata(metadata)
    
    console.print(f"[green]✓[/green] Created new session: {new_session_id}")
    console.print(f"  Cloned from: {session_id}")
    console.print(f"  Location: {new_session_dir}")
    
    # Show session info
    try:
        if new_context_file.exists():
            message_count = sum(1 for _ in open(new_context_file))
            console.print(f"  Messages copied: {message_count}")
    except:
        pass
    
    console.print("\n[green]Starting cloned session...[/green]")
    
    # Reload to start with new session
    from kimi_cli.cli import Reload
    raise Reload()


async def _session_orphan(app: Shell, args: list[str]) -> None:
    """Show orphaned sessions (directories that don't exist)."""
    try:
        metadata = load_metadata()
        sessions = metadata.list_all_sessions()
        
        orphaned = [s for s in sessions if s.get("orphaned")]
        
        if not orphaned:
            console.print("[green]No orphaned sessions found.[/green]")
            return
        
        console.print(f"[yellow]Found {len(orphaned)} orphaned sessions:[/yellow]")
        console.print()
        
        table = Table(border_style="wheat4")
        table.add_column("Session ID", style="cyan", no_wrap=True)
        table.add_column("Original Path", style="magenta")
        table.add_column("Messages", style="green")
        table.add_column("Size", style="blue")
        table.add_column("Last Update", style="yellow")
        
        for session in orphaned:
            original_path = session.get("work_dir_path", "Unknown")
            if original_path and len(original_path) > 60:
                original_path = original_path[:57] + "..."
            
            table.add_row(
                session["session_id"],
                original_path or "Unknown",
                str(session.get("message_count", 0)),
                f"{session.get('size', 0) / 1024:.1f}KB",
                _format_timestamp(session.get("updated_at", 0))
            )
        
        console.print(table)
        console.print()
        console.print("[dim]You can resume any session with: /session continue <session-id>[/dim]")
    except Exception as e:
        console.print(f"[red]Error finding orphaned sessions: {e}[/red]")
        logger.exception("Error in _session_orphan")
