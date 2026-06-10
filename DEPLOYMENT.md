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

## Current EC2 Layout

The initial EC2 deployment uses:

```text
app repo    /home/ec2-user/futu_ai
OpenD       /home/ec2-user/opend/current
web         127.0.0.1:8787
OpenD API   127.0.0.1:11111
```

## autoNews Integration On Server

Do not reuse the Mac path from local `.env`. Deploy autoNews on the server and
let both services share one server-side SQLite file.

Recommended EC2 layout:

```text
futu app      /home/ec2-user/futu_ai
autoNews app  /home/ec2-user/autoNews
signal db     /home/ec2-user/news-data/news.db
```

Set autoNews `config.local.ini`:

```ini
[SETTINGS]
db_path = /home/ec2-user/news-data/news.db
```

Set `/home/ec2-user/futu_ai/.env`:

```bash
AUTONEWS_DB_PATH=/home/ec2-user/news-data/news.db
AUTONEWS_LOOKBACK_HOURS=24
AUTONEWS_MIN_IMPACT=60
AUTONEWS_MAX_SIGNALS=8
```

With this layout there is no code change and no local path migration. autoNews
writes signals into the shared database; `futu-paper-ai` reads the same file and
passes recent high-impact signals into Gemini.

Install or refresh the services:

```bash
sudo cp /home/ec2-user/futu_ai/deploy/futu-opend.service /etc/systemd/system/
sudo cp /home/ec2-user/futu_ai/deploy/futu-paper-ai-web.service /etc/systemd/system/
sudo cp /home/ec2-user/futu_ai/deploy/futu-paper-ai-loop.service /etc/systemd/system/
sudo cp /home/ec2-user/futu_ai/deploy/start-futu-opend.sh /home/ec2-user/opend/start-futu-opend.sh
sudo systemctl daemon-reload
```

Create `/home/ec2-user/opend/opend.env` for OpenD login when you are ready:

```bash
FUTU_OPEND_LOGIN_ACCOUNT=your_futu_id_or_email_or_phone
FUTU_OPEND_LOGIN_PWD_MD5=your_32_char_md5_password_hash
```

Start order:

```bash
sudo systemctl enable --now futu-opend
sudo systemctl enable --now futu-paper-ai-web
sudo systemctl enable --now futu-paper-ai-loop
```

Only start `futu-paper-ai-loop` after `python -m futu_paper_ai doctor` reports OpenD as connected.

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
