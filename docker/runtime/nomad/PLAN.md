# Nomad Docker deployment plan

## Agreed scope

One pack deploys fixed-identity FE and BE groups. Preserve the main containers'
upstream Docker entrypoints. Supply MySQL credentials with a Vault KV v2 template
(optionally Nomad Variables), initialize the root password with `initial_root_password`, and perform
endpoint discovery in an ephemeral prestart task.

## Implementation

1. Exercise prestart against a fake MySQL executable: existing FE metadata must
   bypass discovery; a new member must use an available elected master; no master
   must time out; partial metadata must fail; bootstrap requires a one-use permit.
2. Implement prestart using the matching Doris image. Copy its configuration to
   the allocation directory, generate `BASH_ENV` with validated addresses, and
   register new members before invoking the unchanged upstream entrypoint.
3. Render one group per stable node with pinned host volumes and host networking.
   Disable local task restart and reschedule failed allocations so prestart runs
   again. Preserve per-node image selection for deliberate one-FE-at-a-time upgrades.
4. Add an interactive credential preparation tool, examples and operating notes.
5. Run shell syntax checks, behavior tests, pack rendering and Nomad job validation.
   State explicitly whether real Doris images and a live cluster were tested.

## Boundaries

IPv4 and standard Doris ports; one FE and/or BE per Nomad client. Hostname means
the Nomad client node name, while Doris membership uses its configured IP. Storage
is pre-provisioned, persistent, and pinned to the same client. No automatic removal,
decommission, password rotation, metadata recovery, or cross-group rolling upgrade.
Use the existing checkout, with a feature branch, to avoid copying the Doris build
environment for a standalone deployment example.

## Review focus

- Lost bootstrap volume must not silently create a second cluster.
- All existing FEs must start without waiting for an elected master.
- Authentication failures and malformed membership results must not count as readiness.
- Prestart outputs must reach the unmodified main entrypoint on every new allocation.
- Passwords must not enter rendered jobspecs, shell arguments or normal log output.

## Validation record

- Implemented all five steps. Main task retains the image entrypoint; only the
  ephemeral prestart task overrides its command.
- Tests cover real pack rendering, parsed-job comparison during expansion,
  independently configured FE/BE groups, a master outside the seed list, and
  leadership changing mid-discovery. Vault templates are exercised by an isolated
  real Vault dev server and Vault Agent using synthetic secrets.
- Bash syntax, Nomad Pack formatting, and Nomad jobspec validation pass.
- Independent static review found no blocking issues; its two discovery coverage
  suggestions were added to the tests.
- No running Nomad agent or Doris 4.1.4 images were available for integration
  validation. Driver configuration, mounts, real SQL and rescheduling require the
  documented staging checks before production deployment.

## Vault and component configuration follow-up

- User's KV v2 logical path is `kv-data/doris-secret/bootstrap`, key `password`.
  Interpret `kv-data` as the mount; the template API path includes `/data/`.
- Read the static secret at task runtime. Derive FE's initial hash from exact raw
  bytes with OpenSSL in prestart, rather than storing a second hash in Vault.
- Keep the default image configs and append independent FE/BE fragments; forbid
  overriding paths and ports tied to the pack's topology.
- Nomad's existing Vault workload identity integration and the selected JWT role
  must authorize secret reads. Token renewal does not trigger Doris restarts;
  credential rotation remains an explicit operation.
