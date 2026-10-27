# Setup guide

From an empty server to a working EDITH, step by step. Every command is meant for a Debian/Ubuntu server;
on other distributions only the package installation changes.

## 1. The server

Any small Linux machine that stays on: a mini PC or a Raspberry Pi 4/5. 4 GB of RAM is enough, 8 is better.

Install Docker (with the Compose plugin) and let your user run it:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER        # then log out and back in
docker compose version
```

Python 3 is already there on Debian/Ubuntu: the setup wizard needs nothing else.

## 2. Tailscale

Tailscale is how your phone and PC reach EDITH without opening any port to the internet.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then, in the Tailscale admin console → **DNS**: enable **MagicDNS** and **HTTPS Certificates** (needed for the web app).
Install Tailscale on your phone and PC too, with the same account.

## 3. Nextcloud

Needed for the calendar, tasks, notes, memory and news modules. Use your own or an existing one.

- Install the apps you need: **Tasks** (for the `tasks` module) and **News** (for the `news` module, then add some feeds,
  ideally grouped in folders such as Sport, Tech, World). Calendar comes with Nextcloud. Notes is optional.
- Create an **app password**: Settings → Security → Devices & sessions → *Create new app password*.
  EDITH uses this, never your real password.

## 4. Telegram and Gemini

- **Telegram:** talk to [@BotFather](https://t.me/BotFather), send `/newbot`, follow the steps and keep the token.
  Then open the chat with your new bot and press **Start** (the wizard uses this to find your chat ID).
- **Gemini:** create an API key in Google AI Studio (there is a free tier).

## 5. Install EDITH

```bash
git clone https://github.com/Aleprantil/edith.git && cd edith
python3 edith-setup
```

The wizard asks only what is needed for the modules you pick, checks every connection and writes `.env`
(readable only by you). Then:

```bash
docker compose up -d --build
docker logs -f edith                 # look for: «EDITH avviato · moduli: …» (Ctrl+C to stop watching)
```

Say «ciao» to your bot on Telegram: it should answer.

## 6. The web app

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8787
tailscale serve status
```

Open `https://<server-name>.<your-tailnet>.ts.net:8443` from your phone (with Tailscale on). On iPhone and Android you can
add it to the home screen: it opens like an app. If it says you are not allowed in, the page shows your Tailscale login:
put exactly that in `WEB_ALLOWED_LOGINS` (run `python3 edith-setup` again) and restart with `docker compose up -d`.

## 7. Optional

- **Disk and SMART checks** (`server` module): `sudo bash monitor/install_monitor.sh` installs a small host probe
  that runs once a minute. Remove it with `--uninstall`.
- **Desktop (Hyprland):** the Waybar widget and the island, see [`desktop/README.md`](../desktop/README.md).

## Updating

```bash
git pull
docker compose up -d --build
```

## If something does not work

| Problem | What to check |
|---|---|
| The bot does not answer | `docker logs edith`; then `python3 edith-setup --check` |
| «Gemini non risponde» | the main model is overloaded: the fallback model takes over; you can change both in `.env` |
| Nextcloud says 401 | use an **app password**, not your real one |
| No news | the News app has no feeds yet, or they are older than `NEWS_MAX_AGE_HOURS` |
| Permission errors on `data/` | the container runs as UID 1000: `sudo chown -R 1000:1000 data` |
| The web app says «non autorizzato» | your Tailscale login is not in `WEB_ALLOWED_LOGINS` |

You do not need to open any port on your router or firewall: everything goes through Tailscale.
