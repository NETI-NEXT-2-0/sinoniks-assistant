import os
import sys
import tarfile
from pathlib import Path

root = Path("/data")
archive = Path("/backup") / (sys.argv[2] + ".tar")
if sys.argv[1] == "save":
    with tarfile.open(archive, "w") as output:
        output.add(root, arcname=".")
elif sys.argv[1] == "restore":
    with tarfile.open(archive) as source:
        source.extractall(root, filter="data")
        for member in source.getmembers():
            target = root / member.name
            if not target.resolve().is_relative_to(root):
                raise RuntimeError("Unexpected archive path")
            os.chown(target, member.uid, member.gid, follow_symlinks=False)
else:
    raise SystemExit("Unknown transfer operation")
