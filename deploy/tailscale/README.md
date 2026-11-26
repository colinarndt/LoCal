# Single-user server over Tailscale

Runs LoCal headless on an always-on Linux box and serves it to your own
devices with `tailscale serve` (HTTPS on `https://<host>.<tailnet>.ts.net`,
tailnet-only, no open ports). Replaces the Mac app: do not run both.

`refresh-due` is the headless counterpart of the Mac app's scheduler. The
timer runs it every ten minutes; it polls only sources whose interval from
/settings has elapsed, shares the web UI's refresh lock, and never makes the
first paid Instagram poll on its own.

## Install

```bash
sudo useradd --system --home-dir /srv/local-calendar --shell /sbin/nologin local-calendar
sudo mkdir -p /opt/local-calendar /srv/local-calendar /etc/local-calendar
sudo chown "$USER": /opt/local-calendar
git clone <repo> /opt/local-calendar && cd /opt/local-calendar

# uv-managed Python, independent of the distro's
export UV_PYTHON_INSTALL_DIR=/opt/local-calendar/.python UV_PYTHON_PREFERENCE=only-managed
uv python install 3.14
uv venv --python 3.14 && uv pip install -e ".[browser]"
PLAYWRIGHT_BROWSERS_PATH=/opt/local-calendar/ms-playwright .venv/bin/python -m playwright install chromium

sudo cp deploy/tailscale/local.env.example /etc/local-calendar/local.env
sudo cp deploy/tailscale/local-calendar-*.service deploy/tailscale/local-calendar-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now local-calendar-web local-calendar-refresh.timer
sudo tailscale serve --bg 8730
```

Copy an existing calendar (quit the Mac app first) into `/srv/local-calendar`,
then `sudo chown -R local-calendar: /srv/local-calendar` and
`sudo chmod 600 /srv/local-calendar/.env`.

## Performer alerts (ntfy)

Set `NTFY_URL` (a topic URL) and `NTFY_TOKEN` in `/etc/local-calendar/local.env`
and restart the web service. Alerts are published by both the web refresh and
`refresh-due`; one that ntfy rejects stays queued and is retried next pass.
A write-only ntfy user keeps the token from reading or deleting anything:

```bash
sudo podman exec ntfy ntfy user add local-calendar
sudo podman exec ntfy ntfy access local-calendar optiplex write-only
sudo podman exec ntfy ntfy token add local-calendar
```

## Update

```bash
cd /opt/local-calendar && git pull && uv pip install -e ".[browser]" \
  && sudo systemctl restart local-calendar-web
```

## Check

```bash
systemctl list-timers local-calendar-refresh.timer
journalctl -u local-calendar-refresh -n 50     # JSON result of each pass
journalctl -u local-calendar-web -f
```

## Caveats

- Behind `tailscale serve` every request arrives from loopback, so the
  loopback-only DeepSeek key field is editable from any tailnet device.
- Phone `.ics` subscriptions refresh only while Tailscale is connected.
