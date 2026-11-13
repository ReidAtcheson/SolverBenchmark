#!/bin/bash
# Runs on the Batch VM; source and results are the only Cloud Storage objects.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Batch script environments may not export PATH even when the shell can find
# commands. CMake and its child processes need it to locate their own resources.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
exec > /tmp/sb-worker.log 2>&1
token() {
  curl -fsS -H 'Metadata-Flavor: Google' \
    'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}
save_failure_log() {
  local status=$?
  if (( status != 0 )); then
    # A small tail avoids leaking unbounded build output or incurring large egress.
    tail -c 65536 /tmp/sb-worker.log > /tmp/sb-worker-tail.log
    curl -fsS --max-time 30 -X POST -H "Authorization: Bearer $(token)" -H 'Content-Type: text/plain' --data-binary @/tmp/sb-worker-tail.log \
      "https://storage.googleapis.com/upload/storage/v1/b/${RUN_BUCKET}/o?uploadType=media&name=failure-${RUN_KEY}.log" >/dev/null 2>&1 || true
  fi
  return "$status"
}
trap save_failure_log EXIT
apt-get update -qq
apt-get install -y -qq ca-certificates curl cmake g++ python3 python3-venv libblas-dev
work=$(mktemp -d)
cd "$work"
curl -fsS --retry 3 -H "Authorization: Bearer $(token)" \
  "https://storage.googleapis.com/storage/v1/b/${RUN_BUCKET}/o/source.tar.gz?alt=media" -o source.tar.gz
mkdir source
tar xzf source.tar.gz -C source
cd source
curl -fsSL --retry 3 https://github.com/xiaoyeli/superlu/archive/refs/tags/v6.0.1.tar.gz -o /tmp/sb-superlu.tar.gz
echo '6c5a3a9a224cb2658e9da15a6034eed44e45f6963f5a771a6b4562f7afb8f549  /tmp/sb-superlu.tar.gz' | sha256sum -c -
tar xzf /tmp/sb-superlu.tar.gz -C /tmp
/usr/bin/cmake -S /tmp/superlu-6.0.1 -B /tmp/superlu-build -DCMAKE_INSTALL_PREFIX=/tmp/superlu -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -Denable_tests=OFF -Denable_examples=OFF
/usr/bin/cmake --build /tmp/superlu-build -j2
/usr/bin/cmake --install /tmp/superlu-build
python3 -m venv /tmp/sb-venv
/tmp/sb-venv/bin/pip install .
/usr/bin/cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DSOLVER_BENCHMARK_BUILD_TESTS=OFF -DSOLVER_BENCHMARK_BUILD_SUPERLU=ON -DCMAKE_PREFIX_PATH=/tmp/superlu
/usr/bin/cmake --build build -j2
# Capture actual runtime files for the manifest's dependency hashes.
python3 - <<'PY'
import json, os, re, subprocess
lines = subprocess.check_output(['ldd', 'build/sb-superlu'], text=True)
def library(pattern):
    for line in lines.splitlines():
        match = re.search(pattern + r'\S*\s+=>\s+(\S+)', line)
        if match and os.path.isfile(match.group(1)):
            return os.path.realpath(match.group(1))
    raise RuntimeError('runtime dependency missing: ' + pattern + '\n' + lines)
with open('cloud-deployment.json', 'w') as f:
    json.dump({'schema_version': '1.0', 'solvers': {
        'eigen-sparse-lu': {'executable': os.path.abspath('build/sb-eigen'), 'arguments': [], 'environment': {}},
        'superlu': {'executable': os.path.abspath('build/sb-superlu'), 'arguments': [], 'environment': {},
                    'runtime_dependencies': {'superlu': library('libsuperlu'), 'blas': library('libblas')}}}}, f)
PY
set +e
/tmp/sb-venv/bin/solver-benchmark run "$BENCHMARK_PROFILE" --deployment cloud-deployment.json --output /tmp/sb-result
status=$?
set -e
# Only the four files used by the report are transferred; input matrices and worker traces stay on the VM.
if [[ -f /tmp/sb-result/results.jsonl && -f /tmp/sb-result/execution.json && -f /tmp/sb-result/profile.json && -f /tmp/sb-result/version-manifest.json ]]; then
  tar -czf /tmp/sb-result.tar.gz -C /tmp/sb-result results.jsonl execution.json profile.json version-manifest.json
  bytes=$(wc -c < /tmp/sb-result.tar.gz)
  if (( bytes > MAX_RESULT_BYTES )); then
    echo "Result archive exceeds transfer cap: $bytes > $MAX_RESULT_BYTES" >&2
    exit 1
  fi
  curl -fsS --retry 3 -X POST -H "Authorization: Bearer $(token)" -H 'Content-Type: application/octet-stream' --data-binary @/tmp/sb-result.tar.gz \
    "https://storage.googleapis.com/upload/storage/v1/b/${RUN_BUCKET}/o?uploadType=media&name=result-${RUN_KEY}.tar.gz"
fi
exit "$status"
