FROM debian:stable-slim

RUN apt-get update && apt-get install -y \
      git curl jq ripgrep tmux ca-certificates \
      python3 python3-pip nodejs npm sudo \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code
RUN npm install -g @openai/codex || true

COPY . /opt/peers
RUN pip3 install --break-system-packages /opt/peers

# Fixed UID/GID inside the container. Bind-mount permission alignment
# with the host user is handled at runtime via `--userns=keep-id`
# (podman) — no build-time args needed.
RUN groupadd -g 1000 peer && useradd -m -u 1000 -g 1000 peer
USER peer
WORKDIR /work

ARG PEERS_BUILD_REF=unknown
ENV PEERS_BUILD_REF=$PEERS_BUILD_REF
LABEL org.opencontainers.image.revision=$PEERS_BUILD_REF

ENTRYPOINT ["peers"]
CMD ["run"]
