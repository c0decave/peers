FROM debian:stable-slim

RUN apt-get update && apt-get install -y \
      git curl jq ripgrep tmux ca-certificates \
      python3 python3-pip nodejs npm sudo \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code
RUN npm install -g @openai/codex || true
# opencode: optional first-class peer tool (universal model gateway / local
# models). `|| true` keeps the build resilient if the registry is unreachable;
# a config that uses an `opencode` peer needs this present. Auth + config are
# bind-mounted at run time (~/.config/opencode, ~/.local/share/opencode).
RUN npm install -g opencode-ai || true

# Ship a test runner in the image. Without it, implement-mode peers
# improvise — vendoring pytest into a working-tree `.local/` and dropping
# a stray top-level `pytest.py` shim, which pollutes the repo and slips
# past every cleanliness gate (they scan `src/`). Provide the common
# Python test stack so the acceptance command just works.
RUN pip3 install --break-system-packages pytest pytest-timeout hypothesis ruff mypy

COPY . /opt/peers
RUN pip3 install --break-system-packages /opt/peers

# Fixed UID/GID inside the container. Bind-mount permission alignment
# with the host user is handled at runtime via `--userns=keep-id`
# (podman) — no build-time args needed.
RUN groupadd -g 1000 peer && useradd -m -u 1000 -g 1000 peer
# claude-code 2.1.145+ requires a WRITABLE ~/.claude.json at the home root, but
# ~ is a read-only image layer at runtime. Symlink it into the
# always-rw ~/.claude bind-mount so claude-code can create/restore its config.
# In the hardened auth-proxy default, peers does NOT bind-mount ~/.claude.json
# (the token lives in the auth-proxy sidecar); without this symlink claude-code
# 2.1.145 hangs forever in its "configuration file not found / restore backup"
# loop and never makes a single API call. In bypass mode the real host
# ~/.claude.json bind-mounts OVER this symlink, so the symlink is inert there.
# See docs/2026-06-06-claude-2.1.145-config-hang.md.
RUN ln -sfn .claude/.claude.json ~/.claude.json \
    && chown -h peer:peer ~/.claude.json
USER peer
WORKDIR /work

ARG PEERS_BUILD_REF=unknown
ENV PEERS_BUILD_REF=$PEERS_BUILD_REF
LABEL org.opencontainers.image.revision=$PEERS_BUILD_REF

ENTRYPOINT ["peers"]
CMD ["run"]
