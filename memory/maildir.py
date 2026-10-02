"""maildir.py -- the one rule for writing into the mail root (LAYOUT rule 3; phase C, 2026-10-01).

Stdlib only. The memory server loads it from beside server.py (the way it loads heads/score.py), and
scripts/inflation-gate.py loads it under the system python without the server's venv, so the rule
has one copy.

Shape: <root>/<mailbox>/inbox/{tmp,new,cur}/. Every directory from <root>/<mailbox> down is 0750
with group amq-poll: the orchestrator lists names, which carry the sender and the time, and never
reads a body. Every message is 0640 with group amq-read. The group is set on the open descriptor
before the tmp/ -> new/ rename, so a poller never sees a message without it. The writing user must
be in both groups (a non-root process can only chgrp to groups it is in): amq-poll for the
directories it creates, amq-read for the files.

When a group is missing, or this process is not in it (an install that skipped install.sh stage 2),
delivery still works owner-only. problems() says so, and the caller prints it once at start: the
server must announce a degraded mail root, never run on one silently.
"""
import grp
import os

POLL_GROUP, READ_GROUP = "amq-poll", "amq-read"
DIR_MODE, FILE_MODE = 0o750, 0o640


def _gid(name):
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


POLL_GID, READ_GID = _gid(POLL_GROUP), _gid(READ_GROUP)
_MINE = set(os.getgroups()) | {os.getegid()}
# A group this process can set: present on the host and one of ours (root can set any).
_SET_POLL = POLL_GID if POLL_GID is not None and (os.geteuid() == 0 or POLL_GID in _MINE) else None
_SET_READ = READ_GID if READ_GID is not None and (os.geteuid() == 0 or READ_GID in _MINE) else None


def problems():
    """Why delivery would be owner-only: empty when both groups exist and this process is in both."""
    out = []
    for name, gid, can in ((POLL_GROUP, POLL_GID, _SET_POLL), (READ_GROUP, READ_GID, _SET_READ)):
        if gid is None:
            out.append(f"group {name} does not exist")
        elif can is None:
            out.append(f"this process (uid {os.geteuid()}) is not in group {name}")
    return out


def _dir(path):
    """Make the directory if absent, and correct its group and mode only when they differ: a
    correct directory owned by someone else (an old box before the sweep) is left alone."""
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    st = os.stat(path)
    if _SET_POLL is not None and st.st_gid != _SET_POLL:
        os.chown(path, -1, _SET_POLL)
    if st.st_mode & 0o7777 != DIR_MODE:
        os.chmod(path, DIR_MODE)


def ensure_box(root, mailbox, subdirs=("tmp", "new", "cur")):
    """Create <root>/<mailbox>/inbox and the named subdirectories under the directory rule.
    Returns the inbox path. The root itself is created by the installer, never here."""
    box = os.path.join(root, mailbox)
    inbox = os.path.join(box, "inbox")
    for d in (box, inbox) + tuple(os.path.join(inbox, s) for s in subdirs):
        _dir(d)
    return inbox


def deliver(root, mailbox, msg_id, content):
    """Write one message: tmp/ with the file rule, fsync, group set, then rename into new/.
    Returns the path in new/. On failure the tmp file is removed and the error raised."""
    inbox = ensure_box(root, mailbox)
    name = f"{msg_id}.md"
    tmp_path, new_path = os.path.join(inbox, "tmp", name), os.path.join(inbox, "new", name)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            if _SET_READ is not None:
                os.fchown(fd, -1, _SET_READ)
            os.fchmod(fd, FILE_MODE)
        finally:
            os.close(fd)
        os.rename(tmp_path, new_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return new_path


def mark_read(root, mailbox, filename):
    """Move a message from new/ to cur/ (cur/ created under the directory rule). The file keeps its
    group and mode across the rename. Returns the path in cur/."""
    inbox = ensure_box(root, mailbox, ("cur",))
    cur_path = os.path.join(inbox, "cur", filename)
    os.rename(os.path.join(inbox, "new", filename), cur_path)
    return cur_path
