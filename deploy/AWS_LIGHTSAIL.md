# Deploying OpenAlgo to AWS Lightsail (Mumbai)

End-to-end runbook. Everything in Part A must be done by you in your own AWS
account; from Part D onward the GitHub Actions workflow does the work and its
logs are readable from anywhere.

**Why the split:** the assistant working on this repo has no access to your AWS
account and cannot SSH anywhere (port 22 is blocked from its sandbox and there
is no ssh client). It *can* push commits, dispatch the `Deploy` workflow, and
read Actions logs. GitHub's runners have unrestricted SSH, so the runner is the
deploy arm. That is the whole design of `.github/workflows/deploy.yml`.

---

## Part A - Create the server (you, in the AWS console)

Region: **Mumbai (ap-south-1)**. Lowest latency to NSE/BSE and keeps market data
inside India.

1. Lightsail -> **Create instance**
   - Platform: **Linux/Unix**
   - Blueprint: **OS Only -> Ubuntu 24.04 LTS**
   - Plan: **$12/mo** (2 GB RAM, 2 vCPU, 60 GB SSD). The $5/1 GB plan will OOM
     once the master contract loads alongside gunicorn and the WebSocket proxy.
   - Name it `openalgo`.

2. **Networking -> Attach a static IP.** Do this before anything else. A
   Lightsail instance's default public IP changes on stop/start, and your broker
   whitelists a *fixed* IP. A static IP attached to a running instance is free.

3. **Networking -> IPv4 Firewall**: allow `SSH 22`, `HTTP 80`, `HTTPS 443`.
   Do **not** open 5000 or 8765 - nginx fronts both and `setup.sh` keeps them
   loopback-only.

Note the static IP. That is the address you whitelist with your broker.

## Part B - First-boot install (you, once, via the Lightsail browser SSH)

Use the **Connect using SSH** button in the Lightsail console - no key handling
needed for this step.

```bash
curl -fsSL https://raw.githubusercontent.com/kinseyecommerce-alt/openalgo/main/deploy/setup.sh -o setup.sh
less setup.sh          # read it before running anything as root
sudo bash setup.sh --repo https://github.com/kinseyecommerce-alt/openalgo --branch main
```

This installs uv, nginx, ufw, clones to `/opt/openalgo`, and installs the
systemd unit. It deliberately does **not** write your `.env` or start trading.

Then create the `.env` **by hand on the server**:

```bash
cd /opt/openalgo
cp .sample.env .env
~/.local/bin/uv run python -c "import secrets; print(secrets.token_hex(32))"   # run twice
nano .env
```

Set at minimum `APP_KEY`, `API_KEY_PEPPER` (the two generated values),
`BROKER_API_KEY`, `BROKER_API_SECRET`, and
`REDIRECT_URL = 'http://<your-static-ip>/zerodha/callback'` (switch to `https://`
and your domain once TLS is on). Register the same redirect URL in the Kite
developer console.

> **The `.env` never leaves the server.** It is excluded from the rsync in
> `deploy.yml`, it is gitignored, and no credential ever passes through CI or
> through this repo. Do not paste broker credentials into GitHub secrets, into
> a PR, or into a chat.

Start it:

```bash
sudo systemctl start openalgo
sudo systemctl status openalgo
journalctl -u openalgo -f
```

Visit `http://<static-ip>/`. Create your OpenAlgo login, then log in to Zerodha.

## Part C - TLS (recommended before you log in with real credentials)

Point a domain's A record at the static IP, then:

```bash
sudo sed -i 's/server_name _;/server_name your.domain;/' /etc/nginx/sites-available/openalgo
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your.domain
```

Update `REDIRECT_URL` in `.env` to the `https://` form and restart.

## Part D - Wire up automated redeploys

The `Deploy` workflow needs an SSH key and the host. **Generate the key
yourself** - in the Lightsail browser SSH session or AWS CloudShell - so the
private half never appears in a chat transcript or a tool log:

```bash
ssh-keygen -t ed25519 -N '' -f ~/deploy_key -C openalgo-deploy
cat ~/deploy_key.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
cat ~/deploy_key          # copy this whole block, including BEGIN/END lines
shred -u ~/deploy_key     # remove the private key from the server afterwards
```

In GitHub -> repo **Settings -> Secrets and variables -> Actions -> New
repository secret**, add:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | your Lightsail static IP |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_SSH_KEY` | the full private key you just copied |
| `HEALTHCHECK_URL` | optional, e.g. `https://your.domain/` |

Then give the deploy user a passwordless restart of just that one unit, so the
workflow's `systemctl restart` does not hang on a sudo password:

```bash
echo 'ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart openalgo, /bin/systemctl is-active openalgo' \
  | sudo tee /etc/sudoers.d/openalgo-deploy
sudo chmod 440 /etc/sudoers.d/openalgo-deploy
sudo visudo -c
```

### Prerequisite: the workflow must be on `main`

GitHub only registers a `workflow_dispatch` workflow once its file exists on the
**default branch**. While `deploy.yml` lives only on a feature branch, the
Actions API returns `404` for a dispatch. **Merge the PR first**, then Deploy
becomes runnable - including from the API, which is how the assistant triggers
and watches it.

## Part E - Deploy

Actions -> **Deploy** -> **Run workflow**. It builds the frontend on the runner
(so the server needs no Node), rsyncs the app while excluding `.env`, `db/`,
`log/` and your strategy files, runs `uv sync`, restarts the unit, verifies the
service is active, and optionally health-checks the URL. A failed deploy fails
the job loudly and dumps the last 60 journal lines.

## Part F - Prove it takes orders

```bash
export OPENALGO_URL=https://your.domain
export OPENALGO_APIKEY=<from /apikey on the instance>
./deploy/smoke_test.sh
```

It checks reachability and the API key, **refuses to place anything unless
analyzer/sandbox mode is ON**, then places a simulated BUY, verifies the order
and position books, and squares off. Fail-closed: if it cannot read the analyzer
state it aborts rather than risk a live order.

---

## Before live trading

1. **Whitelist the static IP with your broker.** Since April 2026 SEBI requires
   it for transactional API orders; without it the broker rejects orders from
   this server. Whitelist *your server's* IP only.
2. Run a full sandbox session through market hours and read `/performance`.
3. Set the daily capital allocation and enable the capital guard at
   `/settings/broker` - **in sandbox first**, and confirm rejections appear in
   the logs before trusting it live.
4. Only then consider turning analyzer mode off.

## Running costs (Mumbai, approximate)

| Item | Monthly |
| --- | --- |
| Lightsail 2 GB instance | $12 |
| Static IP (attached) | free |
| Data transfer | 3 TB included |
| TLS via Let's Encrypt | free |

Fixed, with no per-request billing to surprise you.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Pages load, nothing streams | nginx must upgrade WebSockets on **both** `/socket.io/` and `/ws` - see `deploy/nginx.conf` |
| Service dead on boot | `journalctl -u openalgo -n 100`; usually a missing `.env` key |
| Broker rejects orders | static IP not whitelisted, or token expired (Indian broker tokens die ~3:00 AM IST daily) |
| Deploy job fails at restart | the sudoers rule in Part D is missing |
| Deploy dispatch returns 404 | `deploy.yml` is not on `main` yet |
| Out of memory | you are on the 1 GB plan; resize to 2 GB |
