# Standard Oracle ARM64 boot disks

## Contents / 目次

- [English](#english)
  - [Artifacts and validation limits](#artifacts-and-validation-limits)
  - [Generate from Standard inputs](#generate-from-standard-inputs)
  - [Isolation and failure recovery](#isolation-and-failure-recovery)
  - [Download and release records](#download-and-release-records)
- [日本語](#日本語)
  - [成果物と検証範囲](#成果物と検証範囲)
  - [Standard 入力からの生成](#standard-入力からの生成)
  - [隔離と失敗時の復旧](#隔離と失敗時の復旧)
  - [取得方法とリリース記録](#取得方法とリリース記録)

## English

### Artifacts and validation limits

New releases add **Standard / oracle / arm64** installed disks alongside the existing container images. The official Talos 1.14.5 oracle profile supplies metadata v2 behavior. The disk uses ARM64 UEFI/systemd-boot/UKI and exactly one `console=ttyAMA0,9600`; this replaces Oracle's default `tty1` and `ttyS0` through imager console-overwrite semantics. Every generated UKI is inspected. The generator does not extract/reassemble an installer UKI or rebuild Sysbox.

| Asset | Meaning |
| --- | --- |
| `arm64-standard-oracle.qcow2.zst` | Zstandard-compressed QCOW2 |
| `arm64-standard-oracle.vmdk` | Standalone streamOptimized VMDK, intended for OCI `source_image_type=VMDK` |
| `arm64-standard-oracle.inputs.json` | Immutable input references, Standard correspondence, recipe/tool identities, file metrics and inspection results |
| `arm64-standard-oracle.sha256` | Separate hashes for compressed QCOW2, decompressed QCOW2, VMDK and inputs JSON |

Both disk formats have exactly **53,687,091,200 bytes (50 GiB)** virtual capacity and no backing image. This does not mean a 50 GiB physical download. The raw imager scratch request is 2 GiB; the resulting total is inspected rather than inferred from diskSize. After imager stops, the raw disk grows sparsely to 50 GiB, backup GPT moves to the new end, and primary/backup GPT CRCs and partition bounds are checked. EFI/META partition hashes must remain identical before/after normalization. The normalized stopped raw is compared against QCOW2 and VMDK with `qemu-img compare`; QCOW2 also passes `qemu-img check` and a decompression hash roundtrip.

Static inspection checks the actual ARM64 EFI bootloader and UKI, `.linux` as either ARM64 Image or ARM64 EFI zboot PE, and `.initrd` compression by magic (including concatenated zstd frames). All three embedded extension rootfs inventories and manifest metadata must match the exact Standard inputs. Sysbox executable hashes additionally match Standard's build metadata and their ELF machine is ARM64.

The recorded comparison explicitly permits only the observed Talos unpack normalization of the two existing Sysbox private `tmp` directory modes from 1777 to 0777; these differences appear in `mode_differences`. File contents, ordinary permission bits and all other modes must match. This is a static packaging observation, not a runtime validation or an Oracle-specific Sysbox modification. The inspector comment names Talos 1.12.6 as the original observation; the same narrow rule is checked against each new output, including 1.14.5.

The unchanged Standard installer is also exported by immutable configuration/digest solely for inspection. Its ARM64 loader and kernel must match, and all embedded extension contents and modes must match Oracle exactly, including the observed tmp modes. These inspected UKI sections are never used to build the Oracle disk.

[Local generation evidence](test-evidence/issue-17-local-oracle.inputs.json) records a real amd64-executor/ARM64-target preview generated from public v0.5.0 Standard inputs. It identifies the then-dirty implementation by base commit plus exact recipe hashes; it is not an exact-final-head CI record. PR artifacts are regenerated from the committed head and carry their own commit, hashes and native-executor evidence. Independent regenerations have different disk hashes.

`OCI NOT_RUN` and boot `NOT_RUN` are explicit. ARM64 Sysbox execution, Docker-in-Sysbox and kind remain `UNRUN`. Generation/static PASS is not evidence of A1 boot or runtime success. No OCI authentication/import/VM operations or cluster bootstrap are performed. Only EFI and META partitions are present; STATE/EPHEMERAL and the consumer's additional 150 GiB disk are not created. No MachineConfig, cluster identity, fixed network/hostname or tenancy data is embedded. Maintenance mode remains the intended first-boot state, pending consumer-side boot validation.

### Generate from Standard inputs

Use Linux, Docker/Buildx and the repository's Python standard-library orchestration. All new compiler, package, formatter, conversion, compression and inspection dependencies run inside containers. No host package installation is required. The executor may be amd64 or arm64; the target is always arm64. The executor lock selects the immutable imager executable image, while the ARM64 lock selects Standard's base installer and extensions. Official imager images include both boot-asset architectures. Input records retain both architectures and both imager references.

After an existing native ARM64 Standard build, reuse its verified OCI layout and metadata:

```sh
python3 scripts/build.py --version 0.7.0 --arch arm64 --with-probe --output .build/arm64
python3 scripts/oracle_disk.py --build-output .build/arm64
```

The second command writes `.build/arm64/oracle/` and adds `disk_artifacts` to that build's `images.json` only after successful validation. It refuses existing disk output or an existing disk record. It uses exactly the same ordered Sysbox/iscsi-tools/util-linux-tools input set as Standard. VMware inputs are not used.

To reuse published inputs, select a new release built with this Talos 1.14.5 recipe. Do not pass historical v0.5.0/Talos 1.12.6 inputs to the current generator; reproduce those using their matching repository revision and recipe. The historical local evidence above remains a Talos 1.12.6 record.

```sh
mkdir -p .build/oracle-release-inputs
gh release download NEW_TAG --repo michibiki-io/talos-sysbox-installer \
  --pattern release-images.json --pattern arm64-build-metadata.json \
  --dir .build/oracle-release-inputs
python3 scripts/oracle_disk.py \
  --release-inputs .build/oracle-release-inputs/release-images.json \
  --build-metadata .build/oracle-release-inputs/arm64-build-metadata.json \
  --output .build/oracle-preview --project-version 0.7.0
```

Choose a fresh output path. `--builder NAME` selects an existing Buildx builder; it never reconfigures or removes other builders. Public extension payload containers are created/exported for static inspection but never started. Published v0.5.0 tags and release assets are not changed. Reproduction means using the same pinned inputs and recipe and passing the same checks; random GPT/FAT identifiers mean byte-identical hashes across independent generations are not promised. Release reruns retain the original disk bytes rather than regenerate them.

### Isolation and failure recovery

Talos 1.14.5 imager writes its raw disk directly without loop devices or mounted filesystems. The generator runs as the caller's UID/GID with all capabilities dropped, no-new-privileges, and default Docker seccomp/AppArmor. It receives only the task's input/output directories, no host block devices or host `/dev`. The pinned official imager and its boot assets are unchanged; the historical loop/formatter gate is no longer used.

Generation is limited to two CPUs (also a two-CPU affinity), 4 GiB RAM with no additional swap, 256 PIDs and a 2 GiB `/tmp` tmpfs. At least 12 GiB free space is required before generation/conversion; the small raw scratch and the existing per-asset 2 GiB limit bound normal output use. Concurrent capacity loss fails the run without publication. Conversion has no block devices, SYS_ADMIN or network; DAC_OVERRIDE only permits access to the task-owned directory.

Only uniquely named/labeled task containers are stopped/removed, including on failure. No loop mappings are created, audited or detached. No Docker prune or host kernel/security change is used. Failed runs retain a unique `<output>.partial-*` directory with `FAILED.txt` and logs; no success directory/record is produced. Inspect `generator.log` and `conversion.log`, and confirm owned-container cleanup before removing only this task's scratch. Successful runs retain their working directory as evidence; only the four files in the success output directory are distribution assets.

The existing Standard/probe corresponding-source collection retains its fixed Ubuntu snapshot. If snapshot source-file retrieval exhausts its retries with HTTP 503, `build/ubuntu_sources.py` obtains the expected file names, sizes and SHA256/SHA512 values from apt's already authenticated snapshot Sources indexes. It fetches those exact bytes from the official `archive.ubuntu.com` pool; only a pool HTTP 404 permits the exact package/version publication in Ubuntu Launchpad (including its librarian redirect). It validates the index's strong hash and also records SHA256, then reruns the original apt command to revalidate the complete source set. No package version or index changes; signature/hash/authentication errors and unavailable indexes fail closed. If a batch specifies multiple versions of the same source package, a pool HTTP 404 stops retrieval because the publication version is ambiguous; no candidate guessing is performed. Used fallback URLs and hashes are recorded in Standard build metadata (`ubuntu_source_fallbacks`) or the probe's `ubuntu-source-fallbacks.json`, and all source files remain in the source offering.

### Download and release records

The PR Test workflow generates ARM64 disks and uploads all four files as the downloadable `standard-oracle-arm64` artifact (14-day retention). This expiry applies to temporary CI artifacts; published GitHub Release assets are separate downloads and do not use that CI retention period:

```sh
gh run download RUN_ID --repo michibiki-io/talos-sysbox-installer \
  --name standard-oracle-arm64 --dir oracle-disks
```

After a human merges and a new release is published:

```sh
gh release download NEW_TAG --repo michibiki-io/talos-sysbox-installer \
  --pattern 'arm64-standard-oracle.*' --dir oracle-disks
```

The checksum list includes the **decompressed** `.qcow2`, which is not a separate download. Decompress with the recorded tools container before checking the complete list. Tool image IDs in inputs JSON identify the locally built container; rebuild its recipe from the recorded executor lock/Dockerfile when that image ID is not present locally. Run zstd, sha256sum and qemu-img inside that container, mounting only the downloaded directory, without SYS_ADMIN or loop access. Preserve the compressed file when decompressing. Import the standalone VMDK directly in the consumer's OCI workflow; no UKI manipulation is required here.

`release-images.json` and `approved-runtime-images.json` retain their existing container `images` arrays and add a separate `disk_artifacts` field. Disk hashes are file SHA256 values, never container manifest digests. Original `images.json` retains per-architecture disk file names/hashes and Standard input relationships. Staging validates every disk file, metadata and checksum list before any release mutation and writes its success marker last. Reruns validate retained original disks even when container publication already completed. Older releases without the optional disk field remain supported. A new release build explicitly requires the ARM64 disk set.

## 日本語

### 成果物と検証範囲

新しいリリースでは、既存コンテナイメージに **Standard / oracle / arm64** のインストール済み起動ディスクを追加します。
公式 Talos 1.14.5 の oracle profile を使用し、metadata v2、ARM64 UEFI、systemd-boot、UKI を扱います。
console は `console=ttyAMA0,9600` の 1 個に固定します。
imager の上書き仕様により既定の `tty1` と `ttyS0` が置換され、生成した全 UKI の実際の引数を検査します。
installer の UKI を分解して組み直す処理や、Oracle 専用の Sysbox 再ビルドはありません。

| 成果物 | 内容 |
| --- | --- |
| `arm64-standard-oracle.qcow2.zst` | Zstandard 圧縮済み QCOW2 |
| `arm64-standard-oracle.vmdk` | 単独で使用できる streamOptimized VMDK。OCI の `source_image_type=VMDK` を想定 |
| `arm64-standard-oracle.inputs.json` | 固定入力、Standard との対応、生成処理とツールの識別情報、実測値、検査結果 |
| `arm64-standard-oracle.sha256` | 圧縮済み QCOW2、展開後 QCOW2、VMDK、入力 JSON を区別した SHA256 |

QCOW2 と VMDK の仮想容量は、どちらも正確に **53,687,091,200 bytes（50 GiB）** です。
backing image はありません。
imager に要求する raw scratch は 2 GiB で、diskSize と生成後の総容量を同一とは仮定せず、実測します。
imager の停止後に raw を sparse 拡張し、backup GPT を新しい末尾へ移します。
前後の EFI/META の内容を hash で比較し、両 GPT の CRC、配置、パーティション境界を検査します。
この正規化済み停止 raw と QCOW2/VMDK のゲスト可視内容を `qemu-img compare` で比較します。
QCOW2 の整合性検査と、圧縮後の展開 hash 一致も確認します。

静的検査では実際の EFI loader と UKI の ARM64 を確認します。
カーネルは ARM64 Image と ARM64 EFI zboot PE を区別し、initrd の圧縮は拡張子ではなく magic で判定します。
3 拡張の rootfs 全体のファイル一覧、内容 hash、mode、symlink と manifest metadata を、Standard の入力そのものと照合します。
Sysbox バイナリは Standard のビルド記録の SHA256 と ARM64 ELF も確認します。

既存 Sysbox の私有 `tmp` ディレクトリ 2 個について、Talos の展開で mode が 1777 から 0777 になる観測差だけを明示的に許容し、`mode_differences` へ記録します。
ファイル内容、通常の permission bit、それ以外の mode は一致を要求します。
これは静的なパッケージングの観測であり、runtime の検証や Oracle 専用の Sysbox 変更ではありません。
inspector のコメントにある Talos 1.12.6 は最初に観測した版を示します。同じ限定規則を、1.14.5 を含む新しい出力ごとに検査します。
固定 digest または configuration ID の Standard installer も静的検査のため export し、ARM64 loader と kernel、および埋め込み済み全拡張の内容と mode が Oracle と一致することを要求します。
検査で読み取った UKI section は、Oracle ディスク生成の入力には使用しません。

[ローカル実生成の証跡](test-evidence/issue-17-local-oracle.inputs.json) は、公開済み v0.5.0 Standard 入力から amd64 実行側で ARM64 ディスクを生成した記録です。
実装途中のため base commit と正確な生成処理 hash、`source_dirty: true` で識別し、最終 head の CI 証拠とは区別します。
PR artifact は commit 済み head から再生成し、その commit、hash、native 実行側の情報を記録します。
別々の生成物は異なるディスク hash を持ちます。

OCI は `NOT_RUN`、起動は `NOT_RUN` と記録します。
ARM64 の Sysbox 実行、Docker-in-Sysbox、kind は `UNRUN` のままです。
生成と静的検査の PASS は、A1 起動や runtime の成功を意味しません。
OCI 認証、イメージ登録、VM 操作、クラスタ bootstrap は行いません。
パーティションは EFI と META だけで、STATE、EPHEMERAL、利用側の追加 150 GiB ディスクは作りません。
MachineConfig、クラスタの識別情報、固定ネットワーク、ホスト名、テナンシ情報は埋め込みません。
初回は maintenance モードを想定し、実際の起動確認は利用側で継続します。

### Standard 入力からの生成

Linux、Docker/Buildx、既存の Python 標準ライブラリによる起動処理を使います。
今回追加するコンパイラ、パッケージ、formatter、変換、圧縮、検査ツールはコンテナ内で実行します。
ホストへの依存パッケージのインストールは不要です。
実行側は amd64 または arm64、出力側は常に arm64 です。
実行側 lock の固定 imager と、ARM64 lock の Standard 入力を区別して記録します。

ローカル ARM64 ビルドを再利用する場合は、英語節の `--build-output` の 2 コマンドを実行します。
成功した場合だけ `oracle/` と `images.json` の `disk_artifacts` が追加されます。
既存の出力やディスク記録がある場合は上書きせず停止します。
Standard と同じ順序の Sysbox、iscsi-tools、util-linux-tools を使用し、VMware の拡張は使用しません。

公開入力を再利用する場合は、この Talos 1.14.5 用の生成処理と一致する新リリースを指定し、英語節のコマンドを使用します。
v0.5.0／Talos 1.12.6 の再現には、その版に対応する repository revision と生成処理を使います。
上記の過去のローカル証跡は Talos 1.12.6 の記録です。
出力先には未作成のパスを指定します。
`--builder NAME` は既存 Buildx builder を選ぶだけで、他の builder を変更しません。
ARM64 拡張のコンテナは静的検査のため create/export しますが、起動しません。
公開済み v0.5.0 のタグと asset は変更しません。
再現とは同じ固定入力と手順で生成し、同じ検査に合格することです。
GPT/FAT の識別子などが生成ごとに変わり得るため、別々の生成物の byte 同一性は保証しません。
リリースの再実行では、元のディスクを保持して検証します。

### 隔離と失敗時の復旧

Talos 1.14.5 の公式 imager は raw ディスクへ直接書き込み、loop device や filesystem mount を必要としません。
生成コンテナは呼び出し元の UID/GID で実行し、全 capability を drop します。
no-new-privileges と既定の Docker seccomp／AppArmor を維持します。
今回の入力と出力だけを mount し、ホスト `/dev` や block device、既存ホストディスクを渡しません。
公式 imager と boot assets は変更せず、過去の loop／formatter gate は使用しません。

生成は CPU 2 個の affinity と quota、RAM 4 GiB、追加 swap なし、PID 256、`/tmp` tmpfs 2 GiB に制限します。
生成前と変換前に 12 GiB 以上の空きを要求し、小さな raw scratch と各 asset 2 GiB 未満の制約を維持します。
空き容量の減少によって失敗した場合も、成功扱いで公開しません。
変換コンテナには block node、SYS_ADMIN、ネットワークを与えず、今回の作業ディレクトリへアクセスする DAC_OVERRIDE だけを追加します。

失敗時も固有の名前と label を持つ今回のコンテナだけを停止・削除します。
loop の作成・監査・detach、一括 Docker prune、ホスト kernel/security 設定変更は行いません。
失敗時は `<output>.partial-*` に `FAILED.txt` とログを残し、成功出力は作りません。
`generator.log` と `conversion.log` を確認し、所有コンテナの後処理を確認してから今回の scratch だけを削除できます。
成功時も作業ディレクトリを証跡として残し、配布対象は成功出力内の 4 ファイルだけにします。

既存 Standard/probe の対応ソース収集では、固定した Ubuntu snapshot を維持します。
ソースファイルの取得が HTTP 503 で再試行を尽くした場合だけ、`build/ubuntu_sources.py` が apt の認証済み snapshot Sources 索引からファイル名、サイズ、SHA256/SHA512 を取得します。
公式 `archive.ubuntu.com` の pool から取得し、pool が HTTP 404 の場合だけ Ubuntu Launchpad の同一パッケージ・同一版の公開記録（librarian への転送を含む）を使用します。
索引の強い hash とサイズに完全一致する内容だけを受け付け、SHA256 も計算して記録した後、元の apt コマンドでソース一式を再検証します。
同じソースパッケージの複数版を一括指定している場合、pool の HTTP 404 では公開版を一意に決められないため取得を停止し、候補の推測は行いません。
パッケージ版と索引は変更せず、署名、hash、認証のエラーや索引自体の取得失敗は停止します。
使用した代替 URL と hash は、Standard の `ubuntu_source_fallbacks` または probe の `ubuntu-source-fallbacks.json` に記録し、対応ソースは従来どおり併配します。

### 取得方法とリリース記録

PR の Test workflow は、4 ファイル全体を `standard-oracle-arm64` artifact として 14 日間保持します。
英語節の `gh run download RUN_ID` で取得できます。
14 日間は一時的な CI artifact の保持期限です。正式な GitHub Release asset は別の配布物であり、この CI 保持期限は適用されません。
人間によるマージと新リリースの公開後は、`gh release download NEW_TAG` で取得します。

チェックサム一覧には、単独配布しない展開後 `.qcow2` の hash も含みます。
記録されたツールコンテナで圧縮ファイルを残して展開してから、一覧全体を検査します。
入力 JSON のツール image ID が手元にない場合は、記録した実行側 lock と Dockerfile からツールを再ビルドします。
zstd、sha256sum、qemu-img は、そのコンテナへ取得ディレクトリだけを mount して実行します。
SYS_ADMIN と loop device は不要です。
VMDK は利用側の OCI workflow で直接取り込む想定です。

`release-images.json` と `approved-runtime-images.json` の既存 `images` 配列は維持します。
ディスク参照は別の `disk_artifacts` に記録し、ファイル SHA256 をコンテナの manifest digest として扱いません。
元の `images.json` は各 architecture のファイル名、hash、Standard との対応を保持します。
staging は全ファイル、入力記録、チェックサムを検証してから公開処理へ進み、成功 marker は最後に書きます。
コンテナ公開済みの再実行でも元ディスクの内容を検証します。
ディスク field のない過去リリースは引き続き扱えます。
新しいリリースのビルドでは ARM64 のディスク一式を必須にします。
