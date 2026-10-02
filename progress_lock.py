"""Coordinate progress writes from both Calibre plugins for the same book."""
from contextlib import contextmanager
import os

@contextmanager
def progress_lock(sidecar):
    path = str(sidecar) + '.progress-sync.lock'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0');stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError('Another progress sync is updating this book; try again') from error
            try:
                yield
            finally:
                stream.seek(0);msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError('Another progress sync is updating this book; try again') from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
