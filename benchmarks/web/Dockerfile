FROM ubuntu:22.04@sha256:9abcaffc92a5279fff3a6a41635c0800ea33e528425fb389658806476e7defde
RUN apt-get update && apt-get install -y --no-install-recommends \
    libstdc++6 libtinfo6 libssl3 zlib1g libgcc-s1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
