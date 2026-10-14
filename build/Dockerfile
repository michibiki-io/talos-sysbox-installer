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
COPY build/assemble.py build/official_sources.py /work/
COPY LICENSE /work/LICENSE
COPY --from=supervisor /usr/local/go/LICENSE /work/GO-LICENSE
COPY --from=supervisor /usr/local/go/PATENTS /work/GO-PATENTS
COPY --from=supervisor /supervisor /work/supervisor
COPY build/ /work/project/build/
COPY supervisor/ /work/project/supervisor/
COPY versions.lock.json /work/project/versions.lock.json
COPY LICENSE /work/project/LICENSE
COPY scripts/build.py scripts/publish.py /work/project/scripts/
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
