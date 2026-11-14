# Cloud benchmark profiles

A cloud profile combines a benchmark recipe with a fixed list of hardware targets,
a job time limit, and an upload limit. `cloud/profiles/light.json` runs
`examples/light-profile.json` on GCP Batch in `us-central1-c` by default:

| Target | Machine | Physical cores | CPU |
| --- | --- | --- | --- |
| intel | n2-standard-16 | 8 (16 vCPU) | Intel, minimum Ice Lake |
| amd | t2d-standard-8 | 8 | AMD EPYC Milan |
| arm | c4a-standard-8 | 8 | Google Axion Arm |

N2's minimum is a constraint, not an exact CPU model. Actual host details are
recorded in `execution.json`. Zone capacity can vary; these jobs do not fall back
to different machines. Copy the profile to add future targets or recipes. The
`provider` field identifies the backend; `cloud/run_gcp.py` is the current GCP
adapter, and other providers can consume the same profile structure with their
own adapters. Both repository solvers (Eigen SparseLU and SuperLU) are built on
each target. The machine code is compiled on its native architecture using the
repository's release build defaults, without `-march=native`. SuperLU links
Debian's pthread OpenBLAS build, which chooses kernels for the host CPU and
honors the profile's OpenBLAS thread limit. The worker verifies the loaded
library and records its hash. SuperLU's sparse factorization remains serial
apart from work delegated to BLAS, so small operations may use only one core.
Eigen SparseLU uses its own serial supernodal update kernel, not external BLAS;
its numeric phase therefore does not gain BLAS parallelism from this build.
OpenBLAS's idle worker threads can spin, so CPU / wall above one does not by
itself establish useful parallel work or a speedup. Compare wall times as well;
system CPU time describes kernel-mode work and does not count user-mode spins.

## Heavy profile

`cloud/profiles/heavy.json` selects `examples/heavy-profile.json`: seven
representative Laplacian, transport and wave cases in one, two and three
dimensions, each with exactly 200,000 rows and columns. It uses the same two
solvers and timing/resource passes as light, but uses one timing warm-up and one
measured run, followed by one traced run without a warm-up. It does not include
every case in the light catalog. Each solver/pass has a 30-minute deadline, and
each Batch task has a two-hour deadline, including setup and input generation.
The 28 individual pass deadlines total 14 hours, so the two-hour limit can end
the task before all cases and passes finish. The worker aims to stop the
benchmark two minutes before the task deadline and upload completed results.
This upload is best effort: if setup or input generation overruns, no results
exist yet; interruption, upload delays or the hard deadline may also prevent
recovery of earlier results. In particular, sparse direct factorization of
200,000-row 3D problems can exhaust memory or time on these machines; check
every solver outcome and missing measurement in the report.

| Target | Machine | vCPUs | Memory |
| --- | --- | ---: | ---: |
| intel | n2-highmem-32, minimum Intel Ice Lake | 32 | 256 GiB |
| amd | n2d-highmem-32 | 32 | 256 GiB |
| arm | c4a-standard-8 | 8 | 32 GiB |

Intel and AMD use simultaneous multithreading; Arm has one vCPU per core, so
the machines do not have the same physical core count. Arm uses the same smaller
machine as the light profile and has considerably less memory, which may prevent
some heavy cases from completing. Each target uses a 100 GB boot disk; the Intel
and AMD tasks reserve 250,000 MiB and Arm reserves 28,000 MiB. The profile raises
the per-case generation limit to 10 million entries. Inputs are all generated before the
first solve and remain on the VM; do not use a laptop as a prerequisite for
running this profile. Verify machine availability and quota in the selected zone.
The heavy Intel and AMD machines can cost substantially more per hour than light;
check current prices and budget for up to two hours per machine, plus provisioning,
disks, network and other charges. The deadline is not a strict billing cap.

After completing the prerequisites below, use a new output directory and save
the launcher's output so the exact job prefix and bucket are available for
cleanup. `--dry-run` creates no cloud resources.

```sh
python3 cloud/run_gcp.py --profile cloud/profiles/heavy.json \
  --project YOUR_PROJECT_ID --service-account YOUR_VM_ACCOUNT@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --output results/cloud-heavy-001 --dry-run

set -o pipefail
python3 cloud/run_gcp.py --profile cloud/profiles/heavy.json \
  --project YOUR_PROJECT_ID --service-account YOUR_VM_ACCOUNT@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --output results/cloud-heavy-001 2>&1 | tee cloud-heavy-001.log

solver-benchmark report results/cloud-heavy-001/intel results/cloud-heavy-001/amd \
  results/cloud-heavy-001/arm --output reports/cloud-heavy-001.html
```

The report command also writes `reports/cloud-heavy-001.json`. If a target
fails, report the available target directories and use the single-target retry
below with `cloud/profiles/heavy.json` and a new output directory. Inspect input
hashes before comparing results across architectures. After every run, verify
deletion of the jobs, VMs, boot disks and scratch bucket as described under
Cleanup and billing.

## Prerequisites

- A billed GCP project, `gcloud` installed and authenticated, and the Batch,
  Compute Engine, and Cloud Storage APIs enabled. The project needs a usable
  default VPC with external internet access for package/source downloads, quota
  and capacity for all three machines and boot disks in the selected zone.
- The caller must be able to create/delete Batch jobs and buckets, set bucket IAM,
  and act as the VM service account. For example, use suitable scoped grants for
  Batch job editor, Storage admin, and Service Account User. The project must
  permit Batch's service agent to create its Compute resources.
- An existing VM service account to pass with `--service-account`. That
  service account needs `roles/batch.agentReporter` on the project before the run.
  The runner grants it `roles/storage.objectUser` on the temporary bucket only;
  no permanent project IAM grant is added by the runner. The instance needs access
  to the metadata server and outbound HTTPS.
- A local Python 3.11+ environment with this project installed for the report.
  The runner uses the Cloud Storage JSON API with the active `gcloud` credentials
  to create its labeled bucket and lifecycle policy atomically.

```sh
python3 cloud/run_gcp.py \
  --project YOUR_PROJECT_ID \
  --service-account YOUR_VM_ACCOUNT@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --output results/cloud-light-001 --dry-run

python3 cloud/run_gcp.py \
  --project YOUR_PROJECT_ID \
  --service-account YOUR_VM_ACCOUNT@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --output results/cloud-light-001

solver-benchmark report results/cloud-light-001/intel results/cloud-light-001/amd \
  results/cloud-light-001/arm --output reports/cloud-light-001.html
```

The first command prints the Batch job configurations and compressed source size
without creating cloud resources. `--region` and `--zone` may be changed together,
but check that all listed machine types and CPU platforms are available there.
The output directory must not exist. The runner submits three jobs in parallel,
collects each result when its job finishes, and returns nonzero on failure or
missing results. The light profile limits each task to six hours. The launcher cancels
any job that has not reached `RUNNING` within an hour of submission and continues
waiting for the other jobs. A timeout can stop the worker before it uploads results;
already collected results remain available locally if the launcher is interrupted.

The source archive contains **tracked working-tree files** (including local edits)
plus the explicitly named benchmark profile and worker script, even if those
are not tracked yet. It excludes other untracked files, Git metadata, local build
outputs, and virtual environments. Commit the source first for reproducibility.
The job builds the pinned SuperLU 6.0.1 used in CI and downloads the dependencies
specified by the repository, then runs all configured cases/solvers. Build and
setup time is billed. Jobs use standard, non-Spot VMs; there are no automatic
retries and no long-lived container registry or images. The boot disks are deleted
with the VMs. CPU time and memory limits are per task; the light benchmark itself
uses a thread limit equal to the available physical cores for each solver (falling back to visible logical CPUs if topology is unavailable). The solver and linked libraries may use fewer threads.

For a single-core comparison, add `--single-core` to the launcher and choose a
new output directory. The worker derives a profile setting every solver's thread
limit to 1, and pins the benchmark process and its children to one available
logical CPU. The returned `profile.json` records the derived solver limits.
The returned `execution.json` records `affinity_control.mode` as `single_core`,
the original allowed CPU mask in `affinity_control.original_affinity`, and the
selected CPU in `affinity_control.selected_cpu`. The host and worker affinities
continue to record the actual pinned mask.
The report compares single-core runs carrying affinity-control metadata with
available-core runs using the same original CPU mask. For older single-core
results without that metadata, pass `--compare-affinity` to
`solver-benchmark report` alongside all their result directories. This option
shares relative baselines across affinity settings on otherwise matching
platforms. Each row displays its actual worker affinity and solver thread limit.

Only `results.jsonl`, `execution.json`, `profile.json`, and
`version-manifest.json` are returned for each machine. Matrices and worker traces
stay on the temporary VM. Each compressed result archive is capped at 10 MiB;
failed workers additionally attempt to upload at most 64 KiB of log tail, saved
locally as `failure-<target>.log`. Abrupt VM loss, setup failures, or the hard
job deadline can prevent even this diagnostic upload. Cloud Logging is not
configured by this runner. Each successful execution records the VM's actual
machine type, CPU platform, and zone from the Compute Engine metadata server in
`execution.json`. The report includes the machine type and CPU platform in its
platform label. If hardware metadata cannot be read, the job reports a failure
but still attempts to return the benchmark results and a diagnostic log. The
report groups matrices by their actual input hash;
if generators produce different bytes across architectures, those matrices
appear separately instead of being treated as identical.

### Retry a target in another zone

Machine types can be listed in a zone even when capacity is temporarily
unavailable. If a job remains queued or scheduled, inspect its Batch status
and events. When the original launcher reports its job prefix, you can cancel
the affected target while leaving the other jobs running, for example:

```sh
gcloud batch jobs cancel JOB_PREFIX-arm \
  --project YOUR_PROJECT_ID --location us-central1
```

The original launcher will return nonzero for the canceled target but will
still collect successful results from the other targets and request cleanup.
To retry only Arm in another zone, make a temporary one-target copy of the
cloud profile, run it, and combine the results:

```sh
export SB_ARM_PROFILE="$(mktemp)"
python3 - <<'PY'
import json
import os
from pathlib import Path

profile = json.loads(Path('cloud/profiles/light.json').read_text())
profile['targets'] = [target for target in profile['targets'] if target['id'] == 'arm']
Path(os.environ['SB_ARM_PROFILE']).write_text(json.dumps(profile))
PY
python3 cloud/run_gcp.py --profile "$SB_ARM_PROFILE" \
  --project YOUR_PROJECT_ID \
  --service-account YOUR_VM_ACCOUNT@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --region us-central1 --zone us-central1-f --output results/cloud-light-arm
rm -f "$SB_ARM_PROFILE"

solver-benchmark report results/cloud-light-001/intel results/cloud-light-001/amd \
  results/cloud-light-arm/arm --output reports/cloud-light-001.html
```

Check availability before choosing the alternate zone. The same technique works
for another target or the heavy profile by changing the profile path, target ID
and output path.

## Cleanup and billing

The launcher requests deletion of its Batch jobs and normally recursively deletes
its unique scratch bucket on success, failure, Ctrl-C and SIGTERM. If a result
or log cannot be collected or its existence cannot be determined, the launcher
retains the bucket for recovery and prints commands to inspect, download, and
delete its contents. `cloud-run.json` records the bucket name and whether it was
retained. Soft delete is disabled and an age-one-day lifecycle rule removes
orphaned objects; this rule does not delete the empty bucket.

Jobs and buckets receive an ownership label at creation, so the launcher can
reconcile an interrupted create without deleting a resource owned by someone
else. Batch job deletion is asynchronous. Hard kills, network/API failures or
unverifiable create requests can still leave resources behind: use the printed
job prefix and bucket name to inspect and, if needed, delete them manually. The
launcher reports cleanup failures and returns nonzero. There is no guarantee of
cleanup after the launcher itself is killed or loses credentials.

VMs, boot disks, temporary storage, operations and network egress can be billed
while jobs run. No destination objects or job definitions are intentionally kept
in GCP after normal cleanup; retained recovery buckets are an exception until
manually removed. The three result archives are capped at 30 MiB total, plus up
to 192 KiB of failure logs; retries can transfer additional bytes. Package/source
downloads are inbound to GCP. One full local light-profile run produced a 52,273-byte report-only
archive; cloud results can differ, so the cap, not that observation, is the limit.
Billing depends on actual machine duration, region, applicable networking, storage
and pricing. Inspect the project after a failed or interrupted run to avoid
unintended ongoing VM or disk charges.

For either profile, use the printed job prefix and bucket name to verify this
run after the launcher exits. Check each target's Batch job with `gcloud batch
jobs describe JOB_PREFIX-TARGET --project YOUR_PROJECT_ID --location REGION`,
and inspect `gcloud compute instances list --project YOUR_PROJECT_ID` and
`gcloud compute disks list --project YOUR_PROJECT_ID` for the run's Batch VMs
and boot disks. Check the bucket with `gcloud storage buckets describe
gs://BUCKET --project YOUR_PROJECT_ID`. A not-found response confirms absence;
an access or API error does not. Job and VM deletion can take time, so repeat
the checks until they disappear. If resources remain, confirm their identity and
ownership before deleting them; do not delete unrelated project resources. A
retained recovery bucket should be removed after downloading needed artifacts.

### Estimate

**As of November 13, 2026, 14:30 UTC**, the successful light run in `us-central1`
is estimated at **$0.09 in VM charges**, before disks, external IP addresses,
storage operations, egress, and taxes. Using the Cloud Audit Logs interval from
VM insert request to deletion completion as an approximation of VM lifetime:

| Target | Approximate lifetime | On-demand rate | Estimated VM charge |
| --- | ---: | ---: | ---: |
| Intel `n2-standard-16` | 3 min 37 sec | $0.776944/hour | $0.047 |
| AMD `t2d-standard-8` | 4 min 06 sec | $0.337968/hour | $0.023 |
| Arm `c4a-standard-8` | 3 min 16 sec | $0.3592/hour | $0.020 |

These intervals include provisioning, native builds, benchmarking, and deletion;
they are not billed-duration records. The first attempt also started Intel and
AMD VMs that failed during setup, adding approximately **$0.02** in VM charges.
The whole session is therefore estimated at **$0.11 for VMs**, plus the other
charges above. This is an estimate, not an invoice: actual billed time, rates,
discounts, and additional charges can differ. Recompressing the downloaded report
inputs produced approximately 139 KiB total; the archives from a future run are
capped at 30 MiB plus failure logs, excluding transfer retries.

The three on-demand VMs together cost about **$1.47/hour** while all are running.
The six-hour task limit is not a precise total VM-billing cap: startup, shutdown
and other resources can add charges. Check current
[Compute Engine pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[Cloud Storage pricing](https://cloud.google.com/storage/pricing), and
[network pricing](https://cloud.google.com/vpc/network-pricing) for your project
and region before running.
