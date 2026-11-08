"""Command group implementations.

One file per group, mirroring the MX2 grouping.
A file here holds only the logic that "turns arguments into results"; the
output format is decided by `cli/`.

## Only command groups belong here

"Building blocks needed by both the kernel layer and the command layer" do not
go here, they go into the kernel:
`invoke.py` (send commands), `fullconfig.py` (full configuration snapshot),
`write.py` (save / reload / read back).
Keeping them here would make the kernel depend on `cmds`, and the layering
check rejects that.
"""

from __future__ import annotations
