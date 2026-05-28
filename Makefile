# Convenience wrappers around podman / pytest.
#
# Targets:
#   make test          run the python test suite
#   make build         build the peers container image (podman)
#   make run TARGET=…  drive peers on a target project (one-shot)
#   make shell TARGET= drop into a shell in the container, mounted on TARGET
#   make hooks-install install the local pre-push test hook
#   make clean         remove the image
#
# Why podman build / podman run directly instead of podman compose?
# `podman compose` delegates to docker-compose, which requires a running
# podman socket. The direct invocations always work.

IMAGE        ?= peers:dev
PROXY_IMAGE  ?= peers-egress-proxy:dev
AUTH_PROXY_IMAGE ?= peers-auth-proxy:dev
TARGET       ?= $(CURDIR)
HOST_HOME    ?= $(HOME)
GIT_SHA      ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
# Container rebuilds need outbound network for apt/npm/pip. Use host
# networking by default to avoid rootless `pasta` surprises on hosts
# without /dev/net/tun; override with BUILD_NETWORK=none/slirp4netns if
# your environment requires it.
BUILD_NETWORK ?= host
# If `pasta` networking fails on your host ("/dev/net/tun: No such
# device"), set NETWORK=slirp4netns (older fallback) or NETWORK=host.
NETWORK      ?=

.PHONY: test build proxy-build auth-proxy-build run shell hooks-install clean help

help:
	@awk '/^## / {sub(/^## /,""); print}' $(MAKEFILE_LIST)

## test           — run the python test suite
test:
	python3 -m pytest

## build          — build the container image
build:
	podman build --network=$(BUILD_NETWORK) \
		--build-arg PEERS_BUILD_REF=$(GIT_SHA) \
		-f Containerfile -t $(IMAGE) .

## proxy-build    — build the egress-proxy sidecar image (Phase-2 hardening)
proxy-build:
	podman build --network=$(BUILD_NETWORK) \
		-f proxy/Containerfile.proxy -t $(PROXY_IMAGE) proxy/

## auth-proxy-build — build the OAuth auth-proxy sidecar image
auth-proxy-build:
	podman build --network=$(BUILD_NETWORK) \
		-f auth-proxy/Containerfile -t $(AUTH_PROXY_IMAGE) .

## run            — run peers on TARGET (defaults to cwd); pass ARGS=...
run:
	podman run --rm -it \
		--userns=keep-id \
		$(if $(NETWORK),--network=$(NETWORK),) \
		--cap-drop=ALL \
		--security-opt=no-new-privileges \
		-v $(TARGET):/work \
		-v $(HOST_HOME)/.claude:/home/peer/.claude \
		-v $(HOST_HOME)/.codex:/home/peer/.codex \
		$(IMAGE) $(ARGS)

## init-target    — run `peers init` inside TARGET
init-target:
	$(MAKE) run ARGS=init

## status         — run `peers status` against TARGET
status:
	$(MAKE) run ARGS=status

## shell          — drop into a bash shell inside the container, mounted on TARGET
shell:
	podman run --rm -it \
		--userns=keep-id \
		$(if $(NETWORK),--network=$(NETWORK),) \
		--cap-drop=ALL \
		--security-opt=no-new-privileges \
		-v $(TARGET):/work \
		-v $(HOST_HOME)/.claude:/home/peer/.claude \
		-v $(HOST_HOME)/.codex:/home/peer/.codex \
		--entrypoint bash $(IMAGE)

## hooks-install  — install local pre-push checks
hooks-install:
	cp scripts/pre-push.sh .git/hooks/pre-push
	chmod +x .git/hooks/pre-push

## clean          — remove the built images (main + proxy)
clean:
	-podman rmi $(IMAGE)
	-podman rmi $(PROXY_IMAGE)
	-podman rmi $(AUTH_PROXY_IMAGE)
