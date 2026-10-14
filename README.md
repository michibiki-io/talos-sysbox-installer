# talos-sysbox-installer

Pinned, isolated builds of the amd64 Sysbox extension and standard/VMware Talos installers.

## Contents / 目次

- [English](#english)
  - [Build and image variants](#build-and-image-variants)
  - [Installation and runtime opt-in](#installation-and-runtime-opt-in)
  - [Upgrades and rollback](#upgrades-and-rollback)
  - [Backend trust and supervision](#backend-trust-and-supervision)
  - [Validation and compatibility](#validation-and-compatibility)
  - [Release and licensing](#release-and-licensing)
  - [Troubleshooting](#troubleshooting)
- [日本語](#日本語)
  - [ビルドとイメージの種類](#ビルドとイメージの種類)
  - [導入とruntimeの選択](#導入とruntimeの選択)
  - [更新とロールバック](#更新とロールバック)
  - [backendの特権と監督](#backendの特権と監督)
  - [検証と互換性](#検証と互換性)
  - [リリースとライセンス](#リリースとライセンス)
  - [障害調査](#障害調査)

## English

### Build and image variants

Requirements: amd64 Linux, Python 3.11+, Docker with Buildx, and HTTPS access to the pinned registries, Ubuntu snapshot and upstream release archives.
No host Go compiler or installed helper programs are used.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/build.py --version 0.2.0
```

The locked Go and Ubuntu images build a static supervisor and collect shell, rsync, modprobe, iptables, fusermount3 and their libraries inside a container.
The three Sysbox binaries are rebuilt from the release's exact component commits using read-only, checksum-verified Go module resolution and the locked builder's static C libraries.
GNU linker maps identify the actual linked static libraries so their matching sources and licenses accompany the application source and rebuild scripts.
The checksum-verified upstream package supplies reference release documentation, not host-derived executables.
Ubuntu's signed snapshot fixes package resolution; upstream archives have SHA-256 locks. APT operations retry downloads and then the same operation at most three times on failure, without changing the snapshot or disabling signature verification.
Each operation allows three download retries, 30-second HTTP/HTTPS timeouts, and two 5-second waits between the three operation attempts. Persistent failures still stop the build. The `dependencies` Docker target permits checking snapshot indexes and build dependencies without building the installers.
Build inputs are in [versions.lock.json](versions.lock.json).
The context physically contains only `build/`, `supervisor/`, the lock and the project license.
It never includes local configs, logs or state.
For hosts without a working default Docker bridge, `--network host` is an explicit local build option; it does not change Docker configuration.
Select an existing Buildx instance with `--builder NAME`.

| Image | Extension set |
| --- | --- |
| `ghcr.io/michibiki-io/talos-sysbox-extension` | Sysbox 0.7.1 and supervised privileged node backends |
| `ghcr.io/michibiki-io/talos-sysbox-installer-standard` | Sysbox, iscsi-tools, util-linux-tools |
| `ghcr.io/michibiki-io/talos-sysbox-installer-vmware` | Standard set plus vmtoolsd-guest-agent |

All tags have the form `v<project-version>-talos-v<talos-version>`, for example `v0.2.0-talos-v1.12.6`.
Project semver describes this repository; Talos and Sysbox retain their independent upstream versions.
Official extensions are consumed directly by digest from the catalogue for the selected Talos release.
The custom extension is passed to imager through an OCI layout, avoiding a local insecure registry.
Both the custom profile and the explicit base installer are pinned.
Generated archives are retagged before loading so they never overwrite or publish a SideroLabs image name.
Local composition records label the custom manifest as `oci-layout@sha256:…`.
Publication replaces that local reference with the anonymously verified GHCR manifest digest; the two digests need not match.

Outputs under `.build/` include all three loaded images, `images.json`, each installer's `inputs.json`, `evidence/build-metadata.json` and `sources.tar.gz`.
To customize the shared extension list, copy the lock, edit `standard_extensions` and add catalogue-approved digest pins to `extensions`, then use `--lock FILE`.
The VMware agent is reserved for the VMware variant.
Build and source-inventory JSON use stable key ordering. Fixed inputs and repeatable runtime payloads do not guarantee identical OCI/image digests across builders: image/layer timestamps and exporter details may differ. Preserve original release archives for retries rather than rebuilding an existing version tag.
Use the same `--lock FILE` for either runtime validation runner; the selected lock is included as `project/versions.lock.json` in the corresponding-source archive.
The validation client is deliberately locked to talosctl 1.13.5 while the target OS remains Talos 1.12.6. Its Docker-cluster provisioning/runtime checks and read-only 1.12.6 API preflight have been exercised; upgrade/reboot compatibility must be recorded by real-node validation separately. Changing the client requires repeating those checks.
CI sends fork PRs to GitHub-hosted runners. A configured `ACTIONS_RUNNER` override for trusted branches must identify a disposable, isolated runner with no production credentials or workloads.
Any new Talos release requires explicit compatibility validation before changing the manifest constraint.

### Installation and runtime opt-in

Use a digest from the actual release's `release-images.json` for installation and upgrades.
Verify the target architecture, full extension list and target Talos version before a maintenance window.
Choose the VMware installer only on VMware: the upstream agent restarts continuously and rejects other platforms.
New-node installation uses `machine.install.image` in the operator's private Talos machine configuration.
This repository supplies no cluster credentials or embedded machine config.

Runtime registration uses the supported `/etc/cri/conf.d/20-customization.part` fragment.
First export its existing contents and reconcile any existing `machine.files` entry for that same path.
For a confirmed fresh node, an explicitly empty input file is appropriate.
Generate a patch offline:

```sh
# After confirming that this path has no machine.files entry:
python3 scripts/runtime_patch.py --input-fragment existing.part --no-existing-machine-file --output runtime.patch.json
# When that machine.files entry already exists (export the complete list privately):
python3 scripts/runtime_patch.py --input-fragment existing.part --existing-machine-files machine-files.json --output runtime.patch.json
```

The existing-entry mode uses Talos strategic `$patch: delete` to select only this file path, then adds its replacement within the same patch/application. It preserves permissions and other entries, and refuses missing or duplicate matches in the supplied list. The input fragment must exactly match the current entry content; stale or empty replacements are rejected. The target entry moves to the end of the list. This works with both single- and multi-document machine configurations; Talos rejects JSON6902 for multi-document input.
Keep the exported array and generated patch private: other file entries may contain secrets. Patch output is mode 0600.
Changes to `machine.files` require a reboot on Talos 1.12.6. Obtain maintenance approval and check drain/PDB conditions as for an upgrade. Use `talosctl patch machineconfig --mode=staged --patch @runtime.patch.json` with explicit connection flags to stage the change for the next approved reboot. The default auto mode may reboot immediately; selector failures require a fresh export and reconciliation, not removal of the delete selector. Recheck the current entry immediately before applying; strategic selectors do not provide a content/resource-version compare-and-swap.
Check the handler after both initial boot and an authorized reboot.
Review the full output before applying it with an explicit Talos config, endpoint and node.
Do not apply a patch made from an empty fragment to an existing node.
The generator appends only the dedicated handler, preserves supplied defaults/custom handlers, and refuses an existing `sysbox-runc` entry.
The handler uses `io.containerd.runc.v2`, `/etc/cri/conf.d/base-spec.json`, `/usr/local/bin/sysbox-runc` and `SystemdCgroup=false`.
No default runtime setting is changed.

With an explicit kubeconfig and context, apply [RuntimeClass](examples/runtimeclass.yaml), then create [the Docker Pod](examples/docker.yaml) in an authorized namespace.
The namespace must admit this workload's user-namespace root capabilities; it is not a Restricted Pod Security workload.
Only Pods specifying `runtimeClassName: sysbox-runc` use Sysbox.
The example uses `hostUsers: false`, UID/GID 0, `privileged: false`, and no service-account token.
Run Docker directly with overlay2 and cgroupfs; privileged Docker children and kind nodes remain within the outer user namespace.
The validated design does not require Podman, manual cgroup delegation, a sysfs adapter, an explicit FUSE device request, a custom Pod seccomp profile, or the `KubeletInUserNamespace` feature gate.

### Upgrades and rollback

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

### Backend trust and supervision

Extension services live under `/usr/local/lib/containers/sysbox-mgr` and `sysbox-fs`.
Their static supervisors talk to **system containerd** at `/system/run/containerd/containerd.sock`, separate from CRI containerd.
Only namespace `talos-sysbox` and exact owned task/container IDs `sysbox-mgr` and `sysbox-fs` are cleaned up.
Ordinary extension services have a private PID namespace, while Sysbox must resolve node PID identities for user-namespace mediation.
Backends therefore join the node PID namespace and share `/run`, `/var` and `/dev` with shared mount propagation.
These privileged backends and the system-containerd socket belong to the trusted node runtime.
An outer non-privileged Pod does not make those node components unprivileged or certify isolation.

Startup waits for connectable sockets with a 60-second deadline; the filesystem daemon additionally waits for the manager socket.
Each ctr control operation has a 5-second limit.
On TERM/INT the supervisor sends TERM to its backend, waits at most 10 seconds, then escalates to KILL and removes only its own task/container.
Talos `restart: always` launches a fresh supervised instance after failure; bounded stale cleanup prevents overlapping owned tasks.
Manager runs with `--disable-shiftfs`; helper rootfs identifies itself as Talos.

### Validation and compatibility

The manifest is restricted to **Talos 1.12.6**, Sysbox 0.7.1 and amd64.
The functional design baseline is Talos kernel 6.18.18-talos, Kubernetes 1.35.2 and containerd 2.1.6.
Runtime validation uses checksum-locked Docker 27.5.1, kind 0.31.0, kindest/node 1.35.0, kubectl 1.35.2 and Helm 3.19.0.
New artifacts require their own validation; historical design results do not validate this build.
Arm64, SELinux enforcing and VMware guest-agent runtime behavior are unvalidated unless release-specific evidence explicitly records them.

CI builds both variants and runs supporting **Talos-in-Docker** validation against a newly owned cluster using the host kernel.
It never connects to an existing real cluster.
This mode is not real Talos-kernel coverage and does not boot the installer.
On a disk-constrained Docker host, `--constrained-disk` changes only the disposable node's eviction and image-GC thresholds (5% free disk, 95% high watermark).
This option is never used on real Talos targets.
Run it locally only when Docker networking, resources and required host kernel facilities are available:
The host's `br_netfilter` module must already be loaded; the local harness refuses to change host kernel modules.
An operator may explicitly authorize a temporary load/unload, while CI loads it only on its disposable runner.

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

It checks direct Docker, a privileged child, kind Ready, Helm rollout, Service DNS/TCP, a changed workload container ID followed by successful child startup, and an ordinary default-runtime Pod.
Before mutating the test namespace, it verifies the installed three Sysbox binaries and both supervisors against this build's SHA-256 values using explicit Talos API reads.
It also refuses artifacts whose recorded source inputs differ from the current checkout, preventing prior prototype results from being attributed to a changed build.
It creates one unique owned namespace and deletes it even on failure.
It preserves the user's default kubeconfig.
Public results contain version/kernel information and boolean checks, without node names, addresses, IDs, configs or raw logs.

For service/backend restart and node reboot recovery, obtain separate approval for the exact target and operations.
Record the installed build's `supervisor_sha256` and binary hashes from `build-metadata.json`, verify the installed files match, and retain a private mapping to the public result.
Restart each extension service, terminate each exact owned backend via system containerd, and reboot the node in separate approved steps.
After each step, wait for both services, repeat Docker-child and ordinary-Pod probes, and record pass/fail plus the artifact digest in a sanitized result.
Verify node PID identity and shared mounts from the backend task's OCI spec.
Never clean unrelated containerd namespaces/tasks or publish raw service logs.
VMware functionality requires a separate authorized VMware target; verify the agent is healthy and guest information is reported through VMware tools before claiming coverage.

### Release and licensing

Release automation starts on pushes to the current default branch and requires an API-confirmed merged `feature/` PR whose merge commit exactly matches the pushed SHA. Direct pushes and other branches cannot publish; no `pull_request_target` policy exception is needed.
Concurrency groups are separated by pushed ref so feature-branch pushes cannot replace a pending default-branch release. When there is no reachable version tag, the initial release version is `v0.2.0`. Release gating assumes trusted repository write collaborators; job conditions are not a security boundary against someone editing workflow files. Each push evaluates the merged PR at its tip SHA; combining multiple PR merges in one push is outside this release contract.
It derives semver and release notes from individual commit subjects/bodies, including breaking-change markers, without using PR titles for classification.
PR builds have read-only repository permissions and no publication credentials.
Post-merge publication uses the existing GitHub Actions token and fails unless all three GHCR images are anonymously readable.
All tags are checked before publication, then all missing images are pushed before any anonymous check. An owner must make all three newly created GHCR packages public using their existing permissions, then rerun the workflow to verify the original tags without rebuilding or repushing. A failed anonymous check is not a successful release.
This repository does not add authentication settings or buy credentials/credits.

Releases attach image digests, effective inputs, package/source checksums and corresponding sources.
The extension retains third-party license texts, package copyright files and applicable NOTICE files under `/usr/local/share/licenses/talos-sysbox`.
`sources.tar.gz` includes matching Sysbox/ctr sources, Talos 1.12.6 and Linux 6.18.18 sources, the official iSCSI/util-linux extensions, the VMware-only guest agent, checksum-verified Go modules, and matching Ubuntu source packages. The pinned SideroLabs Talos/pkgs/extensions/tools/toolchain repositories provide kernel configuration, patches and rebuild recipes. `build/official-sources.lock.json` binds this inventory to the installer and extension digests; changing those pins requires a matching source inventory update. All three images use this common source bundle, with the VMware agent present only in the VMware installer. The pinned catalogue is checked before assembly. ctr is rebuilt from vendored sources with CGO disabled, and its Go build information is recorded. Sources are attached before image publication; distribute it alongside the binaries and keep it available for the corresponding release.
The project MIT license does not replace Apache-2.0, BSD, GPL or LGPL licenses of bundled software.
Registry login and `publish.py --preflight-only` check the classic Docker image store, loaded archive config digests and all existing tags before staging a release. Auth/transport failures stop without creating a release. Configuration digests come from saved archive config bytes, not Docker image IDs; a containerd image store is explicitly unsupported for a fresh or resumed push.
The release retains `extension.tar`, `standard-image.tar` and `vmware-image.tar` with whole-archive SHA-256 values in `images.json`. All original archives, matching sources and provenance are attached to a draft before `images.json` is attached last as its completion marker. Only an incomplete draft without that marker can have its unfinished assets replaced. Matching source assets are made public before any GHCR push, including later releases whose packages are already public.
If staging fails before the marker, rerun to rebuild and finish that unpublished draft after preflight. If login, preflight or a push fails after the marker, rerun to download and checksum/config/tag-verify all original archives before loading them; push only missing tags, never rebuild or replace existing tags/source assets. A preexisting tag with a different config digest still stops before any push. If anonymous checks fail, make all three packages public and rerun. A completed release with `release-images.json` is only verified using its original inputs and published digests, without loading archives, rebuilding or repushing. Missing/corrupt original assets stop explicitly; restore the exact original bytes rather than replacing the release or its tags.
LGPL library replacement remains possible by rebuilding the helper rootfs from the supplied source/build inputs.

### Troubleshooting

| Symptom | Checks |
| --- | --- |
| Builder DNS/bridge error | Check the selected builder and Docker network; use an existing working builder or the explicit host-network build option without altering global settings. |
| Checksum mismatch | Stop; independently investigate upstream assets before editing a lock. |
| RuntimeClass not found / handler failure | Verify both the Kubernetes RuntimeClass and merged CRI fragment, without changing the default handler. |
| Service restarts | Check private `ext-sysbox-mgr`/`ext-sysbox-fs` logs, system-containerd availability, socket readiness and exact owned task IDs. |
| Docker child/kind failure | Verify actual installed artifact, PID/mount propagation, cgroup v2 and kernel facilities; compare sanitized results to the correct environment. |
| VMware agent restarts on bare metal | Select the standard installer during an approved upgrade. |
| Anonymous GHCR check fails | Confirm package visibility with the owner; publishing credentials do not prove anonymous access. |

Public upstream references: [Sysbox release](https://github.com/nestybox/sysbox/releases/tag/v0.7.1), [Sysbox design](https://github.com/nestybox/sysbox/blob/v0.7.1/docs/user-guide/design.md), [Talos extension services](https://docs.siderolabs.com/talos/v1.12/build-and-extend-talos/custom-images-and-development/extension-services), [official extensions](https://github.com/siderolabs/extensions), [VMware agent](https://github.com/siderolabs/talos-vmtoolsd), [Ubuntu snapshots](https://snapshot.ubuntu.com/).

## 日本語

### ビルドとイメージの種類

amd64 Linux、Python 3.11 以降、Buildx 付き Docker、固定した registry と Ubuntu snapshot と上流アーカイブへの HTTPS 接続を使用します。
ホストの Go compiler や helper は使用しません。
英語節のビルドコマンドで、固定した Go と Ubuntu のコンテナ内から static supervisor、shell、rsync、modprobe、iptables、fusermount3 と共有ライブラリを生成します。
ビルド入力は `versions.lock.json` にあり、署名付き Ubuntu snapshot と上流アーカイブの SHA-256 を検証します。APT はダウンロードを再試行し、失敗時には同じ操作を最大3回試します。snapshot の変更や署名検証の無効化は行いません。
各操作はダウンロード再試行3回、HTTP／HTTPS timeout 30秒、操作の試行間に5秒の待機2回を設定します。恒久障害時はビルドを停止します。Docker の `dependencies` target で、installer を構築せずに snapshot index とビルド依存を確認できます。
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
タグは `v<project-version>-talos-v<talos-version>` です。
プロジェクトの semantic version、Talos 版、Sysbox 版は別々に管理します。
公式 extension は対象 Talos の catalogue にある digest を直接使用します。
自作 extension は OCI layout として imager に渡すため、ローカル insecure registry を必要としません。
生成した installer archive は読み込む前にこのプロジェクトの名前へ変換します。
ローカルの構成記録は自作 manifest を `oci-layout@sha256:…` として区別します。
公開時は匿名取得を確認した GHCR manifest digest に置き換えます。両者の digest は一致するとは限りません。

`.build/` に3つのイメージ、`images.json`、installer ごとの `inputs.json`、`evidence/build-metadata.json`、`sources.tar.gz` を生成します。
構成変更は lock のコピーで `standard_extensions` と対応する digest を編集し、`--lock FILE` で指定します。
VMware agent は VMware 版だけに含めます。
ビルドと source inventory の JSON は key 順序を固定します。固定入力と同じ runtime payload を再生成できることは、builder 間の OCI／image digest の完全一致を保証しません。image／layer の timestamp や exporter の違いが残るため、既存 version tag の再ビルドではなく元の release archive を使って復旧します。
runtime 検証 runner にも同じ `--lock FILE` を指定します。対応ソースの archive には、選択した lock を `project/versions.lock.json` として保存します。
検証 client は talosctl 1.13.5、対象 OS は Talos 1.12.6 に固定しています。Docker クラスタ作成・runtime 検証と 1.12.6 API の読み取り preflight を確認しています。実ノードの更新・再起動の互換確認は別に記録し、client 更新時にこれらを再確認します。
CI は fork PR を GitHub-hosted runner で実行します。信頼したブランチで `ACTIONS_RUNNER` を指定する場合も、production 資格情報や workload を持たない隔離された使い捨て runner に限ります。
Talos 互換指定を広げる前に、新しい版で検証してください。

### 導入とruntimeの選択

実際のリリースの `release-images.json` にある digest を使用し、対象 Talos 版、amd64、必要な extension 全件を保守作業前に確認します。
VMware agent は VMware 以外で失敗して再起動するため、VMware 版は VMware 上で使用します。
新規ノードの `machine.install.image` は運用者の非公開 machine config に設定します。
このリポジトリはクラスタ認証情報や埋め込み設定を提供しません。

runtime 登録には `/etc/cri/conf.d/20-customization.part` を使用します。
同じ `machine.files` entry がないことを確認した場合は `--no-existing-machine-file` を指定します。既存 entry がある場合は、現在の配列全体を非公開 JSON に保存して `--existing-machine-files FILE` に渡します。
既存 entry は Talos の strategic `$patch: delete` で対象パスだけを選択し、同じ patch/apply 内で置き換える entry を追加します。permissions と他の entry を保持し、入力中の対象の不足・重複を拒否します。入力 fragment と現在の entry の content が完全一致することを要求し、古い内容や空の内容への置換を拒否します。対象 entry は配列の末尾へ移ります。単一・複数 document の両方に対応し、Talos が複数 document で拒否する JSON6902 を使いません。
配列と patch は秘密情報を含む可能性があるため公開しません。出力の permission は 0600 です。
Talos 1.12.6 の `machine.files` 変更には再起動が必要です。更新と同じ保守承認と drain／PDB の確認を済ませ、接続先を明示した `talosctl patch machineconfig --mode=staged --patch @runtime.patch.json` で次の承認済み再起動に向けて保存します。既定の auto mode は直後に再起動する場合があります。selector が失敗したら現在の設定を再取得して照合し、delete selector を削除しないでください。strategic selector は内容や resource version の compare-and-swap を提供しないため、適用直前にも現在の entry と照合します。初回起動と承認した再起動の両方で handler を確認します。
既存の内容を取得し、同じパスを持つ `machine.files` 設定とも照合してから、英語節の `runtime_patch.py` コマンドで patch を生成します。
空の入力は新規ノードであることを確認した場合だけ使います。
既存ノードの fragment を空の入力から作った patch で上書きしないでください。
生成処理は既存 handler を保持し、`sysbox-runc` の重複があれば拒否します。
handler の BinaryName は `/usr/local/bin/sysbox-runc`、SystemdCgroup は false、base spec は `/etc/cri/conf.d/base-spec.json` です。
既定 runtime は変更しません。

明示した kubeconfig と context で RuntimeClass と Docker Pod 例を適用します。
外側 Pod は `hostUsers: false`、UID/GID 0、`privileged: false`、API token なしで、`runtimeClassName: sysbox-runc` を指定した Pod だけ Sysbox を使用します。
Pod 内の Docker は overlay2 と cgroupfs を使用します。
Docker 子コンテナと kind の特権は外側の user namespace 内にあります。
user namespace 内の root capability を許可できる namespace が必要で、Restricted Pod Security profile ではありません。
この設計の基準では Podman、手動 cgroup 委譲、sysfs adapter、FUSE device resource、独自 seccomp profile、KubeletInUserNamespace 設定を必要としません。

### 更新とロールバック

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

### backendの特権と監督

extension service 内の supervisor は、CRI とは別の system containerd socket `/system/run/containerd/containerd.sock` を使用します。
清掃対象は `talos-sysbox` namespace の `sysbox-mgr` と `sysbox-fs` の task と container だけです。
通常の extension service の PID namespace は private ですが、Sysbox が user namespace を仲介するにはノード上の PID が必要です。
backend はノード PID namespace に参加し、`/run`、`/var`、`/dev` を shared propagation で共有します。
これらの特権 backend と system containerd はノードの信頼境界に含まれます。
外側 Pod の非特権設定は、ノード backend の安全性を保証しません。

起動時の socket 接続待ちは60秒、ctr 操作は各5秒に制限します。
filesystem daemon は manager socket の接続を待ちます。
TERM/INT は backend に TERM を送り、最大10秒の後に KILL へ進み、自身の task と container だけを除去します。
Talos の `restart: always` で再起動し、残った所有 task を限定して清掃します。
manager は `--disable-shiftfs` を使用し、helper rootfs の os-release は Talos を示します。

### 検証と互換性

manifest の対象は Talos 1.12.6、Sysbox 0.7.1、amd64 です。
設計上の実行基準は Talos kernel 6.18.18-talos、Kubernetes 1.35.2、containerd 2.1.6 です。
検証ツールと workload の固定版は英語節にあります。
新しい成果物には新しい検証が必要で、過去の成功を今回のビルドの成功証拠にしません。
arm64、SELinux enforcing、VMware agent の実動作は、リリースごとの証拠がある場合だけ検証済みとします。

CI の Talos-in-Docker は、今回の helper rootfs を使う新しい専用クラスタで実行します。
ホスト kernel を使用する補助検証で、実 Talos kernel や installer の起動を検証するものではありません。
ディスク容量が制約される Docker ホストでは `--constrained-disk` で専用ノードだけの閾値を空き5%、image GC 開始95%へ調整できます。
実 Talos 対象には適用しません。
既存の実ノードへは接続しません。
Docker networking と必要な kernel 機能とリソースがある環境で、英語節のコマンドを使用します。

実 Talos 検証は、新しい installer を導入済みで runtime 登録済みの、明示的に許可された対象で実行します。
runner は更新や再起動を行いません。
kubeconfig、context、期待する node、`real-talos`、結果ファイル、build metadata、Talos API 接続設定を明示します。
検証 namespace を作る前に、実ノードの Sysbox binary と supervisor を Talos API で読み、今回の SHA-256 と一致することを確認します。
記録したソース入力と checkout が異なる成果物も拒否し、過去の prototype の成功を変更後のビルドへ流用できないようにします。
直接 Docker、特権子コンテナ、kind Ready、Helm rollout、Service DNS/TCP、container ID が変わる再作成後の子コンテナ、通常 runtime の Pod を確認します。
一意の専用 namespace を作り、失敗時も削除します。
既定 kubeconfig は変更しません。
公開結果には版、kernel、成否を記録し、ノード名、アドレス、ID、設定、生ログを含めません。

service/backend 再起動とノード再起動は、対象と操作ごとに別途承認を得て実行します。
`build-metadata.json` の supervisor と各 binary の hash が導入済みファイルと一致することを確認し、公開結果との対応は非公開で保持します。
各 extension service の再起動、system containerd の所有 backend の停止、ノード再起動を別々に行います。
各操作後に両サービスの復旧、Docker 子コンテナ、通常 Pod を確認し、artifact digest と成否だけを安全に整えた結果へ記録します。
backend の OCI spec で PID と mount namespace を確認します。
別 namespace の task を清掃しません。
VMware の runtime 対応をうたう前に、承認された VMware 環境で agent の健全性と guest 情報の報告を検証します。

### リリースとライセンス

現在の既定ブランチへの push を起点とし、push の SHA と merge commit が一致するマージ済み `feature/` PR を API で確認してからリリースします。直接 push や他ブランチでは公開せず、`pull_request_target` のポリシー例外は不要です。
concurrency group は push された ref ごとに分け、feature branch の push が待機中の既定ブランチの release を置き換えないようにします。到達可能な version tag がない場合の初期 release version は `v0.2.0` です。 リリースの起動条件は repository への write 権限を持つ協力者を信頼する前提で、workflow 自体を編集できる人に対するセキュリティ境界ではありません。各 push の先端 SHA に対応するマージ済み PR を評価し、複数 PR の merge を一度の push にまとめる運用は、このリリース契約の対象外です。
semantic version とノートは各 commit の subject/body と breaking-change marker から生成し、PR title を判定に使いません。
PR build は read-only 権限で、公開用資格情報を渡しません。
マージ後の公開は既存の GitHub Actions token を使い、3つの GHCR イメージを匿名で取得できることを確認します。
全 tag を事前確認した後、存在しない3イメージすべてを push してから匿名確認します。新規 GHCR package は所有者が既存権限で3つとも public にし、workflow を再実行して元の tag を再ビルド／再 push せず検証します。
匿名確認が失敗したリリースを成功扱いにしません。
認証設定の追加は行いません。

リリースにはイメージ digest、実際の入力、package/source checksum、対応ソースを添付します。
extension image の `/usr/local/share/licenses/talos-sysbox` に第三者ライセンス、copyright、該当 NOTICE を保持します。
`sources.tar.gz` は Sysbox/ctr、Talos 1.12.6、Linux 6.18.18、公式 iSCSI/util-linux extension、VMware 版専用の guest agent、checksum 検証済み Go module、Ubuntu source package を含みます。固定した SideroLabs の Talos/pkgs/extensions/tools/toolchain リポジトリに kernel 設定・patch・再ビルド recipe が含まれます。`build/official-sources.lock.json` で image digest と対応させ、image pin の変更時には source inventory も更新します。3イメージで共通の source bundle を使い、VMware agent の binary は VMware installer のみに含みます。組み立て前に固定 catalogue と照合します。ctr は vendor source から CGO 無効で再ビルドし、Go build 情報を記録します。binary 公開前に source asset を添付し、各 binary リリースと一緒に配布して利用可能な状態を維持します。
リポジトリの MIT は同梱物の Apache-2.0、BSD、GPL、LGPL を置き換えません。
registry login と `publish.py --preflight-only` により、release を作る前に classic Docker image store、読み込んだ archive の config digest、全既存 tag を確認します。認証・通信の失敗時は release を作りません。configuration digest は Docker image ID ではなく保存済み archive の config bytes から計算し、新規／復旧時の push で containerd image store は明示的に拒否します。
release に `extension.tar`・`standard-image.tar`・`vmware-image.tar` を保持し、archive 全体の SHA-256 を `images.json` に記録します。元の archive・対応ソース・provenance を draft に全件添付した後、最後に `images.json` を完了 marker として添付します。marker のない未完成 draft の途中 asset だけ置換できます。既に package が public の後続リリースも含め、全 GHCR push の前に対応ソースを公開します。
marker 前に添付が失敗した場合は、再実行で事前確認後に再ビルドした未公開 draft を完成させます。marker 後の login・事前確認・push の失敗では、再実行で元の archive を全件取得し、checksum・config digest・tag を検証してから読み込み、不足 tag だけを push します。再ビルドや既存 tag／source asset の置換はしません。異なる config digest の既存 tag は push 前に拒否します。匿名確認の失敗時は3 package を public にして再実行します。`release-images.json` のある完了済み release は元の入力と公開 digest の再検証だけを行い、archive の読み込み・再ビルド・再 push はしません。元の asset の不足・破損は明示的に停止し、release や tag の置換ではなく元の bytes を復元します。
提供した source と build input から helper rootfs を再構築し、LGPL ライブラリを交換できます。

### 障害調査

builder の DNS や bridge エラーは、使用中の builder と Docker network を確認し、既存の正常な builder または明示した host-network build を使用します。
checksum 不一致は処理を止め、lock を変更する前に上流配布物を確認します。
handler エラーは RuntimeClass と既存設定を保持した CRI fragment の両方を確認します。
service の再起動ループは、非公開ログ、system containerd、socket、所有 task を調べます。
Docker や kind の失敗は、導入済み artifact、PID/mount propagation、cgroup v2、kernel 機能を確認し、同じ検証環境の結果と比較します。
VMware 以外の guest-agent エラーは、承認された更新で共通版へ変更します。
GHCR の匿名取得に失敗した場合は、所有者に package visibility を確認します。
公開情報の参照先は英語節にあります。
