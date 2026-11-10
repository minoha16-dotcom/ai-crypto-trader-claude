# Oracle Cloud Always Free deployment

This guide deploys the existing FastAPI dashboard to an OCI Ampere A1 Ubuntu VM. It keeps SQLite on the VM's persistent boot volume, enables HTTP Basic Authentication, and leaves all trading activity paper-only.

## What this setup uses

- OCI Ampere A1 Always Free compute. A practical starting size is **1 OCPU / 6 GB RAM**; the Always Free tenancy allowance is shared across A1 instances and currently totals up to 2 OCPUs / 12 GB RAM.
- Ubuntu Server ARM64 image marked **Always Free Eligible**.
- The instance boot volume (choose 50 GB). OCI boot volumes are persistent block storage and count toward the tenancy's 200 GB Always Free block-volume allowance. The database is stored under `/var/lib/ai-crypto-trader/trader.db`.
- Python 3, FastAPI, Uvicorn, SQLite, systemd, and optionally Caddy for automatic HTTPS.
- No exchange API keys, live orders, background scanner, or scheduled jobs.

Always Free compute and storage are subject to OCI's current eligibility, home-region, capacity, and idle-instance policies. Check the OCI Console's **Always Free Eligible** labels and your tenancy limits before creating resources. Oracle may reclaim idle Always Free compute instances under its published utilization policy. The 200 GB storage allowance is shared by boot and block volumes.

## 1. Create the OCI account and VM

1. Create/sign in to an Oracle Cloud Free Tier account: https://www.oracle.com/cloud/free/
2. Choose the **Home Region** carefully during registration. Always Free compute and block volumes must be provisioned in the home region.
3. In OCI Console, open **Compute → Instances → Create instance**.
4. Choose an instance name such as `ai-crypto-trader-v2`.
5. Select an Ubuntu Server ARM64 image marked **Always Free Eligible**.
6. Select shape **VM.Standard.A1.Flex** and start with **1 OCPU and 6 GB memory**. Do not exceed the shared Always Free allowance of 2 OCPUs and 12 GB memory.
7. Keep the boot volume at **50 GB** initially and verify the estimate shows Always Free eligible.
8. Use a public subnet and assign a public IPv4 address. Create or select an SSH key pair and save the private key safely.
9. Create the instance. If OCI reports that host capacity is unavailable, try again later or another availability domain in the same home region; do not switch to a paid shape.

The boot volume is persistent. Do not select an option that deletes the boot volume if you later terminate the compute instance; retain the boot volume if you want to attach it to a replacement VM. Keep regular backups of the SQLite database as well.

## 2. Configure network access

In the subnet's Security List or the instance's Network Security Group, add ingress rules:

- TCP 22 (SSH): source should be **your own public IP/32**, not the whole internet.
- TCP 80: source `0.0.0.0/0` (needed for Caddy's HTTP validation and redirects).
- TCP 443: source `0.0.0.0/0` (HTTPS dashboard).

Do not open port 8000. The application listens only on `127.0.0.1:8000`; Caddy proxies HTTPS traffic to it.

If Ubuntu's UFW firewall is enabled, allow OpenSSH, HTTP, and HTTPS there too.

## 3. Connect and install the app

From your computer, SSH into the public IP using the private key and the default Ubuntu image username (usually `ubuntu`):

```bash
ssh -i /path/to/private-key.key ubuntu@YOUR_PUBLIC_IP
```

Clone the deployment branch and run the installer:

```bash
git clone --branch deploy/oracle-cloud-always-free https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
cd ai-crypto-trader-claude
sudo bash deploy/oracle-cloud/install.sh
```

The installer creates a locked-down Linux service user, installs Python dependencies, writes a systemd service, creates the persistent SQLite directory, and generates a random dashboard password if no environment file exists. It does not print the password to the terminal.

The generated settings are stored in `/etc/ai-crypto-trader.env` with root-only permissions. The database URL is:

```
sqlite:////var/lib/ai-crypto-trader/trader.db
```

This path is on the instance's persistent boot volume, not an ephemeral container filesystem.

## 4. Enable HTTPS with a hostname

For a public dashboard, use a DNS hostname. You can use a domain you own or a free dynamic-DNS hostname. Create an **A record** pointing to the VM's public IPv4 address, wait for DNS to resolve, then run the installer again and enter the hostname when prompted. The installer will configure Caddy, which obtains and renews a TLS certificate automatically.

If you prefer to configure Caddy manually, copy `Caddyfile.example` to `/etc/caddy/Caddyfile`, replace the example hostname, then run:

```bash
sudo systemctl reload caddy
```

Do not send the dashboard password over plain HTTP. If you do not yet have a hostname, keep the app bound to localhost and use an SSH tunnel for temporary access:

```bash
ssh -i /path/to/private-key.key -L 8000:127.0.0.1:8000 ubuntu@YOUR_PUBLIC_IP
```

Then open `http://127.0.0.1:8000` on your computer. This tunnel is for temporary administration, not a public deployment.

## 5. Sign in and verify

- Dashboard URL: `https://YOUR_HOSTNAME`
- Username: `admin`
- Password: read securely on the VM with `sudo less /etc/ai-crypto-trader.env`
- Health endpoint: `https://YOUR_HOSTNAME/api/health`

Expected health response:

```json
{"status":"ok","version":"2.1.0","mode":"paper-only"}
```

The health endpoint is public for monitoring. All other dashboard/API routes require Basic Authentication. API documentation is disabled.

Useful service commands:

```bash
sudo systemctl status ai-crypto-trader
sudo journalctl -u ai-crypto-trader -f
sudo systemctl restart ai-crypto-trader
```

## 6. Updating the application

After changes are merged into the deployment branch, SSH to the VM and run:

```bash
cd ~/ai-crypto-trader-claude
git pull
sudo bash deploy/oracle-cloud/install.sh
```

The installer keeps the existing environment file and password. It refreshes the app code and Python environment, then restarts the service. Back up the SQLite database before upgrades.

## 7. Back up the SQLite database

Create a consistent SQLite backup while the app is running:

```bash
sudo install -d -m 700 /var/backups/ai-crypto-trader
sudo sqlite3 /var/lib/ai-crypto-trader/trader.db \
  ".backup '/var/backups/ai-crypto-trader/trader-$(date +%F-%H%M%S).db'"
```

For this command, install the SQLite CLI if needed with `sudo apt-get install sqlite3`. Copy backups off the VM periodically (for example, to your own computer or OCI Object Storage). A persistent disk is not a substitute for backups.

## Safety and cost notes

- The dashboard stays **paper trading only** and reads public exchange market data. Do not add exchange API keys.
- Scanning remains manual; this deployment does not add cron jobs or background trading.
- Keep SSH restricted to your IP and expose only 80/443 publicly.
- Keep the generated dashboard password private and rotate it by editing `/etc/ai-crypto-trader.env`, then restarting the service.
- Oracle's Free Tier is not an unconditional uptime guarantee. Monitor resource usage, account notices, and Always Free eligibility. Avoid paid resources and verify the estimated cost before provisioning.
