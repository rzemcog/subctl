import os
import stat

from subctl.atomic import atomic_write_text


def test_atomic_write_preserves_existing_owner_group_and_mode(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o640)
    original = target.stat()
    chown_calls = []

    def record_chown(path, uid, gid):
        chown_calls.append((path, uid, gid))

    monkeypatch.setattr(os, "chown", record_chown)

    atomic_write_text(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert len(chown_calls) == 1
    temporary_path, uid, gid = chown_calls[0]
    assert temporary_path.parent == target.parent
    assert uid == original.st_uid
    assert gid == original.st_gid
