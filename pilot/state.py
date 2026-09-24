"""Single-host durable-state ownership. Never use on ephemeral or shared NFS disks."""
import os
from pathlib import Path
import shutil


class StateOwner:
    def __init__(self, directory):
        self.file = (Path(directory)/'owner.lock').open('a+b')
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError('state_already_owned') from None

    def close(self):
        self.file.close()


def clean_scratch(directory):
    """Only the service-owned scratch root; never follow links into caller data.

    Caller must hold StateOwner and have no active worker. OS worker containment
    is responsible for terminating descendants before releasing ownership.
    """
    root = Path(directory)
    if root.is_symlink() or root.is_junction():
        raise ValueError('scratch_root_must_not_be_link')
    root = root.resolve(strict=True)
    for path in root.iterdir():
        if path.is_symlink():
            path.unlink()
        elif path.is_junction():
            path.rmdir()
        elif path.is_dir():
            if not path.resolve().is_relative_to(root):
                raise ValueError('scratch_scope_violation')
            shutil.rmtree(path)
        else:
            path.unlink()
    return not any(root.iterdir())
