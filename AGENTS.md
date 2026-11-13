# Agent instructions

## Run cloud light

When the user asks to run the light cloud benchmark (for example, “run cloud
light”), use `cloud/profiles/light.json` and follow [the cloud run guide](docs/cloud.md).
This request authorizes the billed benchmark run; follow any user-specified
project, hardware, budget, or other constraints.

1. Resolve the GCP project from the user's request or the active `gcloud`
   configuration. Find an existing VM service account that can run Batch jobs
   and that the caller can use; prefer the least-privileged suitable account.
   Do not hard-code a project or account. Ask the user only if the required
   project or account cannot be determined or access is blocked.
2. Inspect the working tree before launching. The runner packages tracked
   working-tree files and the named benchmark profile and worker script, but
   omits other untracked files. If that would omit relevant changes, tell the
   user rather than silently benchmarking different code.
3. Run the profile with a new output directory and retain the launcher's output
   so its job and bucket names are available for cleanup. Follow it to completion
   in the current task. If a machine is unavailable, preserve completed results
   and use the guide's single-target retry in another suitable zone.
4. Check all requested targets and solver outcomes, then generate and link the
   combined local HTML report and its JSON companion. The profile lists solvers
   explicitly; identify which ran and flag any available solvers it omits. Note
   failures, missing measurements, or input-hash differences that prevent direct
   comparisons.
5. Verify that this run's Batch jobs, VMs, boot disks, and scratch bucket have
   been deleted, accounting for asynchronous deletion. Report any resources
   that remain or could not be verified; do not delete unrelated resources.

The guide documents prerequisites, estimated costs, transfer limits, and cleanup.
