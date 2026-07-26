#!/usr/bin/env bash
#
# Deploy the Bugzilla triage poller to Cloud Run as a worker pool.
#
# A worker pool runs an always-on, non-request workload (no HTTP port). This
# service polls Bugzilla for untriaged bugs and triggers the frontend-triage
# hackbot agent via the hackbot-api. See README.md for how it works.
#
# Prereqs (one-time):
#   gcloud auth login
#   gcloud config set project <PROJECT_ID>
#   gcloud components install beta   # worker pools live under the beta track
#
# Usage:
#   PROJECT=my-proj ./deploy.sh
#
# Tuning knobs are not set here; their defaults live in app/config.py.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-hackbot-bug-poller}"
REPO="${REPO:-hackbot}"

# Not a secret, just a public URL, so it lives here rather than being passed in
# every time. Fill this in once with the deployed hackbot-api URL and commit it;
# after that a normal deploy needs nothing but PROJECT.
HACKBOT_API_URL="${HACKBOT_API_URL:-SET_ME}"
if [ "${HACKBOT_API_URL}" = "SET_ME" ]; then
  echo "ERROR: HACKBOT_API_URL has no value yet." >&2
  echo "       Edit the default in this script to your deployed hackbot-api URL" >&2
  echo "       (one-time), or pass HACKBOT_API_URL=... for a one-off deploy." >&2
  exit 1
fi

SA_NAME="${SA_NAME:-hackbot-bug-poller-run}"
SA_EMAIL="${SA_EMAIL:-${SA_NAME}@${PROJECT}.iam.gserviceaccount.com}"

# Shared with hackbot-api and hackbot-ui; must already exist.
API_KEY_SECRET="${API_KEY_SECRET:-external-api-key}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:latest"
# Build context is the repo root (the Dockerfile needs the workspace lock files).
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "==> Ensuring runtime service account '${SA_EMAIL}' exists"
gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Hackbot Bugzilla Poller (Cloud Run runtime)"

echo "==> Checking secret '${API_KEY_SECRET}' exists"
if ! gcloud secrets describe "${API_KEY_SECRET}" >/dev/null 2>&1; then
  echo "ERROR: secret '${API_KEY_SECRET}' not found. It is shared with hackbot-api" >&2
  echo "       and hackbot-ui, so it should already exist -- check the project." >&2
  exit 1
fi

echo "==> Granting the SA read access to the API key"
gcloud secrets add-iam-policy-binding "${API_KEY_SECRET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role=roles/secretmanager.secretAccessor >/dev/null

echo "==> Ensuring Artifact Registry repo '${REPO}' exists in ${REGION}"
gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker --location="${REGION}" \
    --description="Hackbot container images"

echo "==> Building & pushing image with Cloud Build: ${IMAGE}"
gcloud builds submit "${ROOT_DIR}" \
  --config <(printf 'steps:\n- name: gcr.io/cloud-builders/docker\n  env: ["DOCKER_BUILDKIT=1"]\n  args: ["build","-t","%s","-f","services/%s/Dockerfile","."]\nimages: ["%s"]\n' "${IMAGE}" "${SERVICE}" "${IMAGE}")

# ^|^ makes gcloud split on '|' rather than ',', which a pasted Bugzilla URL can
# legally contain. '|' cannot appear in a URL, so it is the one safe separator.
ENV_VARS="^|^HACKBOT_API_URL=${HACKBOT_API_URL}"
ENV_VARS="${ENV_VARS}|ENVIRONMENT=production"

# --- Bugzilla queries --------------------------------------------------------
#
# One BUGZILLA_QUERY_<LABEL> line per search, the value pasted straight from your
# browser's address bar. See README.md -- and check the hit count before adding
# one, since every bug a query matches eventually gets a paid run.
#
# The query below is a placeholder; replace it with your own.
ENV_VARS="${ENV_VARS}|BUGZILLA_QUERY_NEW_TAB_PAGE=https://bugzilla.mozilla.org/buglist.cgi?product=Firefox&component=New%20Tab%20Page&bug_status=UNCONFIRMED&bug_status=NEW&f1=assigned_to&o1=equals&v1=nobody%40mozilla.org&bug_type=defect&bug_severity=S1&bug_severity=S2"
# ENV_VARS="${ENV_VARS}|BUGZILLA_QUERY_TABBED_BROWSER=https://bugzilla.mozilla.org/buglist.cgi?product=Firefox&component=Tabbed%20Browser&bug_status=UNCONFIRMED&bug_status=NEW&f1=assigned_to&o1=equals&v1=nobody%40mozilla.org&bug_type=defect&bug_severity=S1&bug_severity=S2"

echo "==> Deploying worker pool"
gcloud beta run worker-pools deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --scaling 1 \
  --memory 512Mi \
  --service-account "${SA_EMAIL}" \
  --set-env-vars "${ENV_VARS}" \
  --set-secrets "HACKBOT_API_KEY=${API_KEY_SECRET}:latest"

echo "==> Deployed worker pool '${SERVICE}'"
echo "    Logs: gcloud beta run worker-pools logs read ${SERVICE} --region ${REGION}"
