# All executable inputs come from the selected immutable executor lock.
ARG GO_IMAGE
ARG UBUNTU_IMAGE
ARG IMAGER_IMAGE
FROM ${GO_IMAGE} AS launcher
WORKDIR /src
ENV GOMAXPROCS=2
COPY build/oracle/loop.go ./loop.go
RUN CGO_ENABLED=0 go build -p=2 -trimpath -ldflags='-s -w -buildid=' -o /loop-gate loop.go

FROM ${IMAGER_IMAGE} AS generator
COPY --from=launcher /loop-gate /oracle/loop-gate
COPY --from=launcher /loop-gate /oracle/mkfs.vfat
ENTRYPOINT ["/oracle/loop-gate"]

FROM ${UBUNTU_IMAGE} AS tools
ARG APT_SNAPSHOT
COPY --from=launcher /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY build/apt_retry.sh /oracle/apt_retry.sh
RUN test -n "$APT_SNAPSHOT" && rm -f /etc/apt/sources.list.d/* && \
    printf 'deb [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble main universe\ndeb [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble-updates main universe\ndeb [check-valid-until=no] http://snapshot.ubuntu.com/ubuntu/%s/ noble-security main universe\n' "$APT_SNAPSHOT" "$APT_SNAPSHOT" "$APT_SNAPSHOT" > /etc/apt/sources.list && \
    sh /oracle/apt_retry.sh -o APT::Update::Error-Mode=any update && \
    sh /oracle/apt_retry.sh install -y --no-install-recommends python3 python3-yaml qemu-utils zstd gdisk mtools squashfs-tools && \
    dpkg-query -W > /oracle/packages.txt && rm -rf /var/lib/apt/lists/*
COPY build/oracle/inspect.py /oracle/inspect.py
ENTRYPOINT ["python3", "/oracle/inspect.py"]
