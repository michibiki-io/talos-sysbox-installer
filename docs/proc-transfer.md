# Proc transfer integration / Proc transfer の取り込み

## Contents / 目次

- [English](#english)
  - [Source selection and updates](#source-selection-and-updates)
  - [Build and activation](#build-and-activation)
  - [Validation artifacts](#validation-artifacts)
  - [Approved operator verification](#approved-operator-verification)
  - [Evidence boundaries](#evidence-boundaries)
- [日本語](#日本語)
  - [ソースの選択と更新](#ソースの選択と更新)
  - [ビルドと有効化](#ビルドと有効化)
  - [検証用成果物](#検証用成果物)
  - [承認後の運用者検証](#承認後の運用者検証)
  - [検証結果の範囲](#検証結果の範囲)

## English

### Source selection and updates

[build/proc-transfer.lock.json](../build/proc-transfer.lock.json) is the authoritative integration lock. It selects public upstream commit `450a656b3722f4281d6812e465a2c0fd65091915` and archive SHA-256 `a265d00a66eca7d73d790356f7282e5383c3b0c422b17c912d80590e4f6268cd`. This reviewed baseline matches the Sysbox 0.7.1 component pins and implements protected nested procfs and binding the existing `/sys` tree. New sysfs/FUSE behavior and arm64 expansion are separate features.

The builder downloads the immutable public archive, verifies its actual SHA-256 and complete regular-file inventory, then checks the upstream source manifest against the downstream root/fs/runc/ipc/libs/fuse revisions and repositories. Missing files, hash/revision differences, unsupported manifests, helper contracts, archive links/path traversal and patch scope stop the build. Nothing reads an inspection checkout or private evidence at build/runtime. There is no branch-following service.

Generate a candidate only after reviewing a specific upstream commit:

```sh
python3 scripts/update_proc_transfer.py --revision FULL_COMMIT --output candidate-proc-transfer.lock.json
# Review every changed input, the FD/argument contract, and component/tool compatibility.
# After accepting the diff, explicitly replace build/proc-transfer.lock.json.
python3 -m unittest discover -s tests -v
python3 scripts/build.py --builder ISOLATED_BUILDER --output .build/selected-update
python3 scripts/build_validation_image.py --builder ISOLATED_BUILDER --output .build/selected-validation
```

The six-component adapter maps `fuse` to the existing `versions.lock.json` `sysbox-fs/bazil` pin (`nestybox/fuse`, revision `5ce319439091b08f33b149cb2482039f1b0a24fe`). The catalog must match exactly: unknown, missing or extra components, repository/revision/hash mismatches and unsupported recipe/ABI contracts fail closed. The archive inventory includes unused sysfs/FUSE sources and runtime fixtures; their presence does not authorize executing the upstream build recipe or building/applying its new helper/patch. Production still applies only `patches/proc-transfer.patch` and builds only `helper/proc-mount-helper.c`.

The updater refuses to overwrite the authoritative lock and compares both production inputs against the previous approved lock, rejecting even self-consistent upstream hash updates that change either file. Relative to reviewed revision `ab02207dafe00284797f64becd214c10e1c99285`, helper SHA-256 remains `78444175cb3e046093442db8e5cad116c2310d4c4879a26d05ec0ba386217208` and proc patch SHA-256 remains `d7a6b3962fe4d58eeed0537660f226d3542bccb71dc0c29c8069b5d61fe88838`. A new ABI/build contract needs an adapter change and review. Updating checksums cannot confer support for new mount types or architectures. Repeat both installer builds, selected upstream regressions and Test CI for the published head. Record actual failures, conditional skips and unrun checks separately.

### Build and activation

The existing digest-pinned Go 1.26.2/Ubuntu builder, signed `20261001T000000Z` APT snapshot, finite retries, read-only Go modules and protobuf generator 1.3.2 remain effective inputs. The upstream's Go 1.27.2/generator 1.3.5 are recorded as upstream metadata, not silently adopted.

Standalone runc and mgr compile first from unpatched component sources. Immediately before fs compilation, the integration applies its fs and runc nsenter changes with `patch --batch --forward --fuzz=0` after a successful dry-run. The nsenter C change is linked into fs only. The static production helper compiles from the same verified archive. The builder and final extension layer assertions require a matching amd64 static pair, root ownership and mode 0755; checkpoint tools must be absent from the production rootfs. Both installer variants consume this same verified extension OCI layout.

The helper is installed at host `/usr/local/lib/containers/sysbox-fs/proc-mount-helper`, visible as backend `/proc-mount-helper`. The supervisor sets `--env=SYSBOX_PROC_TRANSFER_HELPER=/proc-mount-helper` in its actual system-containerd fs invocation. Caller environment variables do not configure the daemon. Existing owned backend supervision, node PID entry, mount propagation and runtime registration remain in place. Install the complete installer in an approved maintenance operation, retaining all other required extensions. No machine-config switch or runtime filter change enables this path.

`build-metadata.json` records the integration revision, original archive/file/patch/helper hashes, upstream manifest, effective Go/protoc/generator inputs, build scope and paired binary SHA-256 values. Corresponding sources include original archives, patch, effective module locks, generated protobuf sources, linker maps and build recipes. Helper static-library owners use the existing signed-snapshot source/license collection; MIT helper notices and Apache-2.0 patch notices accompany them. Source-before-binary publication, immutable tags, anonymous release pulls and original archive recovery use the existing release mechanism.

### Validation artifacts

The fixed builder runs the four selected upstream Go regressions and upstream `test_lifecycle.py` C/kernel tests and `test_proc_verifier.py` unprivileged syscall-stub regressions. The latter exercises the actual proc verifier entry points for all optional-entry absence combinations and rejects lost masks, read-only protections, malformed manifests and observation errors. These cover policy normalization, actual-TID acquisition without fallback, invalid-notification launch suppression, real-notification invalidation, signal/thread/process/exec lifecycle, parent-death cleanup and the helper alarm. Kernel/API failures fail the build; no seccomp relaxation obtains a pass. These tests do not run a Talos cluster.

`evidence/validation/` is separate from `evidence/rootfs/`. Its `artifacts.lock.json` records the launcher, container-exec wrapper, idle-probe program, upstream runtime sources, static checkpoint helper/requester and static ctr hashes. The checkpoint helper includes the production C source with test pauses and is never the installed helper.

The separate `validation-probe` target starts from the same fixed builder. It builds bubblewrap **0.12.0** from a public SHA-256-verified archive, and preinstalls C compiler, Python 3.12+, nsenter and ctr. It records effective versions, matching signed-snapshot package sources and licenses. The default image user is UID/GID 1001. Verification installs no packages.

The post-merge release build uses `scripts/build.py --with-probe` to add the optional dedicated probe to the existing `images.json` release inputs. Its immutable tag is `talos-sysbox-proc-transfer-probe:vPROJECT-talos-vTALOS`. The existing publisher checks every selected tag before any push; staging retains the original `probe-image.tar` alongside all original installer archives before the `images.json` completion marker. Recovery validates the original archive checksum, config digest and tag before loading it. Older releases without a probe retain their three-image contract.

The common `sources.tar.gz` offering includes the original probe bubblewrap archive, every installed signed-snapshot package's matching source/packaging, notices, source hashes, effective versions and recipes. Existing production sources supply ctr's vendored upstream sources, the integration's checkpoint/helper sources and static-library sources/link maps. The inherited Go toolchain source and notices are included too. Sources become public through the existing release before any binary push. No separate publisher, authentication or service is introduced.

Only after actual anonymous manifest and all blob checks succeed does the release emit `test-images.lock.json`, using the existing `test_images` namespace and an exact public digest reference. The publisher checks the anonymous manifest hash and its configuration against the original archive. This generated release asset is the public operator input; no guessed digest is committed to `versions.lock.json`. Publication and anonymous retrieval remain **unrun until the authorized post-merge release**.

For CI and approved local validation, `scripts/build_validation_image.py` exports a digest/checksum-verified local archive and generated lock marked `distribution: local-archive-import-only`, `anonymous_pull_verified: false`. This explicitly unpublished route requires import into node CRI and is not evidence of public availability. CI also compares the probe's embedded production metadata with the installers' build metadata.

### Approved operator verification

Real Talos operations require separate approval naming the concrete node, generated probe image and temporary namespace/privileged launcher operations. The target must already have this build installed and `sysbox-runc` registered. Run the existing supporting Docker/kind/Helm/Service/default-runtime validation separately; the proc runner does not replace it.

The trusted launcher requires amd64, Python **>=3.12**, initial User namespace root, node PID namespace/proc access, `CAP_SYS_ADMIN`, `CAP_SYS_PTRACE` (pidfd_getfd), `CAP_SETUID`, `CAP_SETGID`, `CAP_SYS_CHROOT`, and access to CRI and system-containerd sockets. It uses the image's fixed ctr/nsenter/Python and checkpoint helper. An authorized privileged hostPID Pod on the exact node supplies that context; the actual probe remains a nonprivileged UID/GID 1001 Sysbox Pod with the production filter. Namespace identities are captured independently from `/proc/1/ns/{pid,user}` in the trusted launcher, never inferred inside the probe.

The launcher mounts only `/usr/local` and `/run/containerd` from the host, both read-only. Talos system-containerd is reached at `/proc/1/root/system/run/containerd/containerd.sock` only after the independent namespace and initial UID-map checks. `/system/run/containerd` is absent from the kubelet root view; neither that path nor a proc-root magic-link replacement is a hostPath bind source. CRI stays at `/run/containerd/containerd.sock`. No host filesystem changes are required.

For the public route, obtain the completed release's `test-images.lock.json` asset, render it with the same operator command, and create only the namespace, probe and launcher manifests. The renderer uses the exact `image@sha256` reference and `IfNotPresent`; no bootstrap/import is needed. After readiness, transfer that public lock to the launcher and run the same verification/result commands below. If publication or anonymous checks have not succeeded, there is no public lock to use.

The probe security context reproduces UID/GID 1001, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, capabilities `drop: [ALL]`, and Kubernetes `seccompProfile: Unconfined` for the outer profile only. The Sysbox production filter remains required: the independent process check demands `Seccomp: 2`, `CapEff: 0` and `NoNewPrivs: 1`. No runtime filter is disabled.

For the local-archive route, the image build also exports `tools/ctr` and its artifact lock from the fixed isolated builder. The renderer includes a temporary bootstrap Pod using the **existing public BusyBox digest in `versions.lock.json`**, initial-user root and only the CRI socket mount. It runs the source-built static ctr after SHA-256 verification; BusyBox provides file transport and hashing. This gives Talos an import path without SSH, a pre-existing shell, package installation or a registry. Include the bootstrap's trusted socket access and temporary image import in the operation approval. Workstation Docker loading alone does not populate Talos CRI.

Render manifests offline (output contains private target/token information, mode 0600):

```sh
python3 scripts/proc_transfer_operator.py \
  --test-image-lock .build/validation/test-images.lock.json \
  --expected-node AUTHORIZED_NODE --output .validation/private-operation
```

The output contains a unique namespace, a public bootstrap, a dedicated probe and trusted launcher. The latter two use `imagePullPolicy: Never`. After target/image/operation approval, use explicit kubeconfig/context for each command:

```sh
kubectl --kubeconfig CONFIG --context CONTEXT create -f .validation/private-operation/namespace.json
kubectl --kubeconfig CONFIG --context CONTEXT create -f .validation/private-operation/bootstrap.json
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE wait --for=condition=Ready pod/proc-transfer-bootstrap --timeout=120s
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec -i proc-transfer-bootstrap -- \
  sh -ceu 'cat > /tmp/ctr; chmod 755 /tmp/ctr' < .build/validation/tools/ctr
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec proc-transfer-bootstrap -- sha256sum /tmp/ctr
# Compare to bootstrap_ctr_sha256 in test-images.lock.json BEFORE executing ctr.
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec -i proc-transfer-bootstrap -- \
  sh -ceu 'cat > /tmp/probe-oci.tar' < .build/validation/probe-oci.tar
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec proc-transfer-bootstrap -- sha256sum /tmp/probe-oci.tar
# Compare to proc_transfer_probe.archive_sha256 BEFORE importing.
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec proc-transfer-bootstrap -- \
  /tmp/ctr --address /run/containerd/containerd.sock --namespace k8s.io images import /tmp/probe-oci.tar
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec proc-transfer-bootstrap -- \
  /tmp/ctr --address /run/containerd/containerd.sock --namespace k8s.io images ls
# Verify the imported local_tag manifest digest equals oci_digest in the lock.
kubectl --kubeconfig CONFIG --context CONTEXT create -f .validation/private-operation/probe.json
kubectl --kubeconfig CONFIG --context CONTEXT create -f .validation/private-operation/launcher.json
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE wait --for=condition=Ready pod/proc-transfer-probe pod/proc-transfer-launcher --timeout=120s
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec -i proc-transfer-launcher -- \
  sh -ceu 'cat > /tmp/test-images.lock.json' < .build/validation/test-images.lock.json
# Obtain CONTAINER from probe.status.containerStatuses[0].containerID (strip containerd://).
# TOKEN and OWNED_NAMESPACE come from private-operation/operation.json. Keep them private.
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec proc-transfer-launcher -- \
  python3 /opt/proc-transfer/launcher.py --container CONTAINER --token TOKEN \
  --metadata /opt/production-build-metadata.json --image-lock /tmp/test-images.lock.json \
  --result /tmp/result.json --log /tmp/private.log
kubectl --kubeconfig CONFIG --context CONTEXT -n OWNED_NAMESPACE exec proc-transfer-launcher -- cat /tmp/result.json
# In a finally/cleanup step even on failure, delete only the unique owned namespace:
kubectl --kubeconfig CONFIG --context CONTEXT delete namespace OWNED_NAMESPACE --wait=true --timeout=120s
```

The launcher refuses unrelated processes, pins the owned probe by pidfd, derives its mapped UID/GID for credential verification, validates the imported OCI digest, actual probe image and bwrap/Python/compiler/nsenter hashes, every validation artifact hash and the installed pair/hash/ownership, and checks `/proc/BACKEND/exe` against the downstream fs hash, the running backend's helper environment/path/ownership, and its initial User/node PID namespaces. Containerd's selected image reference (including its CRI config-ID alias) must resolve to the locked manifest digest. The idle-probe token prevents accidental use against another workload. Production binary hashes must match the downstream build embedded in this particular validation image.

Each exec allocates one private mode-0700 directory below the launcher Pod's `/tmp`, ignoring `TMPDIR`. The fixed ctr receives `--fifo-dir=/proc/OWN_PYTHON_PID/root/ABSOLUTE_PRIVATE_DIRECTORY` (the absolute path appended after `root`); hostPID and the live wrapper process let both client and host daemon/shim resolve the same FIFO inodes. A Pod-only `/tmp` spelling is insufficient for the daemon. The wrapper verifies directory identity/mode, keeps it alive through the exec deadline (1–600 seconds) and, on exceptions/cancellation, through the bounded 15-second deletion attempt for only its unique exec ID, then removes it. Cleanup failures remain failures requiring owned-namespace cleanup. Host containerd mounts remain read-only.

Actual-path execution uses ctr task exec with UID/GID 1001 and retains the runtime filter. It runs the unchanged original bwrap command, PID/User/seccomp checks, registered mask/read-only/detach protections, recursive read-only procfs, existing `/sys` bind, both real nonleader mount cases and 500 concurrent mounts with reaping. The stress exec has a 300-second deadline plus bounded cleanup allowance; interruption force-deletes only its unique server exec ID. Failed/time-limited checks never mark cleanup passed; deletion of the owned namespace remains a separate operator step. Helper arguments remain outer-User-namespace UID/GID 1001; host-mapped IDs are used only for process credential checks. Then it runs all four rejection modes and all 32 leader/nonleader checkpoint cases. Only these direct-helper fixtures enter via trusted nsenter to create their own listener; they do not alter any service or running process's filter. Each fixture closes descriptors and reaps uniquely identified owned actors. A final independent containerd process inventory must show only the pinned idle probe before cleanup passes; `namespace_cleanup` stays unrun for the separate operator deletion. The result distinguishes actual-path and checkpoint checks. Missing capabilities/APIs, wrong versions/hashes, observer errors and timeouts fail rather than skip. Preserve raw logs privately; publish only sanitized result JSON and record the final namespace cleanup outcome separately. If cache cleanup is approved too, use the bootstrap ctr to remove only this generated `local_tag` after deleting the probe/launcher Pods; never prune the node image store. Then delete the owned namespace (including bootstrap).

### Evidence boundaries

Talos 1.12.6/kernel 6.18.18-talos remains the amd64 target. Historical v0.3.0 installation and installed hash checks passed, with recovery of the node/workspaces/PVCs and retention of the existing RuntimeClass. Its original launcher failed at the hostPath mount; after a local socket correction, ctr failed at FIFO creation. Locally corrected tooling reached the original UID 1001 bwrap, namespace/seccomp/proc protections and existing sys bind checks, then failed at the old fixture's unconditional `/proc/asound` requirement. Nonleader, stress, rejection/lifecycle and remaining supporting checks were unrun. These partial local results are not official corrected-artifact results.

The selected upstream fix captures all 11 proc candidates before sandbox checks, allows only ENOENT for four kernel-dependent entries (including `/proc/asound`), keeps the three masks mandatory, and checks later entries even after an absent optional item. Recursive-readonly verification receives the captured manifest. No fixture is edited after export.

Updating the source lock changes the production fs embedded integration revision, binary hash and `source_inputs_sha256` even though the proc patch/helper bytes are unchanged. Validation/production revision, source-lock and installed-binary pairing checks remain mandatory. The full new-artifact Talos suite requires matching installer and probe/tool artifacts from the same head, followed by a separately approved installer upgrade and required reboot. Until then retain installed v0.3.0 and the existing RuntimeClass; no split production/test-lock compatibility is supported. Launcher/FIFO-only checks on the old lock do not satisfy this gate.

For this source update, portable launcher/FIFO/adapter/updater and build/source/license/publication/recovery regressions pass locally. Fixed-builder regeneration of both installer compositions (standard and VMware), probe/tool artifacts, inventories, image locks, metadata and corresponding sources, selected upstream build tests, exact-head CI, the full corrected-artifact real Talos suite, Docker/kind/Helm/Service/default-runtime checks and live publication/anonymous pulls remain unrun pending their separate execution. No new image or real-node result is claimed. Upstream arm64/Linux 7.1/bwrap 0.9.0 results are historical evidence about different artifacts. Compilation and host-kernel regressions cannot establish that this pair fixes Talos. New nested sysfs and arm64 support are outside this integration. A missing public probe remains a documented distribution limitation, not a fabricated anonymous-pull success.

## 日本語

### ソースの選択と更新

[build/proc-transfer.lock.json](../build/proc-transfer.lock.json) を取り込みの正本とします。
公開 upstream の確認済み commit `450a656b3722f4281d6812e465a2c0fd65091915` と、archive SHA-256 `a265d00a66eca7d73d790356f7282e5383c3b0c422b17c912d80590e4f6268cd` を採用します。
この基準は Sysbox 0.7.1 の component 固定値と一致し、保護された nested procfs と既存 `/sys` tree の bind を扱います。
新規 sysfs/FUSE と arm64 の拡張は別機能です。

builder は公開 immutable archive を取得し、実際の SHA-256、全通常ファイルの hash、upstream manifest、root/fs/runc/ipc/libs/fuse の revision と repository を照合します。
不足、不一致、未対応の manifest/helper 契約、archive の link や path traversal、patch 範囲の違いで停止します。
調査用 checkout や非公開の証跡を build/runtime の入力にせず、floating branch の自動追従も行いません。

更新時は特定 commit をレビューし、英語節の `scripts/update_proc_transfer.py` で新しい候補ファイルを生成します。
6 component adapter は `fuse` を既存 `versions.lock.json` の `sysbox-fs/bazil` pin（`nestybox/fuse`、revision `5ce319439091b08f33b149cb2482039f1b0a24fe`）へ対応させます。
component 集合の不足、追加、未知の項目、repository/revision/hash の不一致、未対応の recipe/ABI 契約は拒否します。
archive inventory には未使用の sysfs/FUSE source と runtime fixture も含まれますが、upstream recipe の実行、新 helper のビルド、新 patch の適用は行いません。
production は従来の `patches/proc-transfer.patch` と `helper/proc-mount-helper.c` だけを使います。

updater は正本の上書きを拒否し、両 production 入力を直前の承認済み lock と比較します。
upstream manifest の hash を同時に更新しても、いずれかの実ファイルが変われば拒否します。
既レビュー revision `ab02207dafe00284797f64becd214c10e1c99285` と比較して、helper SHA-256 は `78444175cb3e046093442db8e5cad116c2310d4c4879a26d05ec0ba386217208`、proc patch SHA-256 は `d7a6b3962fe4d58eeed0537660f226d3542bccb71dc0c29c8069b5d61fe88838` のままです。
変更する入力、FD と引数の契約、component と tool の互換性を確認した後、正本を明示的に置き換えます。
新しい ABI/build 契約には adapter の変更とレビューが必要です。
両 installer のビルド、upstream 回帰検査、公開 head の Test CI を再実行し、失敗、条件付き skip、未実施を区別します。

### ビルドと有効化

既存の digest 固定 Go 1.26.2/Ubuntu builder、署名付き APT snapshot `20261001T000000Z`、有限 retry、read-only Go module、protobuf generator 1.3.2 を使います。
upstream の Go 1.27.2/generator 1.3.5 は metadata として記録し、無断で採用しません。

単体 runc と mgr を未パッチのソースで先にコンパイルします。
fs のコンパイル直前に dry-run と `patch --batch --forward --fuzz=0` を実行し、fs とそこへリンクする runc nsenter を変更します。
同じ検証済み archive から static production helper を生成します。
builder と最終 extension layer の検査は、amd64 の static pair、root 所有、mode 0755、production rootfs 内に検査 helper がないことを要求します。
両 installer は、この共通 extension OCI layout を使います。

helper の host path は `/usr/local/lib/containers/sysbox-fs/proc-mount-helper`、backend 内の path は `/proc-mount-helper` です。
supervisor が実際の system-containerd fs 起動引数へ `--env=SYSBOX_PROC_TRANSFER_HELPER=/proc-mount-helper` を設定します。
コンテナ caller の環境変数で daemon を変更することはできません。
既存の所有 backend の監督、node PID namespace、mount propagation、RuntimeClass の登録を維持します。
承認された保守操作で、他に必要な extension を含む installer 全体を導入します。
有効化のための machine-config switch や runtime filter の変更はありません。

provenance は revision、元 archive と各入力の hash、patch/helper の hash、実効 Go/protoc/generator、ビルド範囲、生成 binary の hash を記録します。
対応ソースは元 archive、patch、実効 module lock、生成 protobuf、linker map、recipe を含みます。
helper が実際にリンクした static library の package にも、既存の署名付き snapshot から対応ソースと license を収集します。
MIT helper と Apache-2.0 patch の表示を保持し、ソース先行公開、tag 不変、匿名 pull、元 archive の復旧は既存 release 機構を使います。

### 検証用成果物

固定 builder で upstream Go 回帰検査 4 件、`test_lifecycle.py`、非 privileged の syscall stub を使う `test_proc_verifier.py` を実行します。
後者は実 proc verifier の entry point を使い、任意項目の不在の全組み合わせを検査し、mask や read-only 保護の欠落、不正な manifest、観測エラーを拒否します。
policy normalization、実際の TID の取得と fallback 不在、失効した通知での起動抑止、実通知の失効、signal/thread/process/exec、親終了時の回収、helper alarm を検査します。
kernel/API エラーは build の失敗とし、seccomp の緩和で合格を得ません。
Talos cluster は操作しません。

`evidence/validation/` は `evidence/rootfs/` から分離します。
`artifacts.lock.json` に launcher、exec wrapper、idle probe、upstream runtime source、static checkpoint helper/requester、ctr の hash を記録します。
検査 helper は production C source に確認地点を加えたもので、production helper として導入しません。

別 target の `validation-probe` は同じ固定 builder を使い、公開 SHA-256 固定ソースから bubblewrap 0.12.0 をビルドします。
C compiler、Python 3.12 以降、nsenter、ctr を事前に導入し、実効版、snapshot package の対応ソースと license を収録します。
既定ユーザーは UID/GID 1001 で、検証中に package を導入しません。

merge 後の既存 release は `scripts/build.py --with-probe` で専用 probe を任意の追加 image として `images.json` に含めます。
tag は `talos-sysbox-proc-transfer-probe:vPROJECT-talos-vTALOS` で固定します。
既存 publisher が全 tag を事前確認し、元の `probe-image.tar` を installer archive とともに保持してから、最後に完了 marker を添付します。
復旧時は元の checksum、config digest、tag を照合してから読み込みます。
probe のない過去 release の3 image 契約も維持します。

共通の `sources.tar.gz` に bubblewrap の元 archive、導入済み snapshot package の対応ソースと packaging、notice、hash、実効版、recipe を追加します。
既存の source offering にある ctr の vendored source、checkpoint/helper の upstream source、static library の source と linker map も使います。
継承した Go toolchain の source と notice も収録します。
対応ソースを既存 release で公開してから binary を push し、publisher、認証、service は追加しません。

実際の匿名 manifest と全 blob の確認後にだけ、release asset の `test-images.lock.json` を生成します。
既存の `test_images` に公開 digest を記録し、匿名 manifest の hash と config を元 archive に照合します。
架空の digest を `versions.lock.json` へ追加しません。
実公開と匿名取得は、許可された merge 後の release まで未実施です。

CI と承認済みの local 検証では、checksum と digest を固定した archive を出力します。
生成 lock は `distribution: local-archive-import-only` と `anonymous_pull_verified: false` を明示します。
この経路には node CRI への import が必要で、公開取得の証拠にはなりません。
CI では probe に収録した production metadata と両 installer の metadata も照合します。

### 承認後の運用者検証

実 Talos 操作には、対象 node、生成 probe image、一時 namespace と privileged launcher の操作を指定する別承認が必要です。
対象には今回の build が導入済みで、`sysbox-runc` が登録済みである必要があります。
既存の Docker/kind/Helm/Service/default-runtime 検証は別途維持します。

trusted launcher は amd64、Python 3.12 以降、initial User namespace の root、node PID/proc へのアクセス、`CAP_SYS_ADMIN`、`CAP_SYS_PTRACE`、`CAP_SETUID`、`CAP_SETGID`、`CAP_SYS_CHROOT`、CRI と system-containerd socket を要求します。
承認済み hostPID privileged Pod がこの環境を用意し、実 probe は production filter を保持する UID/GID 1001 の非 privileged Sysbox Pod として動きます。
ホスト namespace は trusted launcher の `/proc/1/ns/{pid,user}` から独立して取得します。

launcher の host mount は `/usr/local` と `/run/containerd` だけで、どちらも read-only です。
namespace と initial UID map の独立した確認後にだけ、`/proc/1/root/system/run/containerd/containerd.sock` から system-containerd を参照します。
`/system/run/containerd` は kubelet の root view に存在しないため、hostPath として指定しません。
proc-root の magic link を代わりの bind 元にすることもありません。
CRI は `/run/containerd/containerd.sock` を使い、host filesystem の変更は不要です。

公開経路では、公開完了済み release の `test-images.lock.json` を取得して同じ renderer に渡します。
namespace、probe、launcher だけを作成し、bootstrap や import は不要です。
image は正確な `image@sha256` と `IfNotPresent` を使います。
readiness 後の lock 転送、検証、結果取得は英語節の同じ command を使います。
公開と匿名確認が成功する前は、この公開 lock はありません。

probe は UID/GID 1001、`runAsNonRoot: true`、`allowPrivilegeEscalation: false`、capability の `drop: [ALL]` を指定します。
Kubernetes の `seccompProfile: Unconfined` は外側の profile だけに適用します。
Sysbox の production filter は保持し、独立した process 検査で `Seccomp: 2`、`CapEff: 0`、`NoNewPrivs: 1` を要求します。

image build は固定 builder の static `tools/ctr` と artifact lock も出力します。
manifest に、既存の公開 BusyBox digest を使う一時 bootstrap Pod を含めます。
initial-user root と CRI socket へのアクセスを持ち、BusyBox の file transport と hash 機能で今回ビルドした ctr と OCI archive を転送、照合してから import します。
通常の SSH/shell、事前の node shell、package 導入、registry は不要です。
bootstrap の socket アクセスと一時 image import も操作承認に含めます。
workstation の Docker へ load するだけでは Talos の CRI に入りません。
英語節に転送、hash 照合、import、manifest digest の確認を含む実行 command を示します。

英語節の `scripts/proc_transfer_operator.py` で非公開 manifest を生成します。
固有 namespace、公開 bootstrap、専用 probe、trusted launcher を含みます。後二つの imagePullPolicy は `Never`、出力ファイルは mode 0600 です。
承認後、明示した kubeconfig/context で create と readiness 確認を行います。
生成した test image lock を launcher に転送し、専用 probe の container ID と token を渡して、収録した build metadata と照合します。
成功時も失敗時も、最後に固有 namespace だけを削除します。
英語節に実行 command と結果の取得手順を示します。

launcher は無関係な workload を拒否し、専用 probe を pidfd で固定します。
UID/GID map、import 済み OCI digest、実 probe image と bwrap/Python/compiler/nsenter の hash、全検証 artifact の hash、導入した pair の hash と所有権、実 fs backend の `/proc/BACKEND/exe` hash、helper の環境変数と path と所有権、initial User namespace と node PID namespace を照合します。
CRI の config-ID alias を含む実 container image が、固定 manifest digest を指すことも確認します。
各 exec は launcher Pod の `/tmp` に mode 0700 の private directory を一つ作り、`TMPDIR` は使いません。
固定 ctr に `--fifo-dir=/proc/OWN_PYTHON_PID/root/ABSOLUTE_PRIVATE_DIRECTORY`（`root` の後に private directory の絶対 path を続ける）を指定します。
hostPID と実行中の wrapper process により、client と host daemon/shim が同じ FIFO inode を参照できます。
Pod 内だけの `/tmp` path では daemon から参照できません。
wrapper は directory の同一性と mode を照合し、1～600 秒の実行期限まで保持します。
例外や中断時には固有 exec ID だけを15秒以内の期限で削除を試み、その後に directory を回収します。
削除失敗は成功とせず、所有 namespace の cleanup を要求します。
host containerd mount は read-only のままです。

実経路は UID/GID 1001 の ctr task exec で production filter を維持し、元の bwrap、PID/User/seccomp、mask/read-only/detach、再帰的 read-only procfs、既存 `/sys` bind、実非 leader の二条件、500 回の並行 mount と回収を検査します。
500 回の stress exec には 300 秒の期限と、期限付きの後始末時間を設けます。
中断時は固有 exec ID の server process だけを force-delete します。
失敗や timeout で cleanup の合格を記録せず、専用 namespace の削除は別の操作として残します。
helper の引数は外側 User namespace の UID/GID 1001 を維持し、host に mapped された ID は process credential の照合だけに使います。
次に、直接 helper の拒否 4 モードと leader/nonleader の全 32 checkpoint を検査します。
この直接検査だけは、独自 listener のため trusted nsenter を使い、service や既存 process の filter を変更しません。
各 fixture は自身の FD と固有 actor を回収します。
最後に containerd の独立した process inventory が idle probe だけであることを要求します。
`namespace_cleanup` は運用者による削除まで未実施のままです。

capability/API の不足、版や hash の不一致、observer failure、timeout は検査失敗です。
生ログと識別子は非公開に保ち、公開するのは安全に整えた result JSON と、別途記録する namespace 削除の結果だけです。cache の後始末も承認された場合は、probe/launcher Pod の削除後、bootstrap ctr で今回の local_tag だけを削除してから namespace を削除します。node image store 全体の prune は行いません。

### 検証結果の範囲

対象は amd64 Talos 1.12.6/kernel 6.18.18-talos です。
過去の v0.3.0 導入と導入済み hash の確認は成功し、node/workspace/PVC の復帰と既存 RuntimeClass の保持を確認しています。
元の launcher は hostPath mount で失敗し、ローカルで socket を修正した後は ctr が FIFO の作成で失敗しました。
ローカル修正版では UID 1001 の元の bwrap、namespace/seccomp/proc 保護、既存 sys bind が成功し、その後に旧 fixture の `/proc/asound` 必須扱いで失敗しました。
非 leader、stress、拒否/lifecycle、残りの補助検査は未実施です。
この部分的なローカル結果は、正式な修正版成果物の検証結果ではありません。

採用した upstream 修正は sandbox 検査前に11候補すべてを取得し、`/proc/asound` を含む kernel 依存の4項目だけに ENOENT を許容します。
3 mask は必須のままで、任意項目の不在後も後続項目を検査します。
再帰的 read-only 検査には取得済み manifest を渡し、export 後の fixture は編集しません。

source lock を更新すると、proc patch/helper の実体が同じでも、production fs の埋め込み revision、binary hash、`source_inputs_sha256` が変わります。
validation/production の revision、source-lock、導入済み binary の pair 検査を維持します。
新成果物の実 Talos 完全検証には、同一 head の installer と probe/tool、および別途承認された installer upgrade と必要な reboot が必要です。
それまでは導入済み v0.3.0 と既存 RuntimeClass を保持します。
production/test lock を分離する互換契約は導入せず、旧 lock の launcher/FIFO 単独検査でこの条件を満たしたとは扱いません。

今回の source 更新では、移植可能な launcher/FIFO/adapter/updater と build/source/license/公開/復旧のローカル回帰検査が成功しています。
固定 builder による両 installer 構成（standard と VMware）、probe/tool、inventory、image lock、metadata、対応ソースの再生成、選択した upstream build test、同一 head の CI、修正版成果物の実 Talos 完全 suite、Docker/kind/Helm/Service/default-runtime、実公開と匿名 pull は、別途実行するまで未実施です。
新 image や新たな実ノード結果は主張しません。
upstream の arm64/Linux 7.1/bwrap 0.9.0 の成功は異なる成果物の過去の結果です。
コンパイルと host kernel の回帰検査から、今回の pair が Talos を修正したとは主張しません。
新規 nested sysfs と arm64 は対象外です。
公開 probe image の不在は配布上の制約として記録し、匿名取得の成功を作りません。
