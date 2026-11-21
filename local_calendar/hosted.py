"""Unattended refresh pass for every hosted tenant.

Designed for a systemd timer: run frequently, do work only when each source's
saved clock says it is due, and never perform the first paid Instagram poll
without the user's explicit refresh from the UI.
"""

from __future__ import annotations

import json

from . import config, db, discovery, notifications, runner, scheduler, tenancy


def refresh_tenant(tenant: tenancy.TenantPaths) -> dict:
    result = {
        "tenant_id": tenant.id,
        "email": tenant.email,
        "state": "idle",
        "message": "nothing due",
    }
    if not tenant.db_path.exists():
        result["message"] = "no calendar database yet"
        return result

    lock = None
    with tenancy.activate(tenant):
        try:
            lock = tenancy.acquire_refresh_lock(tenant)
        except tenancy.RefreshBusy:
            result.update(state="busy", message="refresh already running")
            return result

        try:
            cfg = config.load()
            with db.session(tenant.db_path) as conn:
                instagram_due = scheduler.due(
                    conn, cfg["instagram_refresh_hours"]
                )
                venue_ids = scheduler.due_venue_source_ids(
                    conn, cfg["venue_refresh_hours"]
                )
                performer_ids = scheduler.due_performer_source_ids(
                    conn, cfg["performer_refresh_hours"]
                )
                handles = discovery.approved_handles(conn) if instagram_due else []

            website_ids = venue_ids + performer_ids
            if not handles and not website_ids:
                return result

            apify_token = config.secret("APIFY_TOKEN")
            deepseek_key = config.secret("DEEPSEEK_API_KEY")
            if handles and not apify_token:
                result.update(state="error", message="APIFY_TOKEN is missing")
                return result
            if handles and not deepseek_key:
                result.update(state="error", message="DeepSeek key is missing")
                return result

            source = extractor = None
            if handles:
                from .sources import ApifySource
                source = ApifySource(apify_token)
            if handles or (website_ids and deepseek_key):
                from .extract import Extractor
                extractor = Extractor(api_key=deepseek_key)

            with db.session(tenant.db_path) as conn:
                groups = runner.fetch_windows(conn, handles)
                stats = runner.poll(
                    conn, source, extractor, handles, groups=groups,
                    website_source_ids=website_ids,
                )
                delivered = notifications.deliver_pending(conn)
            result.update(
                state="done", message="refresh complete", stats=stats,
                delivered=delivered,
            )
            return result
        except Exception as exc:
            result.update(state="error", message=f"{type(exc).__name__}: {exc}")
            return result
        finally:
            tenancy.release_refresh_lock(lock)


def refresh_all() -> list[dict]:
    return [refresh_tenant(tenant) for tenant in tenancy.listing()]


def main() -> None:
    results = refresh_all()
    print(json.dumps(results, indent=2, default=str))
    if any(result["state"] == "error" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
