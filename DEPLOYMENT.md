# Server Deployment Notes

This project can run on a server, but do not expose Futu OpenD directly.

## Recommended Layout

```text
OpenD                 127.0.0.1:11111 only
futu-paper-ai web     127.0.0.1:8787
nginx / caddy         public HTTPS with auth
ai-loop service       background systemd service
```

## Rules

- Keep OpenD bound to `127.0.0.1`.
- Do not expose port `11111` to the public internet.
- Put the web console behind HTTPS and authentication.
- Store `.env` on the server with `chmod 600`.
- Rotate the Gemini key before long-term deployment.
- Keep `GEMINI_EXECUTE_MARKETS=US` until HK buying power is confirmed.
- Keep this as paper trading only unless you deliberately redesign the system.

## Example Commands

```bash
cd /opt/futu-paper-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m futu_paper_ai doctor
python -m futu_paper_ai web --host 127.0.0.1 --port 8787
```

Run the automation loop:

```bash
python -m futu_paper_ai ai-loop --execute
```

## systemd Example

`/etc/systemd/system/futu-paper-ai-loop.service`

```ini
[Unit]
Description=Futu Paper AI Gemini Loop
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/futu-paper-ai
ExecStart=/opt/futu-paper-ai/.venv/bin/python -m futu_paper_ai ai-loop --execute
Restart=always
RestartSec=10
User=futuai

[Install]
WantedBy=multi-user.target
```

Start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now futu-paper-ai-loop
```
