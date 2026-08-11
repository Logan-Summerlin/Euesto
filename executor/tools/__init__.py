"""Network-disabled, staging-only workspace executor tools.

Filesystem lookup: use ``list_files`` for directory structure, filename discovery, and existence
checks. Use ``max_depth`` for shallow structural queries and ``max_results`` when only a bounded
number of entries is needed. Limits and filters constrain traversal where practical; results may
be truncated, so check ``truncated``/``has_more`` and use the cursor to continue. Use
``search_text`` when you need to search file contents. ``read_file`` reads by line range or byte
range, never both; ``search_text`` path filters restrict which files are searched.
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
