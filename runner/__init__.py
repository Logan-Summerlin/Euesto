"""The runner: a standard-library command server for the sandboxed workbench container.

It runs one Bash command at a time in the shared staging volume, captures bounded output plus a
full log, and kills the command's processes when it ends. It holds no credentials, never sees
``/source`` or the executor's state, and trusts nothing it is sent beyond basic shape checks:
the executor validates every argument and owns checkpoints, status, and publication.
"""
