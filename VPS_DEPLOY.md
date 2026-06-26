# Deploying Veritas on a VPS

Runs the whole stack on one machine with Docker Compose:
**app + Postgres + Ollama + MinIO**.

## 1. Pick a VPS

CPU-only is fine (just slower inference). Size by the model you want:

| RAM   | Model              | Notes                          |
|-------|--------------------|--------------------------------|
| 4 GB  | `llama3.2:1b`      | Works, tight                   |
| 8 GB  | `llama3.2:3b`      | **Recommended** sweet spot     |
| 16 GB | `llama3` (8B)      | Best quality                   |

Good value: Hetzner CPX21/CPX31, DigitalOcean, Vultr, Contabo. Use Ubuntu 22.04/24.04.

## 2. Install Docker on the VPS

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out/in after this
```

## 3. Get the code

```bash
git clone https://github.com/ichigo-k/Veritas-AI-Powered-Grading-System.git
cd Veritas-AI-Powered-Grading-System
```

## 4. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Minimum to set:

```ini
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(50))"
DJANGO_SECRET_KEY=<long-random-string>

DEBUG=False
ALLOWED_HOSTS=your-domain.com,<vps-ip>

# Postgres / MinIO credentials (change from defaults!)
POSTGRES_PASSWORD=<strong-password>
MINIO_ROOT_USER=<minio-user>
MINIO_ROOT_PASSWORD=<strong-password>

# Which model to run (must match what you pull in step 6)
OLLAMA_MODEL_ID=llama3.2:3b
S3_BUCKET_NAME=grader-uploads
```

> `DATABASE_URL`, `OLLAMA_BASE_URL`, `AWS_S3_ENDPOINT_URL` etc. are wired
> automatically in `docker-compose.yml` — you don't set them in `.env`.

## 5. Bring up the stack

```bash
docker compose up -d --build
docker compose ps          # all services should be "running"/"healthy"
```

## 6. Pull the Ollama model (one time — persists in a volume)

```bash
docker compose exec ollama ollama pull llama3.2:3b
docker compose exec ollama ollama run llama3.2:3b "Say OK"   # quick test
```

## 7. Run database migrations

```bash
docker compose exec app python manage.py migrate              # Django system DB
docker compose exec app python manage.py migrate --database=neon   # grader DB (if any managed tables)
```

## 8. Create the MinIO bucket

Open the MinIO console at `http://<vps-ip>:9001` (login = your
`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) → **Buckets → Create Bucket** →
name it `grader-uploads` (must match `S3_BUCKET_NAME`).

## 9. Verify

```bash
curl http://localhost:8000/api/health/        # -> healthy
```

The API is now live on port `8000`.

## 10. (Recommended) Put it behind HTTPS

Don't expose port 8000 directly in production. Add Caddy or Nginx in front for
TLS. Minimal Caddy example (`/etc/caddy/Caddyfile`):

```
your-domain.com {
    reverse_proxy localhost:8000
}
```

Then lock down the firewall so only 80/443 (and SSH) are open:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

(The MinIO console on 9001 should stay closed to the public or be proxied too.)

## Updating later

```bash
git pull
docker compose up -d --build
docker compose exec app python manage.py migrate
```
