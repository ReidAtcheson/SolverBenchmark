#!/bin/bash
# Runs on the Batch VM; source and results are the only Cloud Storage objects.
set -euo pipefail
# Budget setup time against the Batch deadline, leaving a window for uploads.
SECONDS=0
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
apt-get install -y -qq ca-certificates curl cmake g++ python3 python3-venv libopenblas-pthread-dev
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
# Select Debian's pthread build directly, independent of the system BLAS
# alternatives. SuperLU otherwise silently falls back to its serial CBLAS.
export SB_OPENBLAS_LIBRARY
SB_OPENBLAS_LIBRARY=$(dpkg-query -L libopenblas-pthread-dev | grep '/openblas-pthread/libopenblas\.so$')
test -f "$SB_OPENBLAS_LIBRARY"
/usr/bin/cmake -S /tmp/superlu-6.0.1 -B /tmp/superlu-build -DCMAKE_INSTALL_PREFIX=/tmp/superlu -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -Denable_tests=OFF -Denable_examples=OFF -Denable_internal_blaslib=OFF -DTPL_BLAS_LIBRARIES:FILEPATH="$SB_OPENBLAS_LIBRARY"
/usr/bin/cmake --build /tmp/superlu-build -j2
/usr/bin/cmake --install /tmp/superlu-build
python3 -m venv /tmp/sb-venv
/tmp/sb-venv/bin/pip install .
/usr/bin/cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DSOLVER_BENCHMARK_BUILD_TESTS=OFF -DSOLVER_BENCHMARK_BUILD_SUPERLU=ON -DCMAKE_PREFIX_PATH=/tmp/superlu -DBLA_VENDOR=OpenBLAS
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
blas = library('libopenblas')
if not os.path.samefile(blas, os.environ['SB_OPENBLAS_LIBRARY']):
    raise RuntimeError('SuperLU did not load the requested pthread OpenBLAS: ' + blas)
with open('cloud-deployment.json', 'w') as f:
    json.dump({'schema_version': '1.0', 'solvers': {
        'eigen-sparse-lu': {'executable': os.path.abspath('build/sb-eigen'), 'arguments': [], 'environment': {}},
        'superlu': {'executable': os.path.abspath('build/sb-superlu'), 'arguments': [], 'environment': {},
                    'runtime_dependencies': {'superlu': library('libsuperlu'), 'blas': blas}}}}, f)
PY
set +e
if [[ "$SINGLE_CORE" == 1 ]]; then
  # Derive the actual recorded profile on the VM from the packaged source
  # profile, and restrict the benchmark and all its children to one CPU.
  export SB_SINGLE_CORE_AFFINITY_PATH=/tmp/sb-single-core-affinity.json
  command=(/tmp/sb-venv/bin/python cloud/single_core.py "$BENCHMARK_PROFILE" /tmp/sb-single-core-profile.json
    /tmp/sb-venv/bin/solver-benchmark run /tmp/sb-single-core-profile.json
    --deployment cloud-deployment.json --output /tmp/sb-result --max-entries "$MAX_ENTRIES")
else
  command=(/tmp/sb-venv/bin/solver-benchmark run "$BENCHMARK_PROFILE" --deployment cloud-deployment.json --output /tmp/sb-result --max-entries "$MAX_ENTRIES")
fi
if (( UPLOAD_GRACE_SECONDS > 0 )); then
  remaining=$(( MAX_RUN_SECONDS - UPLOAD_GRACE_SECONDS - SECONDS ))
  if (( remaining > 0 )); then
    # The Python supervisor handles SIGINT by reaping its isolated worker.
    # GNU timeout reports 124 for this intentional interruption.
    timeout --signal=INT --kill-after=20s "${remaining}s" "${command[@]}"
    status=$?
  else
    echo 'Setup exhausted the benchmark budget; reserving time for diagnostics.' >&2
    status=124
  fi
else
  "${command[@]}"
  status=$?
fi
set -e
# Record the actual VM identity in the portable execution metadata. Even if
# metadata lookup fails, transfer any completed results and report the error.
if [[ -f /tmp/sb-result/execution.json ]]; then
  if [[ "$SINGLE_CORE" == 1 ]]; then
    if ! /tmp/sb-venv/bin/python cloud/single_core.py --attach \
        /tmp/sb-single-core-affinity.json /tmp/sb-result/execution.json; then
      status=1
    fi
  fi
  if ! /tmp/sb-venv/bin/python -m solver_benchmark.gcp_metadata /tmp/sb-result/execution.json; then
    status=1
  fi
fi
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
