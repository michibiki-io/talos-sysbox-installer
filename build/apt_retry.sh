#!/bin/sh
# Retry the same signed, fixed-snapshot operation; never change repositories.
set -eu
for attempt in 1 2 3; do
    if apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"; then
        exit 0
    else
        status=$?
    fi
    if [ "$attempt" -eq 3 ]; then
        exit "$status"
    fi
    echo "APT operation failed; retrying the same snapshot (attempt $attempt/3)" >&2
    sleep 5
done
