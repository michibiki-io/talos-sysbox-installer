# talos-sysbox-installer

Pinned, isolated builds of the amd64 and ARM64 Sysbox extension and standard/VMware Talos installers.

## Contents / 目次

- [English](#english)
  - [Build and image variants](#build-and-image-variants)
  - [Installation and runtime opt-in](#installation-and-runtime-opt-in)
  - [Upgrades and rollback](#upgrades-and-rollback)
  - [Proc transfer integration](#proc-transfer-integration)
  - [Backend trust and supervision](#backend-trust-and-supervision)
  - [Validation and compatibility](#validation-and-compatibility)
  - [Release and licensing](#release-and-licensing)
  - [Troubleshooting](#troubleshooting)
- [日本語](#日本語)
  - [ビルドとイメージの種類](#ビルドとイメージの種類)
  - [導入とruntimeの選択](#導入とruntimeの選択)
  - [更新とロールバック](#更新とロールバック)
  - [Proc transfer の取り込み](#proc-transfer-の取り込み)
  - [backendの特権と監督](#backendの特権と監督)
  - [検証と互換性](#検証と互換性)
  - [リリースとライセンス](#リリースとライセンス)
  - [障害調査](#障害調査)

## English

### Build and image variants

Requirements: native amd64 or ARM64 Linux matching the build target, Python 3.11+, Docker with Buildx, and HTTPS access to the pinned registries, Ubuntu snapshot and upstream release archives.
No host Go compiler or installed helper programs are used.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/build.py --version 0.7.0  # native amd64, output .build/
python3 scripts/build.py --version 0.7.0 --arch arm64 --with-probe  # native ARM64, output .build/arm64/
```

`--arch` selects the default architecture lock; `--lock FILE` selects an explicit lock, and conflicting `--arch`/lock values are rejected.
Use `--output DIR` to choose a separate output directory. Both installer variants, the extension, optional probe, validation tools and corresponding sources use the selected target.
The amd64 tag remains `v<project-version>-talos-v<talos-version>`; ARM64 appends `-arm64` for every image, including the probe.
These are separate single-platform tags. Select the release image record whose `architecture` matches the node; do not use an amd64 digest on ARM64.

New releases also provide [Standard Oracle ARM64 boot disks](docs/oracle-disks.md): compressed QCOW2 and standalone streamOptimized VMDK, each exactly 50 GiB virtual capacity, with input records and checksums. OCI boot and ARM64 Sysbox runtime remain unverified; see the linked generation/download and isolation procedures.

Test and release builds run on native runners: `ACTIONS_RUNNER` for amd64 (fallback `ubuntu-latest`) and `ACTIONS_RUNNER_ARM64` for ARM64 (fallback `ubuntu-24.04-arm`). Fork PRs always use the corresponding GitHub-hosted runner.
Build-embedded supervisor/unit/upstream checks, including kernel/seccomp checks, helper rejection and version identity checks retain identical fail-closed rules on both targets.
They do not establish Talos runtime behavior. **ARM64 runtime verification is UNRUN and is performed separately.**
Host clients installed by `scripts/install_test_tools.py` follow the host architecture; tools copied into validation Pods and helm archive paths follow the selected target lock.
For a separate ARM64 runtime run, pass `--lock versions.arm64.lock.json` to the existing validation harness and use the ARM64 build metadata. Do not reuse amd64 runtime evidence.

A new automated release combines both native outputs into schema-v2 `images.json` (`architectures.amd64` and `architectures.arm64`), while each original per-target input remains intact.
GitHub Release assets use `amd64-`/`arm64-` prefixes for `extension.tar`, `standard-image.tar`, `vmware-image.tar`, `probe-image.tar`, `sources.tar.gz` and `build-metadata.json`.
`release-images.json` and `approved-runtime-images.json` list both targets; probe distribution locks are `amd64-test-images.lock.json` and `arm64-test-images.lock.json`.
The serialized release retains both source bundles before binary pushes, checks all tags before any push, and restores exact original archives on rerun. Legacy single-target amd64 releases remain verifiable without rebuilding.

The locked Go and Ubuntu images build a static supervisor and collect shell, rsync, modprobe, iptables, fusermount3 and their libraries inside a container.
The three Sysbox binaries are rebuilt from the release's exact component commits using read-only, checksum-verified Go module resolution and the locked builder's static C libraries.
GNU linker maps identify the actual linked static libraries so their matching sources and licenses accompany the application source and rebuild scripts.
The checksum-verified upstream package supplies reference release documentation, not host-derived executables.
Ubuntu's signed snapshot fixes package resolution; upstream archives have SHA-256 locks. APT operations retry downloads and then the same operation at most three times on failure, without changing the snapshot or disabling signature verification.
Each operation allows three download retries, 30-second HTTP/HTTPS timeouts, and two 5-second waits between the three operation attempts. Persistent failures still stop the build. The `dependencies` Docker target permits checking snapshot indexes and build dependencies without building the installers.
Official source URLs follow the pinned upstream recipes, including the HTTP libaio/attr/popt URLs; SHA-256 verification remains mandatory before caching or export. Archive downloads retry only the explicitly classified transient DNS/timeout/reset/HTTP errors, at most three total attempts. Other errors stop the build; an incomplete file may remain in the failed build workspace but cannot enter the verified cache or successful exports. The APT retry policy above is separate.
Build inputs are in [versions.lock.json](versions.lock.json) for amd64 and [versions.arm64.lock.json](versions.arm64.lock.json) for ARM64.
The context physically contains only `build/`, `supervisor/`, the lock and the project license.
It never includes local configs, logs or state.
For hosts without a working default Docker bridge, `--network host` is an explicit local build option; it does not change Docker configuration.
Select an existing Buildx instance with `--builder NAME`.

| Image | Extension set |
| --- | --- |
| `ghcr.io/michibiki-io/talos-sysbox-extension` | Sysbox 0.7.1 and supervised privileged node backends |
| `ghcr.io/michibiki-io/talos-sysbox-installer-standard` | Sysbox, iscsi-tools, util-linux-tools |
| `ghcr.io/michibiki-io/talos-sysbox-installer-vmware` | Standard set plus vmtoolsd-guest-agent |

amd64 tags have the form `v<project-version>-talos-v<talos-version>`, for example `v0.7.0-talos-v1.14.5`; ARM64 tags append `-arm64`.
Project semver describes this repository; Talos and Sysbox retain their independent upstream versions.
Official extensions are consumed directly by digest from the catalogue for the selected Talos release.
The custom extension is passed to imager through an OCI layout, avoiding a local insecure registry.
Both the custom profile and the explicit base installer are pinned.
Generated archives are retagged before loading so they never overwrite or publish a SideroLabs image name.
Local composition records label the custom manifest as `oci-layout@sha256:…`.
Publication replaces that local reference with the anonymously verified GHCR manifest digest; the two digests need not match.

Outputs under `.build/` (amd64) or `.build/arm64/` (ARM64) include all three loaded images, `images.json`, each installer's `inputs.json`, `evidence/build-metadata.json` and `sources.tar.gz`.
To customize the shared extension list, copy the lock, edit `standard_extensions` and add catalogue-approved digest pins to `extensions`, then use `--lock FILE`.
The VMware agent is reserved for the VMware variant.
Build and source-inventory JSON use stable key ordering. Fixed inputs and repeatable runtime payloads do not guarantee identical OCI/image digests across builders: image/layer timestamps and exporter details may differ. Preserve original release archives for retries rather than rebuilding an existing version tag.
Locked archive and Go-module downloads use at most three attempts for selected transient network failures, retaining the same URLs and checksum verification. Authentication, certificate verification and checksum errors fail immediately. Only verified archive bytes enter the build cache.
Use the same `--lock FILE` for either runtime validation runner; the selected lock is included as `project/versions.lock.json` in the corresponding-source archive.
Talos and talosctl target 1.14.5. `test_tools.outer_kubectl` pins kubectl 1.37.1 for the outer Kubernetes 1.37.1 API; `test_tools.kubectl` pins kubectl 1.35.2 for nested kind 1.35.0. Docker 27.5.1, kind 0.31.0 and Helm 3.19.0 retain their pins because this combination passed the native amd64 feasibility spike. This does not replace final-build validation. Host clients use the executor architecture, while nested tools use the target-node architecture.
CI sends fork PRs to GitHub-hosted runners. A configured `ACTIONS_RUNNER` or `ACTIONS_RUNNER_ARM64` override for trusted branches must identify a disposable, isolated runner with no production credentials or workloads.
Any new Talos release requires explicit compatibility validation before changing the manifest constraint.

### Installation and runtime opt-in

Use a digest from the actual release's `release-images.json` for installation and upgrades.
Verify the target architecture, full extension list and target Talos version before a maintenance window.
Choose the VMware installer only on VMware: the upstream agent restarts continuously and rejects other platforms.
New-node installation uses `machine.install.image` in the operator's private Talos machine configuration.
This repository supplies no cluster credentials or embedded machine config.

Fresh Talos 1.14.5 registration uses a named `CRICustomizationConfig` and a `SysctlConfig` enabling user namespaces. This sysctl applies node-wide. Stock `user.max_user_namespaces=0` prevents Sysbox manager startup; the generated configuration sets 11255 (override with `--max-user-namespaces`). Retain `SecurityProfileConfig.workloadIsolation=true`; the generator does not change that setting.

Inspect all existing CRI customizations, including legacy `/etc/cri/conf.d/20-customization.part`, and confirm there is no `sysbox` named document or `sysbox-runc` handler. Named documents are merged in memory; inspect active MachineConfig (`get machineconfig v1alpha1`, namespace `config`) and the rendered `/etc/cri/conf.d/cri.toml`, rather than expecting a physical named `.part` file. An unqualified MachineConfig query may also return the separate persistent resource. Supply the relevant current TOML fragment for duplicate-handler preflight; an empty file is appropriate only after confirming fresh registration. The output adds only the Sysbox handler and leaves other fragments/default handlers intact:

```sh
python3 scripts/runtime_patch.py --input-fragment existing.part \
  --no-existing-customization --output runtime.patch.yaml
talosctl --talosconfig CONFIG --context CONTEXT --endpoints ENDPOINT --nodes NODE \
  patch machineconfig --patch @runtime.patch.yaml
```

Review the output and target before applying. The generator refuses an existing handler and an existing output file; output mode is 0600. The acknowledgement covers customizations outside the supplied fragment, which this offline generator cannot inspect. Talos automatically restarts CRI when its customization changes, so apply it within the authorized disposable/maintenance scope and verify service recovery. The handler retains `io.containerd.runc.v2`, `/etc/cri/conf.d/base-spec.json`, `/usr/local/bin/sysbox-runc` and `SystemdCgroup=false`. Its name is `sysbox-runc`; the default runtime remains unchanged.

The older `machine.files` generator/flags are replaced. That registration route is UNRUN on 1.14.5; reconcile/remove its Sysbox definition before using the named document. Existing-cluster migration, rollback and reboot procedures are not validated by this change.

With an explicit kubeconfig and context, apply [RuntimeClass](examples/runtimeclass.yaml), then create [the Docker Pod](examples/docker.yaml) in an authorized namespace.
The namespace must admit this workload's user-namespace root capabilities; it is not a Restricted Pod Security workload.
Only Pods specifying `runtimeClassName: sysbox-runc` use Sysbox.
The example uses `hostUsers: false`, UID/GID 0, `privileged: false`, and no service-account token.
Run Docker directly with overlay2 and cgroupfs; privileged Docker children and kind nodes remain within the outer user namespace.
The validated design does not require Podman, manual cgroup delegation, a sysfs adapter, an explicit FUSE device request, a custom Pod seccomp profile, or the `KubeletInUserNamespace` feature gate.

### Upgrades and rollback

The following are general operator considerations, **UNRUN for existing-node upgrade/rollback paths on Talos 1.14.5**. This change validates fresh runtime configuration on an authorized sandbox, not a production migration path.

Installed extensions are not automatically carried over by Talos upgrades.
The selected installer must include **every extension the node still needs**, including extensions beyond this repository's default set.
Record the current OS version, installer digest and extension list privately before upgrading.
Back up recovery configs outside the build context, check storage/PDB drain requirements, and follow the applicable Talos upgrade procedure for the installed version.
Do not automatically relax PDBs or skip maintenance approval.

An operator-authorized upgrade uses the chosen digest with explicit `talosctl --talosconfig CONFIG --endpoints ENDPOINT --nodes NODE upgrade --image IMAGE@sha256:DIGEST`.
After boot, check extension services and repeat functional validation.
Rollback uses the previously recorded complete installer digest or the applicable Talos rollback procedure, with the same explicit target and authorization.
Recheck runtime registration, ordinary Pods and Sysbox Pods after rollback.
Removing Sysbox requires migrating/deleting opted-in workloads and removing its handler and RuntimeClass during an approved maintenance operation.

### Proc transfer integration

The protected nested procfs/sysfs integration pins public upstream commit `122de40f8a8ad70973f827fe59ab9d8a60478ded` in [build/proc-transfer.lock.json](build/proc-transfer.lock.json). The archive SHA-256 is `4ecfeffb4e3f3dc56e819da881de05a263573160918aa058ab147e4c8197a67e` (55 files).

The merged upstream #11 / PR #12 update corrects the shared sysfs oracle and its callers, with 27 selected `tests/static/test_sysfs_oracle.py` regressions. Fresh mounts use `check_fresh_network()` for both class/net and virtual-net; inherited interleave/dentry use `check_fuse_network()` for the per-request virtual-net FUSE view. Inherited native class/net retains its mount-time Network. Write-policy targets only protected FUSE virtual-net, retaining network-owner preflight, nobody ownership, write denial and unchanged MTU. Independent rtnetlink observations allow additional default interfaces and the exact regular `bonding_masters` attribute while retaining security assertions; all four production helper/patch inputs remain byte-identical.

Standalone runc/mgr compile before patching; fs and both static production helpers build as a verified triple with profile `proc-sysfs-fuse-v1` and patched local bazil. The same fs backend receives both fixed helper environments/paths. See [source updates, activation and operator verification](docs/proc-transfer.md) for the separate validation artifacts, post-merge public-digest probe distribution and explicitly unpublished local bwrap 0.12.0 archive route.

The unchanged v0.4.1 actual-Talos suite remains FAILED at inherited interleave after passing proc/protection and eight fresh-sysfs cases; later checks were UNRUN. The separate FUSE diagnostic and upstream static passes are inherited evidence only. The changed revision changes embedded fs identity and requires new output verification. Official publication, anonymous retrieval and the three unavailable separate fixtures remain UNRUN. The local Standard amd64 candidate has separate [Talos 1.14.5 native evidence](docs/test-evidence/issue-19-native.json) and [79-check proc/sysfs results](docs/test-evidence/issue-19-native-proc.json); these do not establish public release availability.

### Backend trust and supervision

Extension services live under `/usr/local/lib/containers/sysbox-mgr` and `sysbox-fs`.
Their static supervisors talk to **CRI containerd** at `/run/containerd/containerd.sock`. Talos 1.14 workload isolation gives CRI a PID namespace distinct from the initial host. Sysbox-runc's PID values originate there; mgr/fs therefore join that same runtime PID context. They retain the initial User namespace and share `/run`, `/var` and `/dev` with shared mount propagation.
Only namespace `talos-sysbox` and exact owned task/container IDs `sysbox-mgr` and `sysbox-fs` are cleaned up. The supervisor does not expose arbitrary commands. These privileged backends and the CRI socket belong to the trusted node runtime.
An outer non-privileged Pod does not make those node components unprivileged or certify isolation.

Startup waits for connectable sockets with a 60-second deadline; the filesystem daemon additionally waits for the manager socket.
Each ctr control operation has a 5-second limit.
On TERM/INT the supervisor sends TERM to its backend, waits at most 10 seconds, then escalates to KILL and removes only its own task/container.
Talos `restart: always` launches a fresh supervised instance after failure; bounded stale cleanup prevents overlapping owned tasks.
Manager runs with `--disable-shiftfs`; helper rootfs identifies itself as Talos.

### Validation and compatibility

The manifest targets **Talos 1.14.5**, Sysbox 0.7.1 and the selected amd64/arm64 target.
The native amd64 feasibility baseline is kernel 6.18.58-talos, outer Kubernetes 1.37.1 and containerd 2.3.7, with workload isolation enabled. [Spike evidence](docs/test-evidence/issue-19-spike.json) uses unchanged published Sysbox binaries with an experimental supervisor and is not final-build evidence.
Validation uses pinned talosctl 1.14.5 and outer kubectl 1.37.1; nested kind uses kind 0.31.0, kindest/node 1.35.0, kubectl 1.35.2, Docker 27.5.1 and Helm 3.19.0.
The rebuilt local Standard amd64 candidate passed installation/reboot, fresh runtime configuration after removing spike-only settings, all 79 proc/sysfs checks, owned mgr/fs restart followed by Docker/privileged child/kind/Helm/internal DNS-TCP/upstream DNS, container recreation and ordinary-Pod security checks. [Native evidence](docs/test-evidence/issue-19-native.json) binds installed hashes and results to producer source inputs; a changed build requires new validation. This was a configuration reset on an existing sandbox, not a fresh OS installation or production upgrade-path test. Owned namespaces and RuntimeClass were removed; sysboxfs had no residual directories. Three separate proc/sysfs fixtures remain UNRUN.
Arm64, SELinux enforcing and VMware guest-agent runtime behavior are unvalidated unless release-specific evidence explicitly records them.

PR CI runs static/unit checks and builds both variants plus the probe for both architectures. Supporting **Talos-in-Docker** validation is retained behind the Test workflow's explicit `workflow_dispatch` input `run_runtime=true` (default false); it never runs on PRs. This update does not trigger it; record it as UNRUN unless separately executed. A manual run creates an owned cluster using the host kernel.
It never connects to an existing real cluster.
This mode is not real Talos-kernel coverage and does not boot the installer.
On a disk-constrained Docker host, `--constrained-disk` changes only the disposable node's eviction and image-GC thresholds (5% free disk, 95% high watermark).
This option is never used on real Talos targets.
Run it locally only when Docker networking, resources and required host kernel facilities are available:
The host's `br_netfilter` module must already be loaded; the local harness refuses to change host kernel modules.
An operator may explicitly authorize a temporary load/unload, while an explicitly requested runtime CI run loads it only on its disposable runner.

```sh
python3 test/e2e/scripts/talos_docker.py \
  --rootfs .build/evidence/rootfs --subnet TEST_SUBNET
```

Real Talos validation needs an explicitly authorized target with the newly built installer already installed and the runtime registered.
The runner itself does not upgrade or reboot nodes:

```sh
python3 scripts/validate_runtime.py \
  --kubeconfig TEST_KUBECONFIG --context TEST_CONTEXT \
  --expected-node AUTHORIZED_NODE --environment real-talos \
  --build-metadata .build/evidence/build-metadata.json \
  --talosconfig TEST_TALOSCONFIG --talos-context TEST_TALOS_CONTEXT \
  --endpoint AUTHORIZED_ENDPOINT --node-address AUTHORIZED_ADDRESS \
  --result test/e2e/results/real-talos.json
```

It checks direct Docker, a privileged child, kind Ready, CoreDNS readiness, Helm rollout, internal Service DNS/TCP, upstream DNS, a changed workload container ID followed by successful child startup, and an ordinary UID1001/CapEff0/NNP1/seccomp2 default-runtime Pod.
Nested kind defaults to Pod `10.200.0.0/16` and Service `10.201.0.0/16`. Before mutation, the runner reads the controller-manager `--cluster-cidr` ranges, verifies node Pod CIDRs belong to them, and reads Kubernetes ServiceCIDRs; it then checks the inner Docker networks too. Overlap stops the run; select different ranges with `--kind-pod-subnet` and `--kind-service-subnet`. It requires controller-manager cluster-CIDR flags and read access to those Pods in `kube-system` and ServiceCIDRs, and rejects clients that do not match the executor-architecture pins.
Before final-candidate validation, remove SPIKE-only sysctl/CRI/RuntimeClass configuration (or provision anew), install the final candidate, and apply the documented configuration from its declared prerequisites. Record the method, resulting configuration and final build/input/binary/supervisor hashes. A configuration reset is not a fresh OS installation.
Before mutating the test namespace, it verifies the installed three Sysbox binaries, production proc helper and both supervisors against this build's SHA-256 values using explicit Talos API reads.
It also refuses artifacts whose recorded source inputs differ from the current checkout, preventing prior prototype results from being attributed to a changed build.
It creates one unique owned namespace and deletes it even on failure.
It preserves the user's default kubeconfig.
Public results contain version/kernel information and boolean checks, without node names, addresses, IDs, configs or raw logs.

For service/backend restart and node reboot recovery, obtain separate approval for the exact target and operations.
Record the installed build's `supervisor_sha256` and binary hashes from `build-metadata.json`, verify the installed files match, and retain a private mapping to the public result.
Restart each extension service, terminate each exact owned backend via CRI containerd, and reboot the node in separate approved steps.
After each step, wait for both services, repeat Docker-child and ordinary-Pod probes, and record pass/fail plus the artifact digest in a sanitized result.
Verify CRI PID-context identity and shared mounts from the backend task's OCI spec.
Never clean unrelated containerd namespaces/tasks or publish raw service logs.
VMware functionality requires a separate authorized VMware target; verify the agent is healthy and guest information is reported through VMware tools before claiming coverage.

### Release and licensing

Release automation starts on pushes to the current default branch and requires an API-confirmed merged `feature/` PR whose merge commit exactly matches the pushed SHA. Direct pushes and other branches cannot publish; no `pull_request_target` policy exception is needed.
Only API-confirmed release candidates enter the publication concurrency group; metadata calculation, tagging and publication are serialized together. Noncandidate pushes cannot replace a pending release, including case variants of the default-branch name. A later eligible candidate can replace another pending candidate in GitHub’s concurrency group; commits of the replaced PR are not individually classified for a version. A fresh release stops if a descendant commit already has a semver tag; recovery of an existing tag on the exact same commit remains allowed. When there is no reachable version tag, the initial release version is `v0.2.0`. Release gating assumes trusted repository write collaborators; job conditions are not a security boundary against someone editing workflow files. Each push evaluates the merged PR at its tip SHA; combining multiple PR merges in one push is outside this release contract.
It derives semver and release notes from individual commit subjects/bodies, including breaking-change markers, without using PR titles for classification.
PR builds have read-only repository permissions and no publication credentials.
Post-merge publication uses the existing GitHub Actions token and fails unless all selected GHCR images, including the optional probe, are anonymously readable.
All tags are checked before publication, then all missing images are pushed before any anonymous check. An owner must make all selected newly created GHCR packages public using their existing permissions, then rerun the workflow to verify the original tags without rebuilding or repushing. A failed anonymous check is not a successful release.
This repository does not add authentication settings or buy credentials/credits.

Releases attach image digests, effective inputs, package/source checksums and corresponding sources.
The extension retains third-party license texts, package copyright files and applicable NOTICE files under `/usr/local/share/licenses/talos-sysbox`.
`sources.tar.gz` includes matching Sysbox/ctr sources, Talos 1.14.5 and Linux 6.18.58 sources, the official iSCSI/util-linux extensions, the VMware-only guest agent, checksum-verified Go modules, and matching Ubuntu source packages. The pinned SideroLabs Talos/pkgs/extensions/tools/toolchain repositories provide kernel configuration, patches and rebuild recipes. `build/official-sources.lock.json` binds this inventory to the installer and extension digests; changing those pins requires a matching source inventory update. All three images use this common source bundle, with the VMware agent present only in the VMware installer. The pinned catalogue is checked before assembly. ctr is rebuilt from vendored sources with CGO disabled, and its Go build information is recorded. Sources are attached before image publication; distribute it alongside the binaries and keep it available for the corresponding release.
The project MIT license does not replace Apache-2.0, BSD, GPL or LGPL licenses of bundled software.
Registry login and `publish.py --preflight-only` check the classic Docker image store, loaded archive config digests and all existing tags before staging a release. Auth/transport failures stop without creating a release. Configuration digests come from saved archive config bytes, not Docker image IDs; a containerd image store is explicitly unsupported for a fresh or resumed push.
The release retains `extension.tar`, `standard-image.tar` and `vmware-image.tar`, plus `probe-image.tar` when selected, with whole-archive SHA-256 values in `images.json`. All original archives, matching sources and provenance are attached to a draft before `images.json` is attached last as its completion marker. Only an incomplete draft without that marker can have its unfinished assets replaced. Matching source assets are made public before any GHCR push, including later releases whose packages are already public.
If staging fails before the marker, rerun to rebuild and finish that unpublished draft after preflight. If login, preflight or a push fails after the marker, rerun to download and checksum/config/tag-verify all original archives before loading them; push only missing tags, never rebuild or replace existing tags/source assets. A preexisting tag with a different config digest still stops before any push. If anonymous checks fail, make all selected packages public and rerun. A completed release with `release-images.json` is only verified using its original inputs and published digests, without loading archives, rebuilding or repushing. Missing/corrupt original assets stop explicitly; restore the exact original bytes rather than replacing the release or its tags.
LGPL library replacement remains possible by rebuilding the helper rootfs from the supplied source/build inputs.

### Troubleshooting

| Symptom | Checks |
| --- | --- |
| Builder DNS/bridge error | Check the selected builder and Docker network; use an existing working builder or the explicit host-network build option without altering global settings. |
| Checksum mismatch | Stop; independently investigate upstream assets before editing a lock. |
| RuntimeClass not found / handler failure | Verify both the Kubernetes RuntimeClass and merged CRI fragment, without changing the default handler. |
| Service restarts | Check private `ext-sysbox-mgr`/`ext-sysbox-fs` logs, CRI-containerd availability, socket readiness and exact owned task IDs. |
| Docker child/kind failure | Verify actual installed artifact, PID/mount propagation, cgroup v2 and kernel facilities; compare sanitized results to the correct environment. |
| VMware agent restarts on bare metal | Select the standard installer during an approved upgrade. |
| Anonymous GHCR check fails | Confirm package visibility with the owner; publishing credentials do not prove anonymous access. |

Public upstream references: [Sysbox release](https://github.com/nestybox/sysbox/releases/tag/v0.7.1), [Sysbox design](https://github.com/nestybox/sysbox/blob/v0.7.1/docs/user-guide/design.md), [Talos extension services](https://docs.siderolabs.com/talos/v1.14/build-and-extend-talos/custom-images-and-development/extension-services), [official extensions](https://github.com/siderolabs/extensions), [VMware agent](https://github.com/siderolabs/talos-vmtoolsd), [Ubuntu snapshots](https://snapshot.ubuntu.com/).

## 日本語

### ビルドとイメージの種類

対象に一致する native amd64 または ARM64 Linux、Python 3.11 以降、Buildx 付き Docker、固定した registry と Ubuntu snapshot と上流アーカイブへの HTTPS 接続を使用します。
ホストの Go compiler や helper は使用しません。
英語節のビルドコマンドで、固定した Go と Ubuntu のコンテナ内から static supervisor、shell、rsync、modprobe、iptables、fusermount3 と共有ライブラリを生成します。
ビルド入力は amd64 用の `versions.lock.json` と ARM64 用の `versions.arm64.lock.json` にあり、署名付き Ubuntu snapshot と上流アーカイブの SHA-256 を検証します。APT はダウンロードを再試行し、失敗時には同じ操作を最大3回試します。snapshot の変更や署名検証の無効化は行いません。
各操作はダウンロード再試行3回、HTTP／HTTPS timeout 30秒、操作の試行間に5秒の待機2回を設定します。恒久障害時はビルドを停止します。Docker の `dependencies` target で、installer を構築せずに snapshot index とビルド依存を確認できます。
公式ソースの URL は固定した上流 recipe に合わせ、libaio／attr／popt の HTTP URL もそのまま記録しています。cache と export の前に SHA-256 を必ず検証します。archive 取得の再試行は明示した一時的な DNS／timeout／reset／HTTP エラーだけを対象とし、合計3試行までです。それ以外はビルドを停止し、未完了ファイルが失敗した作業領域に残っても、検証済み cache や成功成果物には入りません。上記の APT 再試行規則とは別です。
Sysbox の3つの binary は release が固定した component commit、読み取り専用の module 解決と checksum 検証で取得する Go 依存、固定 builder の static C library から再ビルドします。
GNU linker map で実際に結合された static library を特定し、その対応ソースとライセンスを application source と再ビルド手順と一緒に提供します。
検証した上流 package はリリース文書の参照に使い、ホスト由来の実行ファイルは使用しません。
ビルドコンテキストには必要なソースと lock と LICENSE だけを集め、設定、ログ、秘密情報を除外します。
既定 Docker bridge が動かないホストでは、明示した `--network host` を使用できます。
Docker の恒久設定は変更しません。
既存 Buildx instance は `--builder NAME` で選びます。

自作 extension は Sysbox とノード backend を含みます。
共通版 installer はそれに iscsi-tools と util-linux-tools を加え、VMware 版だけ vmtoolsd-guest-agent を加えます。
イメージ名は英語節の表を参照してください。
amd64 のタグは `v<project-version>-talos-v<talos-version>`、ARM64 は末尾に `-arm64` を付けます。
probe を含むすべてのイメージをアーキテクチャ別の単一 platform tag に分けます。

新しいリリースでは [Standard Oracle ARM64 起動ディスク](docs/oracle-disks.md) も配布します。圧縮 QCOW2 と standalone streamOptimized VMDK の仮想容量は各 50 GiB で、入力記録とチェックサムを併配します。生成と取得の手順、特権の範囲、未実施の OCI 起動と ARM64 Sysbox runtime 検証はリンク先を参照してください。
プロジェクトの semantic version、Talos 版、Sysbox 版は別々に管理します。
公式 extension は対象 Talos の catalogue にある digest を直接使用します。
自作 extension は OCI layout として imager に渡すため、ローカル insecure registry を必要としません。
生成した installer archive は読み込む前にこのプロジェクトの名前へ変換します。
ローカルの構成記録は自作 manifest を `oci-layout@sha256:…` として区別します。
公開時は匿名取得を確認した GHCR manifest digest に置き換えます。両者の digest は一致するとは限りません。

amd64 は `.build/`、ARM64 は `.build/arm64/` に3つのイメージ、`images.json`、installer ごとの `inputs.json`、`evidence/build-metadata.json`、`sources.tar.gz` を生成します。
構成変更は lock のコピーで `standard_extensions` と対応する digest を編集し、`--lock FILE` で指定します。
VMware agent は VMware 版だけに含めます。
ビルドと source inventory の JSON は key 順序を固定します。固定入力と同じ runtime payload を再生成できることは、builder 間の OCI／image digest の完全一致を保証しません。image／layer の timestamp や exporter の違いが残るため、既存 version tag の再ビルドではなく元の release archive を使って復旧します。
runtime 検証 runner にも同じ `--lock FILE` を指定します。対応ソースの archive には、選択した lock を `project/versions.lock.json` として保存します。
固定アーカイブと Go module の取得では、一時的なネットワーク障害に対して最大3回の取得試行を行います。
URL と checksum 検証を維持し、認証、証明書検証、checksum のエラーでは直ちに停止します。
アーカイブのキャッシュには、検証済みの内容だけを保存します。

Talos と talosctl は 1.14.5、外側 Kubernetes は 1.37.1 を対象にします。
`test_tools.outer_kubectl` は外側 API 用の kubectl 1.37.1、`test_tools.kubectl` は内側 kind 1.35.0 用の kubectl 1.35.2 です。
Docker 27.5.1、kind 0.31.0、Helm 3.19.0 は native amd64 の spike で組み合わせを確認したため固定版を維持します。
spike の成功は最終成果物の検証を代替しません。
CI は fork PR を GitHub-hosted runner で実行します。信頼したブランチで `ACTIONS_RUNNER` または `ACTIONS_RUNNER_ARM64` を指定する場合も、production 資格情報や workload を持たない隔離された使い捨て runner に限ります。
Talos 互換指定を広げる前に、新しい版で検証してください。

ARM64 ホストでは `python3 scripts/build.py --version 0.7.0 --arch arm64 --with-probe` でビルドします。
`--arch` で既定の lock を選び、`--lock FILE` で明示した lock と対象が異なる場合は拒否します。
出力先は `--output DIR` で分離できます。
Test とリリースのビルドは native runner を使い、amd64 は `ACTIONS_RUNNER`（既定 `ubuntu-latest`）、ARM64 は `ACTIONS_RUNNER_ARM64`（既定 `ubuntu-24.04-arm`）で指定します。
fork PR は各アーキテクチャの GitHub-hosted runner で実行します。
ビルド内の supervisor 単体テスト、kernel／seccomp を含む選択上流検査、helper 拒否確認、identity 照合は両対象で同じ fail-closed 条件を維持します。
これらの成功は Talos 上の動作を証明しません。ARM64 の動作検証は UNRUN で、別途実施します。
ホスト用 client はホストのアーキテクチャ、Pod 内で動かすツールと helm archive path は対象 lock で選びます。
別途 ARM64 の検証を行う場合、既存ハーネスへ `--lock versions.arm64.lock.json` と ARM64 の build metadata を指定します。

新しい自動リリースの `images.json` は schema v2 の `architectures.amd64` と `architectures.arm64` に元の入力を保持します。
Release asset の `extension.tar`、`standard-image.tar`、`vmware-image.tar`、`probe-image.tar`、`sources.tar.gz`、`build-metadata.json` には `amd64-` または `arm64-` を付けます。
`release-images.json` と `approved-runtime-images.json` は両対象を記録し、probe lock は `amd64-test-images.lock.json` と `arm64-test-images.lock.json` に分けます。
公開は直列化し、両方の対応ソースを公開してから binary を push します。
全 tag の事前検査と元 archive による再実行を維持し、従来の amd64 リリースも再ビルドせず検証できます。
対象ノードと同じ `architecture` の digest を選択してください。


### 導入とruntimeの選択

実際のリリースの `release-images.json` にある digest を使用し、対象 Talos 版、ノードのアーキテクチャ、必要な extension 全件を保守作業前に確認します。
VMware agent は VMware 以外で失敗して再起動するため、VMware 版は VMware 上で使用します。
新規ノードの `machine.install.image` は運用者の非公開 machine config に設定します。
このリポジトリはクラスタ認証情報や埋め込み設定を提供しません。

Talos 1.14.5 の新規 runtime 登録には、名前付き `CRICustomizationConfig` と user namespace を有効にする `SysctlConfig` を使います。
この sysctl はノード全体に適用されます。
既定値 `user.max_user_namespaces=0` では manager が起動できないため、生成する設定は 11255 にします。
必要な値は `--max-user-namespaces` で指定できます。
`SecurityProfileConfig.workloadIsolation=true` を維持し、生成器はその値を変更しません。

既存の全 CRI customization と旧 `/etc/cri/conf.d/20-customization.part` を調べ、`sysbox` という名前の document と `sysbox-runc` handler がないことを確認します。
名前付き document はメモリ上でマージされるため、物理的な `.part` ファイルの有無だけで判断せず、active MachineConfig（namespace `config` の `get machineconfig v1alpha1`）と生成済みの `/etc/cri/conf.d/cri.toml` を確認します。
ID を省略すると、別の persistent resource も返る場合があります。
現在の関連 TOML fragment を入力として、英語節の `--no-existing-customization` コマンドで設定を生成します。
空の入力は新規登録を確認した場合だけ使用します。
生成器が検査できるのは渡した fragment だけで、他の設定の不存在は運用者が確認します。
出力は Sysbox 専用 handler だけを追加し、既存 fragment を置き換えません。
既存 handler と出力ファイルの上書きを拒否し、出力の mode は 0600 です。

対象と出力を確認してから、Talos config、context、endpoint、node を明示して適用します。
customization の変更で CRI が自動再起動するため、許可された sandbox または保守作業の範囲で適用し、サービスの復旧を確認します。
handler は `io.containerd.runc.v2`、`/etc/cri/conf.d/base-spec.json`、`/usr/local/bin/sysbox-runc`、`SystemdCgroup=false` を維持します。
既定 runtime の選択は変更しません。

旧 `machine.files` 用の生成器と flag は置き換えました。
旧方式は 1.14.5 では UNRUN です。
名前付き document を使う前に、旧方式の Sysbox 定義を照合して取り除き、重複を避けます。
既存クラスタの移行、ロールバック、再起動の運用は今回の検証対象外です。

明示した kubeconfig と context で RuntimeClass と Docker Pod 例を適用します。
外側 Pod は `hostUsers: false`、UID/GID 0、`privileged: false`、API token なしで、`runtimeClassName: sysbox-runc` を指定した Pod だけ Sysbox を使用します。
Pod 内の Docker は overlay2 と cgroupfs を使用します。
Docker 子コンテナと kind の特権は外側の user namespace 内にあります。
user namespace 内の root capability を許可できる namespace が必要で、Restricted Pod Security profile ではありません。
この設計の基準では Podman、手動 cgroup 委譲、sysfs adapter、FUSE device resource、独自 seccomp profile、KubeletInUserNamespace 設定を必要としません。

### 更新とロールバック

以下は一般的な運用上の確認事項で、Talos 1.14.5 の既存ノード更新とロールバックの経路は UNRUN です。
今回確認するのは許可された sandbox での新規 runtime 設定です。

Talos の更新では、既存 extension は自動で引き継がれません。
次の installer に、ノードが必要とする extension をすべて含めてください。
このリポジトリの既定セット以外も対象です。
現在の OS 版、installer digest、extension 一覧を非公開で保存し、復旧設定をビルドコンテキストの外へ退避します。
PDB やストレージの drain 条件を確認し、対象 Talos 版の更新手順と保守承認に従います。
PDB を自動で緩和しません。

英語節のように Talos config、endpoint、node、image digest を明示して更新します。
起動後は extension services と runtime を検証します。
ロールバックには記録した完全な installer digest または対象版の Talos rollback 手順を使用し、対象と操作の承認を得ます。
通常 Pod と Sysbox Pod の復旧も確認します。
Sysbox の削除は、使用中の Pod の移行と handler と RuntimeClass の除去を含む保守作業です。

### Proc transfer の取り込み

公開 upstream の commit `122de40f8a8ad70973f827fe59ab9d8a60478ded` を [build/proc-transfer.lock.json](build/proc-transfer.lock.json) で固定します。archive SHA-256 は `4ecfeffb4e3f3dc56e819da881de05a263573160918aa058ab147e4c8197a67e`（55ファイル）です。

マージ済みの上流 #11／PR #12 は共有 sysfs oracle と caller を修正し、`tests/static/test_sysfs_oracle.py` の27回帰検査を含みます。新規 mount は `check_fresh_network()` で class/net と virtual-net の両 view を検査します。継承 interleave／dentry は `check_fuse_network()` で要求ごとの virtual-net FUSE view を検査し、native class/net は mount 時の Network を保持します。write-policy は保護された FUSE virtual-net を対象とし、Network 所有の事前確認、nobody 所有、書き込み拒否、MTU 不変を維持します。独立した rtnetlink 観測により追加の既定 interface と正確な通常属性 `bonding_masters` を許容し、保護検査と production helper／patch 4入力の bytes を維持します。

単体 runc/mgr は patch 前にコンパイルし、fs と static production helper 2個を `proc-sysfs-fuse-v1` の triple として検証し、local bazil も patch します。同じ実 fs backend に両 helper の環境変数と path を明示します。[更新、有効化、運用者検証](docs/proc-transfer.md) に、production から分離する検証用成果物、merge 後の公開 digest probe 配布、未公開の local bwrap 0.12.0 archive 経路を記載します。

変更していない v0.4.1 の実 Talos suite は proc／保護検査と新規 sysfs 8条件の成功後、継承 interleave で FAILED となり、後続検査は UNRUN でした。別実行の FUSE 診断と上流 static 検査の成功は継承した参考情報です。revision 更新で fs の埋め込み identity が変わるため、新成果物の照合が必要です。変更後の正式成果物の公開／匿名取得と、環境がない別 fixture 3分類は UNRUN です。ローカル Standard amd64 候補の [Talos 1.14.5 実測](docs/test-evidence/issue-19-native.json) と [proc/sysfs 79項目の結果](docs/test-evidence/issue-19-native-proc.json) は別に記録し、正式公開の証拠とは区別します。

### backendの特権と監督

extension service の supervisor は CRI containerd socket `/run/containerd/containerd.sock` を使用します。
Talos 1.14 の workload isolation により、CRI の PID namespace は初期ホストから分離されます。
sysbox-runc が渡す PID と一致させるため、mgr と fs も同じ CRI の PID namespace に配置します。
初期 User namespace は維持し、`/run`、`/var`、`/dev` を shared propagation で共有します。
清掃対象は `talos-sysbox` namespace の `sysbox-mgr` と `sysbox-fs` の task と container に限り、任意コマンドの実行 API は設けません。
特権 backend と CRI socket はノードの信頼境界に含まれます。
外側 Pod の非特権設定は、ノード backend の安全性を保証しません。

起動時の socket 接続待ちは60秒、ctr 操作は各5秒に制限します。
filesystem daemon は manager socket の接続を待ちます。
TERM/INT は backend に TERM を送り、最大10秒の後に KILL へ進み、自身の task と container だけを除去します。
Talos の `restart: always` で再起動し、残った所有 task を限定して清掃します。
manager は `--disable-shiftfs` を使用し、helper rootfs の os-release は Talos を示します。

### 検証と互換性

manifest の対象は Talos 1.14.5、Sysbox 0.7.1 と、選択した amd64／arm64 です。
native amd64 spike の基準は kernel 6.18.58-talos、外側 Kubernetes 1.37.1、containerd 2.3.7、workloadIsolation=true です。
[spike の証跡](docs/test-evidence/issue-19-spike.json) は公開済み Sysbox バイナリと試作用 supervisor の記録で、最終成果物の成功証拠とは区別します。
検証ツールと workload の固定版は英語節にあります。
再ビルドしたローカル Standard amd64 候補では、導入／再起動、spike 設定除去後の新規 runtime 登録、proc/sysfs 79項目、mgr/fs 再起動後の Docker／privileged child／kind／Helm／内部 DNS-TCP／外部 DNS、コンテナ再作成、通常 Pod のセキュリティ検査が成功しました。[実測証跡](docs/test-evidence/issue-19-native.json) で導入済み hash と producer の source inputs を対応させています。既存 sandbox の設定初期化による確認で、OS の新規インストールや本番更新経路の検証ではありません。所有 namespace と RuntimeClass は削除済みで、sysboxfs に残留ディレクトリはありません。別 fixture 3分類は UNRUN です。ビルド入力が変わった場合は再検証が必要です。
arm64、SELinux enforcing、VMware agent の実動作は、リリースごとの証拠がある場合だけ検証済みとします。

PR CI は両アーキテクチャの静的検査とユニットテスト、両 installer と probe のビルドを実行します。
Talos-in-Docker は Test workflow の手動入力 `run_runtime=true`（既定 false）でのみ実行でき、PR では起動しません。
今回の更新でも自動起動せず、別途実行しなければ UNRUN と記録します。
手動実行では helper rootfs を使う新しい専用クラスタを作成します。
ホスト kernel を使用する補助検証で、実 Talos kernel や installer の起動を検証するものではありません。
ディスク容量が制約される Docker ホストでは `--constrained-disk` で専用ノードだけの閾値を空き5%、image GC 開始95%へ調整できます。
実 Talos 対象には適用しません。
既存の実ノードへは接続しません。
Docker networking と必要な kernel 機能とリソースがある環境で、英語節のコマンドを使用します。

実 Talos 検証は、新しい installer を導入済みで runtime 登録済みの、明示的に許可された対象で実行します。
runner は更新や再起動を行いません。
kubeconfig、context、期待する node、`real-talos`、結果ファイル、build metadata、Talos API 接続設定を明示します。
検証 namespace を作る前に、実ノードの Sysbox binary 3 種、production proc helper、両 supervisor を Talos API で読み、今回の SHA-256 と一致することを確認します。
記録したソース入力と checkout が異なる成果物も拒否し、過去の prototype の成功を変更後のビルドへ流用できないようにします。
直接 Docker、特権子コンテナ、kind Ready、CoreDNS readiness、Helm rollout、内部 Service DNS/TCP、外部 DNS、再作成後の子コンテナを確認します。
通常 runtime の Pod は UID1001、CapEff0、NNP1、seccomp2 と初期 UID map を確認します。
内側 kind の既定値は Pod `10.200.0.0/16` と Service `10.201.0.0/16` です。
controller-manager の `--cluster-cidr` から外側 Pod CIDR 全体を取得し、Node の割当範囲がその中にあることを確認します。
Kubernetes ServiceCIDR と内側 Docker network も照合し、重複や必要な CIDR 情報の欠落があれば停止します。
別の範囲は `--kind-pod-subnet` と `--kind-service-subnet` で指定します。
ServiceCIDR と controller-manager Pod の読み取り権限が必要で、実行側アーキテクチャの固定 client と異なる binary も拒否します。
最終候補の検証前には SPIKE 専用の sysctl、CRI、RuntimeClass 設定を除去するか新規構築し、最終候補と文書どおりの設定を適用します。
開始状態、方法、結果の設定、最終ビルドの入力と binary と supervisor の hash を記録し、設定の初期化を OS の新規インストールとは主張しません。
一意の専用 namespace を作り、失敗時も削除します。
既定 kubeconfig は変更しません。
公開結果には版、kernel、成否を記録し、ノード名、アドレス、ID、設定、生ログを含めません。

service/backend 再起動とノード再起動は、対象と操作ごとに別途承認を得て実行します。
`build-metadata.json` の supervisor と各 binary の hash が導入済みファイルと一致することを確認し、公開結果との対応は非公開で保持します。
各 extension service の再起動、CRI containerd の所有 backend の停止、ノード再起動を別々に行います。
各操作後に両サービスの復旧、Docker 子コンテナ、通常 Pod を確認し、artifact digest と成否だけを安全に整えた結果へ記録します。
backend の OCI spec で PID と mount namespace を確認します。
別 namespace の task を清掃しません。
VMware の runtime 対応をうたう前に、承認された VMware 環境で agent の健全性と guest 情報の報告を検証します。

### リリースとライセンス

現在の既定ブランチへの push を起点とし、push の SHA と merge commit が一致するマージ済み `feature/` PR を API で確認してからリリースします。直接 push や他ブランチでは公開せず、`pull_request_target` のポリシー例外は不要です。
API で確認した release 候補だけを公開 job の concurrency group に入れ、version の計算・tag の作成・公開をまとめて直列化します。候補ではない push は、既定 branch 名の大文字・小文字が異なる場合も含め、待機中の release を置き換えません。後続の公開対象候補は GitHub の concurrency group で待機中の別候補を置き換える場合があり、置き換えられた PR の commit は version 判定のために個別分類されません。後続 commit に semver tag がある場合は新規 release を停止し、同じ commit にある既存 tag の復旧は許可します。到達可能な version tag がない場合の初期 release version は `v0.2.0` です。 リリースの起動条件は repository への write 権限を持つ協力者を信頼する前提で、workflow 自体を編集できる人に対するセキュリティ境界ではありません。各 push の先端 SHA に対応するマージ済み PR を評価し、複数 PR の merge を一度の push にまとめる運用は、このリリース契約の対象外です。
semantic version とノートは各 commit の subject/body と breaking-change marker から生成し、PR title を判定に使いません。
PR build は read-only 権限で、公開用資格情報を渡しません。
マージ後の公開は既存の GitHub Actions token を使い、任意の probe を含む選択した全 GHCR image の匿名取得を確認します。
全 tag を事前確認した後、選択した image（任意の probe を含む）のうち存在しないものすべてを push してから匿名確認します。新規 GHCR package は所有者が既存権限で全件を public にし、workflow を再実行して元の tag を再ビルド／再 push せず検証します。
匿名確認が失敗したリリースを成功扱いにしません。
認証設定の追加は行いません。

リリースにはイメージ digest、実際の入力、package/source checksum、対応ソースを添付します。
extension image の `/usr/local/share/licenses/talos-sysbox` に第三者ライセンス、copyright、該当 NOTICE を保持します。
`sources.tar.gz` は Sysbox/ctr、Talos 1.14.5、Linux 6.18.58、公式 iSCSI/util-linux extension、VMware 版専用の guest agent、checksum 検証済み Go module、Ubuntu source package を含みます。固定した SideroLabs の Talos/pkgs/extensions/tools/toolchain リポジトリに kernel 設定・patch・再ビルド recipe が含まれます。`build/official-sources.lock.json` で image digest と対応させ、image pin の変更時には source inventory も更新します。3イメージで共通の source bundle を使い、VMware agent の binary は VMware installer のみに含みます。組み立て前に固定 catalogue と照合します。ctr は vendor source から CGO 無効で再ビルドし、Go build 情報を記録します。binary 公開前に source asset を添付し、各 binary リリースと一緒に配布して利用可能な状態を維持します。
リポジトリの MIT は同梱物の Apache-2.0、BSD、GPL、LGPL を置き換えません。
registry login と `publish.py --preflight-only` により、release を作る前に classic Docker image store、読み込んだ archive の config digest、全既存 tag を確認します。認証・通信の失敗時は release を作りません。configuration digest は Docker image ID ではなく保存済み archive の config bytes から計算し、新規／復旧時の push で containerd image store は明示的に拒否します。
release に `extension.tar`・`standard-image.tar`・`vmware-image.tar` と、選択時の `probe-image.tar` を保持し、archive 全体の SHA-256 を `images.json` に記録します。元の archive・対応ソース・provenance を draft に全件添付した後、最後に `images.json` を完了 marker として添付します。marker のない未完成 draft の途中 asset だけ置換できます。既に package が public の後続リリースも含め、全 GHCR push の前に対応ソースを公開します。
marker 前に添付が失敗した場合は、再実行で事前確認後に再ビルドした未公開 draft を完成させます。marker 後の login・事前確認・push の失敗では、再実行で元の archive を全件取得し、checksum・config digest・tag を検証してから読み込み、不足 tag だけを push します。再ビルドや既存 tag／source asset の置換はしません。異なる config digest の既存 tag は push 前に拒否します。匿名確認の失敗時は選択した全 package を public にして再実行します。`release-images.json` のある完了済み release は元の入力と公開 digest の再検証だけを行い、archive の読み込み・再ビルド・再 push はしません。元の asset の不足・破損は明示的に停止し、release や tag の置換ではなく元の bytes を復元します。
提供した source と build input から helper rootfs を再構築し、LGPL ライブラリを交換できます。

### 障害調査

builder の DNS や bridge エラーは、使用中の builder と Docker network を確認し、既存の正常な builder または明示した host-network build を使用します。
checksum 不一致は処理を止め、lock を変更する前に上流配布物を確認します。
handler エラーは RuntimeClass と既存設定を保持した CRI fragment の両方を確認します。
service の再起動ループは、非公開ログ、CRI containerd、socket、所有 task を調べます。
Docker や kind の失敗は、導入済み artifact、PID/mount propagation、cgroup v2、kernel 機能を確認し、同じ検証環境の結果と比較します。
VMware 以外の guest-agent エラーは、承認された更新で共通版へ変更します。
GHCR の匿名取得に失敗した場合は、所有者に package visibility を確認します。
公開情報の参照先は英語節にあります。
