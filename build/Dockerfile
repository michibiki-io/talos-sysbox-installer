# Build arguments are supplied exclusively from versions.lock.json by build.py.
ARG GO_IMAGE
ARG UBUNTU_IMAGE
ARG CATALOGUE_IMAGE
FROM ${CATALOGUE_IMAGE} AS catalogue
FROM ${GO_IMAGE} AS supervisor
WORKDIR /src
COPY supervisor/ ./
RUN --mount=type=cache,id=talos-sysbox-installer-go-build,target=/root/.cache/go-build CGO_ENABLED=0 go test ./... && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w -buildid=' -o /supervisor .

FROM ${UBUNTU_IMAGE} AS dependencies
COPY --from=supervisor /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=supervisor /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:$PATH GOTOOLCHAIN=local GOMAXPROCS=2
ARG APT_SNAPSHOT
COPY build/apt_retry.sh /work/apt_retry.sh
# Configure the fixed snapshot before any dependency download. Signed indexes
# and package hashes are checked by apt; expired dates are expected on snapshots.
RUN SNAPSHOT="$APT_SNAPSHOT" && test -n "$SNAPSHOT" && \
    rm -f /etc/apt/sources.list.d/* && \
    printf 'deb [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble main universe\ndeb [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble-updates main universe\ndeb [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble-security main universe\ndeb-src [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble main universe\ndeb-src [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble-updates main universe\ndeb-src [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble-security main universe\n' "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" > /etc/apt/sources.list && \
    sh /work/apt_retry.sh -o APT::Update::Error-Mode=any update && sh /work/apt_retry.sh install -y --no-install-recommends ca-certificates python3 rsync kmod iptables fuse3 dpkg-dev gcc libc6-dev libseccomp-dev pkg-config protobuf-compiler libprotobuf-dev

FROM dependencies AS assemble
COPY --from=catalogue /image-digests /work/catalogue-image-digests
COPY versions.lock.json /work/versions.lock.json
COPY build/assemble.py build/official_sources.py build/proc_transfer.py /work/
COPY LICENSE /work/LICENSE
COPY --from=supervisor /usr/local/go/LICENSE /work/GO-LICENSE
COPY --from=supervisor /usr/local/go/PATENTS /work/GO-PATENTS
COPY --from=supervisor /supervisor /work/supervisor
COPY build/ /work/project/build/
COPY supervisor/ /work/project/supervisor/
COPY versions.lock.json /work/project/versions.lock.json
COPY LICENSE /work/project/LICENSE
COPY scripts/build.py scripts/publish.py scripts/build_validation_image.py scripts/update_proc_transfer.py scripts/proc_transfer_operator.py /work/project/scripts/
ARG GENERATE_MODULE_LOCKS=0
ENV GENERATE_MODULE_LOCKS=$GENERATE_MODULE_LOCKS
RUN --mount=type=cache,id=talos-sysbox-installer-sources,target=/source-cache --mount=type=cache,id=talos-sysbox-installer-go-modules,target=/root/go/pkg/mod --mount=type=cache,id=talos-sysbox-installer-go-build,target=/root/.cache/go-build python3 /work/assemble.py

FROM scratch AS evidence
COPY --from=assemble /out/ /
COPY --from=assemble /work/project/ /project/

FROM scratch AS extension
COPY --from=assemble /out/rootfs/ /rootfs/
COPY --from=assemble /out/manifest.yaml /manifest.yaml
LABEL org.opencontainers.image.source="https://github.com/michibiki-io/talos-sysbox-installer"

# Separate validation image; no checkpoint binary or launcher enters extension.
FROM dependencies AS validation-probe
RUN sh /work/apt_retry.sh install -y --no-install-recommends meson ninja-build libcap-dev util-linux procps
COPY --from=assemble /out/validation/ /opt/proc-transfer/
COPY --from=assemble /out/build-metadata.json /opt/production-build-metadata.json
COPY --from=assemble /out/rootfs/usr/local/share/licenses/talos-sysbox/ /opt/production-licenses/
COPY --from=supervisor /usr/local/go/LICENSE /opt/validation-go-LICENSE
COPY --from=supervisor /usr/local/go/PATENTS /opt/validation-go-PATENTS
COPY versions.lock.json /work/versions.lock.json
COPY build/validation_image.py /work/validation_image.py
RUN python3 /work/validation_image.py && rm -rf /usr/local/go && \
    groupadd --gid 1001 probe && useradd --uid 1001 --gid 1001 --create-home probe && \
    chmod -R a+rX /opt/proc-transfer && chmod 755 /opt/proc-transfer/ctr
USER 1001:1001
WORKDIR /home/probe
CMD ["/bin/sleep", "infinity"]

FROM scratch AS validation-tools
COPY --from=assemble /out/validation/ /
COPY --from=validation-probe /opt/proc-transfer/image-versions.json /image-versions.json

# Export the complete extra source/license offering before release publication.
FROM scratch AS validation-sources
COPY --from=validation-probe /opt/validation-sources/ /packages-and-bubblewrap/
COPY --from=validation-probe /opt/validation-licenses/ /licenses/
COPY --from=validation-probe /opt/proc-transfer/image-versions.json /image-versions.json
COPY --from=assemble /out/build-metadata.json /production-build-metadata.json
COPY --from=assemble /out/validation/artifacts.lock.json /artifacts.lock.json
# The validation image's inherited builder layers also carry the Go toolchain.
# Retain its matching source and notices in the release source offering.
COPY --from=supervisor /usr/local/go/ /go-toolchain/
