"""
Mejoras al sistema de sesiones de kimi-cli:

Problemas arreglados:
1. Las sesiones se pierden si se borra/mueve el directorio
2. No hay forma de listar todas las sesiones
3. No hay forma de reasignar sesiones huérfanas
4. El path es la única clave, no hay identificador persistente

Soluciones implementadas:
1. Agregar ID único persistente para cada work directory
2. Soportar aliases de paths (para directorios movidos)
3. Detección de sesiones huérfanas
4. Comando para listar y reasignar sesiones
5. Backward compatibility con metadatos antiguos
"""

from __future__ import annotations

import json
import os
import uuid
from hashlib import md5
from pathlib import Path

from kaos import get_current_kaos
from kaos.local import local_kaos
from kaos.path import KaosPath
from pydantic import BaseModel, Field

from kimi_cli.share import get_share_dir
from kimi_cli.utils.logging import logger


def get_metadata_file() -> Path:
    return get_share_dir() / "kimi.json"


class WorkDirMeta(BaseModel):
    """Metadata for a work directory."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    """Unique identifier for this work directory (persistent across moves)."""
    
    path: str
    """The full path of the work directory."""
    
    path_aliases: list[str] = Field(default_factory=list)
    """Alternative paths (for moved directories, symlinks, etc)."""

    kaos: str = local_kaos.name
    """The name of the KAOS where the work directory is located."""

    last_session_id: str | None = None
    """Last session ID of this work directory."""
    
    created_at: float = Field(default_factory=lambda: os.times().system)
    """When this work directory was first registered."""

    @property
    def sessions_dir(self) -> Path:
        """The directory to store sessions for this work directory.
        
        Uses the work_dir ID instead of path hash for persistence across moves.
        """
        # Use the persistent ID instead of path hash
        session_dir = get_share_dir() / "sessions" / self.id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir
    
    def matches_path(self, path: str | KaosPath) -> bool:
        """Check if the given path matches this work directory.
        
        Matches against:
        - Current path
        - Path aliases (for moved directories)
        - Real path (resolves symlinks)
        """
        path_str = str(path)
        
        # Check main path
        if self.path == path_str:
            return True
        
        # Check aliases
        if path_str in self.path_aliases:
            return True
        
        # Check resolved path (handles symlinks)
        try:
            resolved = Path(path_str).resolve()
            if str(resolved) == self.path:
                return True
            if str(resolved) in self.path_aliases:
                return True
        except OSError:
            pass
        
        return False
    
    def add_alias(self, path: str | KaosPath) -> None:
        """Add a path alias for this work directory."""
        path_str = str(path)
        if path_str != self.path and path_str not in self.path_aliases:
            self.path_aliases.append(path_str)
            logger.info("Added path alias for work directory {id}: {path}", id=self.id, path=path_str)


class Metadata(BaseModel):
    """Kimi metadata structure."""

    work_dirs: list[WorkDirMeta] = Field(default_factory=list[WorkDirMeta])
    """Work directory list."""

    thinking: bool = False
    """Whether the last session was in thinking mode."""

    def get_work_dir_meta(self, path: KaosPath) -> WorkDirMeta | None:
        """Get the metadata for a work directory.
        
        Matches against current path, aliases, and resolved paths.
        If directory exists but no metadata found, check for "orphaned" sessions.
        """
        path_str = str(path)
        
        # 1. Try direct match against path and aliases
        for wd in self.work_dirs:
            if wd.matches_path(path):
                # Update path if it changed (new canonical location)
                if wd.path != path_str:
                    wd.path = path_str
                return wd
        
        # 2. Check for orphaned sessions (directory exists in sessions/ but path doesn't match)
        share_dir = get_share_dir()
        sessions_base = share_dir / "sessions"
        
        if sessions_base.exists():
            # Look for sessions that contain this path in their context
            for context_file in sessions_base.rglob("context.jsonl"):
                try:
                    with open(context_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if path_str in content or str(Path(path_str).resolve()) in content:
                            session_id = context_file.parent.name
                            work_dir_id = context_file.parent.parent.name
                            logger.info(
                                "Found orphaned session for path: {path}",
                                path=path_str
                            )
                            logger.info(
                                "Session ID: {session_id}, Work Dir ID: {work_dir_id}",
                                session_id=session_id,
                                work_dir_id=work_dir_id
                            )
                            
                            # Find or recreate metadata for this work directory ID
                            for wd in self.work_dirs:
                                if wd.id == work_dir_id:
                                    logger.info("Reusing existing work directory metadata")
                                    wd.add_alias(path_str)
                                    return wd
                            
                            # Create new metadata with the old ID
                            logger.warning("Creating new work directory with existing ID")
                            return self.new_work_dir_meta(path, work_dir_id)
                except:
                    continue
        
        return None

    def new_work_dir_meta(self, path: KaosPath, work_dir_id: str | None = None) -> WorkDirMeta:
        """Create a new work directory metadata.
        
        If work_dir_id is provided, tries to reuse existing metadata for that ID
        (useful for reassigning orphaned sessions).
        """
        if work_dir_id:
            # Try to find existing metadata with this ID
            for wd in self.work_dirs:
                if wd.id == work_dir_id:
                    logger.info("Reusing existing work directory metadata: {id}", id=work_dir_id)
                    wd.path = str(path)  # Update to new path
                    return wd
        
        # Create new metadata
        wd_meta = WorkDirMeta(path=str(path), kaos=get_current_kaos().name)
        self.work_dirs.append(wd_meta)
        logger.info("Created new work directory metadata: {id} for {path}", 
                   id=wd_meta.id, path=path)
        return wd_meta
    
    def list_all_sessions(self) -> list[dict]:
        """List all sessions across all work directories (including orphaned)."""
        all_sessions = []
        
        # Get sessions from registered work directories
        for wd in self.work_dirs:
            if wd.sessions_dir.exists():
                for session_dir in wd.sessions_dir.iterdir():
                    if session_dir.is_dir():
                        context_file = session_dir / "context.jsonl"
                        if context_file.exists():
                            try:
                                # Count messages for better UX
                                message_count = sum(1 for _ in open(context_file))
                            except:
                                message_count = 0
                            
                            all_sessions.append({
                                "work_dir_id": wd.id,
                                "work_dir_path": wd.path,
                                "session_id": session_dir.name,
                                "orphaned": not Path(wd.path).exists(),
                                "size": context_file.stat().st_size,
                                "updated_at": context_file.stat().st_mtime,
                                "message_count": message_count
                            })
        
        # Look for orphaned sessions (directories not in any work_dir.sessions_dir)
        share_dir = get_share_dir()
        sessions_base = share_dir / "sessions"
        
        if sessions_base.exists():
            for work_dir_dir in sessions_base.iterdir():
                if work_dir_dir.is_dir() and not any(wd.id == work_dir_dir.name for wd in self.work_dirs):
                    # This is an orphaned work directory
                    for session_dir in work_dir_dir.iterdir():
                        if session_dir.is_dir():
                            context_file = session_dir / "context.jsonl"
                            if context_file.exists():
                                try:
                                    message_count = sum(1 for _ in open(context_file))
                                except:
                                    message_count = 0
                                
                                all_sessions.append({
                                    "work_dir_id": work_dir_dir.name,
                                    "work_dir_path": None,
                                    "session_id": session_dir.name,
                                    "orphaned": True,
                                    "size": context_file.stat().st_size,
                                    "updated_at": context_file.stat().st_mtime,
                                    "message_count": message_count
                                })
        
        return sorted(all_sessions, key=lambda x: x["updated_at"], reverse=True)


def load_metadata() -> Metadata:
    """Load metadata from file, with backward compatibility."""
    metadata_file = get_metadata_file()
    logger.debug("Loading metadata from file: {file}", file=metadata_file)
    if not metadata_file.exists():
        logger.debug("No metadata file found, creating empty metadata")
        return Metadata()
    
    with open(metadata_file, encoding="utf-8") as f:
        data = json.load(f)
        
    metadata = Metadata(**data)
    
    # Generate IDs for old entries that don't have them (backward compatibility)
    for wd in metadata.work_dirs:
        if not hasattr(wd, 'id') or not wd.id:
            logger.info("Generating ID for legacy work directory: {path}", path=wd.path)
            wd.id = str(uuid.uuid4())
    
    return metadata


def save_metadata(metadata: Metadata):
    """Save metadata to file."""
    metadata_file = get_metadata_file()
    logger.debug("Saving metadata to file: {file}", file=metadata_file)
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata.model_dump(), f, indent=2, ensure_ascii=False)
