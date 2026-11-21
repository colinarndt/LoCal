"""The owner importer copies a consistent snapshot and refuses overwrites."""

import json

import pytest

from local_calendar import auth, db, hosted_import, tenancy


def owner_tenant(tmp_path):
    return tenancy.resolve(
        auth.Identity("owner-sub", "owner@example.com", "cloudflare"),
        {
            tenancy.HOSTED_ROOT_ENV: str(tmp_path / "hosted"),
            tenancy.OWNER_EMAIL_ENV: "owner@example.com",
        },
    )


def source_install(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    with db.session(root / "calendar.db") as conn:
        conn.execute(
            "INSERT INTO source_post "
            "(post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('p1','venue','2099-01-01','now')"
        )
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,created_at) "
            "VALUES ('p1','Imported show','2099-01-01','now')"
        )
    (root / "media").mkdir()
    (root / "media" / "flyer.jpg").write_bytes(b"image")
    (root / "avatars").mkdir()
    (root / "avatars" / "venue.jpg").write_bytes(b"avatar")
    (root / "config.json").write_text(json.dumps({"city": "Charlotte, NC"}))
    return root


def test_import_owner_copies_database_media_and_config(tmp_path):
    owner = owner_tenant(tmp_path)
    source = source_install(tmp_path)

    result = hosted_import.import_owner(source, owner)

    with db.read_session(owner.db_path) as conn:
        assert conn.execute("SELECT title FROM event").fetchone()[0] == "Imported show"
    assert (owner.media_dir / "flyer.jpg").read_bytes() == b"image"
    assert (owner.avatar_dir / "venue.jpg").read_bytes() == b"avatar"
    assert json.loads(owner.config_path.read_text())["city"] == "Charlotte, NC"
    assert result["media_files"] == 1
    assert result["avatar_files"] == 1


def test_import_refuses_to_replace_existing_user_data(tmp_path):
    owner = owner_tenant(tmp_path)
    source = source_install(tmp_path)
    with db.session(owner.db_path) as conn:
        conn.execute(
            "INSERT INTO source_post "
            "(post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('existing','venue','2099-01-01','now')"
        )

    with pytest.raises(hosted_import.ImportRefused, match="already contains"):
        hosted_import.import_owner(source, owner)


def test_import_does_not_copy_provider_secrets(tmp_path):
    owner = owner_tenant(tmp_path)
    source = source_install(tmp_path)
    (source / ".env").write_text("DEEPSEEK_API_KEY=local-secret\n")

    hosted_import.import_owner(source, owner)

    assert not owner.env_path.exists()
