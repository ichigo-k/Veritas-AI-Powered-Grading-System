# verion-ai-grader

A standalone Django microservice that acts as the AI grading engine for the `ai-powered-grading-system`. It connects to the shared PostgreSQL database, grades subjective answers using AWS Bedrock with rubric-guided prompts, can fetch uploaded answer files from S3 and pass them directly to multimodal Bedrock models, detects plagiarism via answer hash comparison, and writes final scores back to the database.

The main system handles MCQ auto-scoring at submission time. This service handles everything after that — subjective grading, score finalisation, and feedback storage.

---

## How it fits in

```
Student submits
  → main system scores MCQ, writes partial score to assessment_attempts.score
  → lecturer triggers grading (sets gradingStatus = GRADING)
  → this service is called via POST /api/grade/assessment/{id}/
  → reads subjective answers, fetches S3 files when present, calls AWS Bedrock per answer
  → adds subjective scores to the existing MCQ score
  → writes final score back to assessment_attempts.score
  → writes per-answer AI feedback to grader_answerfeedback
  → sets assessments.gradingStatus = GRADED
  → main system reads score, computes grade letter on the fly from admin-configured scale
```

**Grade letters are not stored here.** Only the raw numeric score is written to `assessment_attempts.score`. The main system computes the letter grade at read time using the grading scale configured by the admin in system settings.

> **Known gap:** The main system's `POST /api/lecturer/assessments/[id]/start-grading` route currently only sets `gradingStatus = GRADING` — it does not yet call this service. The call to `POST <GRADER_URL>/api/grade/assessment/{id}/` with the `X-API-Key` header still needs to be wired up in the main system.

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- PostgreSQL shared with the main system (must have run `npx prisma migrate deploy` first to create the shared tables)
- AWS credentials with Bedrock access

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values. See [Environment Variables](#environment-variables) below.

### 3. Run migrations

```bash
uv run python manage.py migrate
```

This applies migrations to **two separate databases**:

**Django system database** (`default` — SQLite by default, or `DJANGO_DB_URL` if set):
- `auth_keys_apikey` — hashed API keys for endpoint authentication
- `auth_*`, `django_*` — Django internals

**Shared PostgreSQL** (`neon` — `DATABASE_URL`):
- No tables are created here by Django. All tables in the shared DB are owned by Prisma (`npx prisma migrate deploy` in the main system). Django's grader migration is a no-op — it only records model state so `makemigrations` stays quiet.

> **Why two databases?** Django requires its own system tables (`auth_*`, `django_migrations`, etc.).
> Putting them in the shared Postgres would cause Prisma drift detection errors since Prisma doesn't
> know about them. The split keeps the shared DB clean and Prisma happy.

### 4. Generate an API key

```bash
uv run python manage.py generate_api_key --label "main-system"
```

The plaintext key is printed to stdout **once**. Store it securely — it cannot be recovered. Use it as the `X-API-Key` header on all requests to this service.

### 5. Run the development server

```bash
uv run python manage.py runserver 0.0.0.0:8000
```

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `DJANGO_SECRET_KEY` | Django secret key — generate with `python -c "import secrets; print(secrets.token_hex(50))"` |
| `DATABASE_URL` | Shared PostgreSQL connection string (same DB as the main system) |
| `AWS_ACCESS_KEY_ID` | AWS access key with Bedrock permissions |
| `AWS_SECRET_ACCESS_KEY` | AWS secret access key |
| `AWS_REGION` | AWS region where Bedrock is available, e.g. `us-east-1` |
| `BEDROCK_MODEL_ID` | Bedrock model identifier, e.g. `us.anthropic.claude-sonnet-4-6` |
| `S3_BUCKET_NAME` | S3 bucket that stores uploaded answer files |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `DJANGO_DB_URL` | SQLite (`django_system.db`) | Connection string for Django's own system tables (`auth_*`, `auth_keys_apikey`, etc.). Defaults to a local SQLite file. Set to a separate Postgres DB in production to avoid any shared-DB conflicts. |
| `DEBUG` | `False` | Set to `True` for local development |
| `ALLOWED_HOSTS` | `*` when DEBUG=True, else `[]` | Comma-separated list of allowed hostnames |
| `BEDROCK_MAX_TOKENS` | `2048` | Max tokens per Bedrock invocation |
| `GRADING_CONCURRENCY` | `1` | Retained for compatibility; batch grading processes one attempt at a time |
| `S3_UPLOAD_PREFIX` | `grader-uploads` | Prefix used when resolving plain filenames to S3 keys |
| `S3_PRESIGNED_URL_EXPIRES_IN` | `3600` | Lifetime in seconds for generated presigned URLs |

---

## Database architecture

This service uses a **split database routing** strategy to keep Django's internal tables out of the shared PostgreSQL database.

```
┌─────────────────────────────────────────────────────────┐
│  default (DJANGO_DB_URL or SQLite django_system.db)     │
│                                                         │
│  auth_keys_apikey   ← API key auth (Django-managed)     │
│  auth_*             ← Django internals                  │
│  django_*           ← Django migration tracking         │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  neon (DATABASE_URL) — shared with main system          │
│                                                         │
│  grader_gradingresult    ← Prisma-owned, written here   │
│  grader_answerfeedback   ← Prisma-owned, written here   │
│                                                         │
│  assessments             ← read/write (managed=False)   │
│  assessment_attempts     ← read/write (managed=False)   │
│  assessment_sections     ← read only  (managed=False)   │
│  questions               ← read only  (managed=False)   │
│  rubric_criteria         ← read only  (managed=False)   │
│  student_answers         ← read only  (managed=False)   │
└─────────────────────────────────────────────────────────┘
```

`managed=False` means Django reads and writes rows but never runs `CREATE`, `ALTER`, or `DROP` against those tables. **Prisma is the sole migration authority for every table in the shared DB**, including `grader_gradingresult` and `grader_answerfeedback`. Run `npx prisma migrate deploy` in the main system before starting this service.

---

## API Endpoints

All endpoints require the `X-API-Key` header.

Interactive docs available at:
- **Swagger UI:** `http://localhost:8000/api/docs/`
- **ReDoc:** `http://localhost:8000/api/redoc/`
- **OpenAPI schema:** `http://localhost:8000/api/schema/`

### `POST /api/grade/assessment/{assessment_id}/`

Grades all `SUBMITTED` and `TIMED_OUT` attempts for an assessment sequentially.

**What it does:**
1. Fetches all eligible attempts
2. Runs plagiarism detection across all answer hashes
3. Grades each subjective answer via Bedrock (one at a time)
4. Adds subjective scores to the existing MCQ score on each attempt
5. Writes final `score` to `assessment_attempts`
6. Persists per-answer feedback to `grader_answerfeedback`
7. Sets `assessments.gradingStatus = GRADED`

**Response 200:**
```json
{
  "assessment_id": 42,
  "graded_count": 15,
  "grading_status": "GRADED",
  "plagiarism_flags": [
    { "question_id": 7, "flagged_attempt_ids": [3, 9] }
  ]
}
```

**Response 404:** Assessment not found  
**Response 401:** Missing or invalid API key

---

### `POST /api/grade/attempt/{attempt_id}/`

Grades a single attempt on demand. Useful for re-grading or targeted grading.

**Response 200:**
```json
{
  "attempt_id": 17,
  "score": 74.5,
  "plagiarism_flagged": false,
  "answer_feedbacks": [
    {
      "question_id": 3,
      "total_score": 16.0,
      "max_score": 20.0,
      "flag": "none",
      "flag_reason": "",
      "criteria_feedback": [
        {
          "criterion": "Accuracy of layer descriptions",
          "awarded": 8,
          "max": 10,
          "justification": "Student correctly identified all 7 layers..."
        }
      ],
      "bedrock_error": false
    }
  ],
  "error_notes": ""
}
```

**Response 404:** Attempt not found  
**Response 401:** Missing or invalid API key

---

## Score computation

```
final_score = min(existing_mcq_score + sum(subjective_scores), assessment.totalMarks)
```

- `existing_mcq_score` — already on `assessment_attempts.score` from submission time
- `subjective_scores` — sum of per-criterion scores from Bedrock, each capped at `question.marks`
- Result capped at `assessment.totalMarks`, minimum 0

If Bedrock fails for a specific answer, that answer scores 0 and grading continues for the rest. The error is recorded in `grader_answerfeedback.bedrock_error` and `grader_gradingresult.error_notes`.

If a subjective answer includes a file reference, the service resolves it from S3, infers the file type from metadata or extension, and sends the raw bytes directly to Bedrock. This path currently supports Anthropic Claude models for PDF and image attachments.

---

## Running tests

```bash
uv run pytest
```

---

## Docker

```bash
docker build -t verion-ai-grader .
docker run -p 8000:8000 --env-file .env verion-ai-grader
```

In Docker, set `DJANGO_DB_URL` to a real Postgres connection string rather than relying on the SQLite default, since the SQLite file won't persist across container restarts.

---

## Common commands

```bash
uv sync                                                    # install dependencies
uv run python manage.py migrate                            # apply migrations (both DBs)
uv run python manage.py migrate --database neon            # shared DB migrations only (no-op)
uv run python manage.py migrate --database default         # system DB migrations only
uv run python manage.py generate_api_key --label "label"   # create an API key
uv run python manage.py runserver 0.0.0.0:8000             # dev server
uv run pytest                                              # run tests
```

