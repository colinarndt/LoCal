"""Card images use generated WebP thumbnails while lightboxes keep originals."""

import io

from PIL import Image

from local_calendar import paths, web


def make_image(path, size=(1200, 900)):
    image = Image.new("RGB", size, (180, 70, 40))
    image.save(path, "JPEG", quality=92)


def test_thumbnail_route_resizes_and_caches_original(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME", tmp_path)
    media = tmp_path / "media"
    media.mkdir()
    make_image(media / "flyer.jpg")
    client = web.app.test_client()

    first = client.get("/media/thumbnail/flyer.jpg")
    second = client.get("/media/thumbnail/flyer.jpg")

    assert first.status_code == 200
    assert first.mimetype == "image/webp"
    assert first.data == second.data
    assert "private" in first.headers["Cache-Control"]
    assert "immutable" in first.headers["Cache-Control"]
    with Image.open(io.BytesIO(first.data)) as thumbnail:
        assert thumbnail.format == "WEBP"
        assert max(thumbnail.size) == 320
    assert len(list((tmp_path / "thumbnails").glob("*.webp"))) == 1

    original = client.get("/media/flyer.jpg")
    with Image.open(io.BytesIO(original.data)) as full:
        assert full.size == (1200, 900)


def test_thumbnail_route_rejects_missing_and_invalid_images(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME", tmp_path)
    media = tmp_path / "media"
    media.mkdir()
    (media / "not-an-image.jpg").write_text("nope")
    client = web.app.test_client()

    assert client.get("/media/thumbnail/missing.jpg").status_code == 404
    assert client.get("/media/thumbnail/not-an-image.jpg").status_code == 415


def test_calendar_cards_use_thumbnail_but_lightbox_uses_original(tmp_path, monkeypatch):
    monkeypatch.setitem(web.app.config, "DB", str(tmp_path / "calendar.db"))
    from local_calendar import db

    with db.session(tmp_path / "calendar.db") as conn:
        conn.execute(
            "INSERT INTO source_post "
            "(post_id,polled_handle,posted_at,local_images,fetched_at) "
            "VALUES ('p1','venue','2099-08-01','[\"flyer.jpg\"]','now')"
        )
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,created_at) "
            "VALUES ('p1','Show','2099-08-01','now')"
        )

    page = web.app.test_client().get("/")

    assert b'src="/media/thumbnail/flyer.jpg"' in page.data
    assert b'data-full="/media/flyer.jpg"' in page.data
