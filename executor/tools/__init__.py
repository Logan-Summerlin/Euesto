"""Network-disabled, staging-only workspace executor tools.

Filesystem lookup: use ``list_files`` with an exact directory or glob to find files or check
whether a path exists. Use ``search_text`` when you need to search file contents. ``read_file``
reads by line range or byte range, never both; ``search_text`` path filters restrict which files
are searched; ``list_files`` reports truncation explicitly; invalid ranges are errors rather
than successful empty results.
"""

from ..checkpoints import restore_checkpoint
from .apply_patch import apply_patch
from .file_ops import move_or_copy_file
from .inspect_workspace import inspect_workspace
from .list_files import list_files
from .read_file import MAX_READ_BYTES, read_file
from .run_command import CommandRunner
from .search_text import search_text

__all__ = [
    "MAX_READ_BYTES",
    "CommandRunner",
    "apply_patch",
    "inspect_workspace",
    "list_files",
    "move_or_copy_file",
    "read_file",
    "restore_checkpoint",
    "search_text",
]
