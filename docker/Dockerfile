FROM ubuntu:24.04

ARG TARGETARCH=x86_64

# Set noninteractive mode for apt-get
ENV DEBIAN_FRONTEND=noninteractive

# Set timezone
ENV TZ=Asia/Shanghai

# Install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bash git sudo jq curl wget gzip locales \
        build-essential cmake pkg-config lld ninja-build \
        binutils binutils-dev autoconf automake libtool libncurses5-dev libgdbm-dev libnss3-dev liblzma-dev zlib1g-dev libyaml-dev graphviz libgraphviz-dev \
        libicu-dev libcurl4-openssl-dev libssl-dev libsqlite3-dev ruby-dev libreadline-dev libffi-dev libbz2-dev libc++-dev libc++abi-dev libc6-dev libgcc-9-dev tzdata \
        libyaml-dev ca-certificates apt-transport-https gnupg neofetch texinfo tree gnuplot graphviz graphviz-dev tmux  && \
        ln -sf /usr/share/zoneinfo/$TZ /etc/localtime && \
        dpkg-reconfigure -f noninteractive tzdata

# Install secondary dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    lsb-release software-properties-common gnupg pixz

# Locale settings
RUN locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8

# Install llvm
RUN apt-get update && apt-get install -y lsb-release wget software-properties-common gnupg && \
    wget https://apt.llvm.org/llvm.sh && \
    chmod +x llvm.sh && \
    ./llvm.sh 21 all && \
    rm llvm.sh && \
    LLVM_BIN_DIR="/usr/lib/llvm-21/bin" && \
    MASTER_BIN="$LLVM_BIN_DIR/clang" && \
    if [ -f "$MASTER_BIN" ]; then \
        CMD="update-alternatives --install /usr/bin/clang clang $MASTER_BIN 100" && \
        for f in $LLVM_BIN_DIR/*; do \
            TOOL_NAME=$(basename "$f"); \
            if [ "$TOOL_NAME" != "clang" ] && [ -x "$f" ] && [ ! -d "$f" ]; then \
                CMD="$CMD --slave /usr/bin/$TOOL_NAME $TOOL_NAME $f"; \
            fi; \
        done && \
        eval "$CMD"; \
    else \
        echo "Error: $MASTER_BIN not found." && exit 1; \
    fi && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install AFLplusplus
RUN git clone https://github.com/AFLplusplus/AFLplusplus.git /tmp/AFLplusplus && \
     cd /tmp/AFLplusplus && \
     sed -i 's|^#define USE_COLOR|/* #define USE_COLOR */|g' include/config.h && \
     export LLVM_CONFIG=llvm-config && \
     make all -j$(nproc) && \
     make install && \
     rm -rf /tmp/AFLplusplus

# Install honggfuzz
# RUN git clone https://github.com/google/honggfuzz.git /tmp/honggfuzz && \
#     cd /tmp/honggfuzz && \
#     make -j$(nproc) && \
#     make -j$(nproc) install && \
#     rm -rf /tmp/honggfuzz

# Install code coverage utils
RUN git clone https://chromium.googlesource.com/chromium/src/tools/code_coverage /tmp/code_coverage && \
    cd /tmp/code_coverage && \
    mv coverage_utils.py /usr/local/bin/coverage_utils && \
    sed -i '1i#!/usr/bin/env python3' /usr/local/bin/coverage_utils && \
    chmod +x /usr/local/bin/coverage_utils && \
    mv html_templates/ /usr/local/bin/ && \
    mv static/ /usr/local/bin/

# Install Rust and casr
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    echo 'source $HOME/.cargo/env' >> ~/.bashrc
ENV PATH="/root/.cargo/bin:${PATH}"
RUN cargo install -F dojo casr
RUN cargo install treereduce-c

# Install Ruby
# RUN git clone https://github.com/rbenv/rbenv.git ~/.rbenv && \
    # cd ~/.rbenv && src/configure && make -j$(nproc) -C src && \
    # echo 'export PATH="$HOME/.rbenv/bin:$PATH"' >> ~/.bashrc && \
    # echo 'eval "$(rbenv init -)"' >> ~/.bashrc && \
    # . ~/.bashrc

# Initialize Git
RUN git config --global user.email "example@example.com" && \
    git config --global user.name "example"

# Add `ls` alias
RUN echo "alias ls='ls -F'" >> ~/.bashrc


RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    /root/.local/bin/uv python install 3.12 

ENV PATH="/root/.local/bin:${PATH}"

ENV PATH="/root/FuzzAgent/.venv/bin:${PATH}"
ENV CMAKE_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu"
    

# Clean the environment variables
ENV DEBIAN_FRONTEND=dialog


# For easy debug (tmux and pwndbg).
# pwndbg's setup.sh installs gdb via sudo, which drops http_proxy.
# Write the build proxy into apt so that step still reaches the Ubuntu archive.
RUN --mount=type=secret,id=github_token,required=false \
    proxy="${http_proxy:-${HTTP_PROXY:-}}"; \
    sprox="${https_proxy:-${HTTPS_PROXY:-$proxy}}"; \
    if [ -n "$proxy" ]; then \
      printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$proxy" "$sprox" > /etc/apt/apt.conf.d/99proxy; \
      printf 'Defaults env_keep += "http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY"\n' > /etc/sudoers.d/99proxy; \
      chmod 440 /etc/sudoers.d/99proxy; \
    fi; \
    if [ -s /run/secrets/github_token ]; then \
      basic="$(printf 'x-access-token:%s' "$(cat /run/secrets/github_token)" | base64 -w0)"; \
      export GIT_CONFIG_COUNT=1; \
      export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"; \
      export GIT_CONFIG_VALUE_0="Authorization: Basic ${basic}"; \
      unset basic; \
    fi; \
    cd /root && git clone --single-branch https://github.com/gpakosz/.tmux.git && \
    ln -s -f .tmux/.tmux.conf && \
    cp .tmux/.tmux.conf.local . && \
    git clone --depth 1 https://github.com/pwndbg/pwndbg.git /tmp/pwndbg && \
    cd /tmp/pwndbg && \
    git submodule update --init --recursive && \
    DEBIAN_FRONTEND=noninteractive ./setup.sh && \
    rm -f /root/.gitconfig


# Install later dependencies. The LLVM step deletes apt lists, so update again.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bubblewrap rsync yasm && \
    rm -f /etc/apt/apt.conf.d/99proxy /etc/sudoers.d/99proxy && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Golang dependencies
RUN wget https://go.dev/dl/go1.25.1.linux-amd64.tar.gz && \
    tar -C /usr/local -xzf go1.25.1.linux-amd64.tar.gz && \
    rm go1.25.1.linux-amd64.tar.gz 

ENV PATH=$PATH:/usr/local/go/bin
ENV GOPATH=/go
ENV PATH=$PATH:$GOPATH/bin

RUN go install github.com/boyter/scc/v3@latest


# --- hc runtime: coding-agent CLIs + herdr-core -----------------------------
# Node is needed by the codex/dsh-tui CLIs.  Pin the agent CLIs so a rebuild
# does not silently change behaviour.  grok comes from its own installer (below);
# pi is installed from its own package when the campaign needs it.
RUN apt-get update && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs python3 python3-yaml && \
    npm install -g @openai/codex@0.163.0 @deepseek-ai/dsh@0.2.0-rc.2 \
        @deepseek-harness-tui/dsh-tui@0.19.0 @earendil-works/pi-coding-agent@1.5.2 && \
    npm cache clean --force && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# grok CLI (xAI): official installer, pinned.  It unpacks a statically linked
# binary into $HOME/.grok and symlinks /usr/local/bin/grok itself; the fallback
# link covers installer versions that only edit shell rc files, and keeps grok
# reachable from the per-role HOME directories / non-login shells.
ARG GROK_VERSION=1.0.77
RUN curl -fsSL https://x.ai/cli/install.sh | bash -s "${GROK_VERSION}" && \
    (command -v grok >/dev/null || ln -sf /root/.grok/bin/grok /usr/local/bin/grok) && \
    grok --version

# herdr-core (the `hc` binary): use docker/herdr-core when it is supplied
# (offline builds), otherwise fetch the pinned release with the official
# installer.  Either way the image ends up with `hc` on PATH.
ARG HC_VERSION=0.1.18
COPY docker/ /tmp/docker/
RUN set -eux; \
    if [ -f /tmp/docker/herdr-core ]; then \
        install -m 0755 /tmp/docker/herdr-core /usr/local/bin/hc; \
    else \
        curl -fsSL https://releases.herdr.dev/core/install.sh \
            | HC_VERSION="${HC_VERSION}" HC_INSTALL_DIR=/usr/local/bin sh; \
    fi; \
    rm -rf /tmp/docker; \
    hc --version

# hc-tui: terminal UI for the hc session already running in this image.
# The core is installed above, so skip the installer's own hc download.
# Keep this pin in sync with HC_TUI_VERSION in bin/hc-tui.sh.
ARG HC_TUI_VERSION=0.1.2
RUN curl -fsSL https://followcat.github.io/hc-tui/install.sh \
        | HC_TUI_VERSION="${HC_TUI_VERSION}" \
          HC_TUI_INSTALL_DIR=/usr/local/bin \
          HC_TUI_SKIP_CORE=1 \
          sh \
    && hc-tui --version

# The MCP server every agent CLI spawns; the controller entrypoint.
# hc-tui-attach opens the TUI on the container's existing hc session.
COPY bin/fuzzagent-mcp bin/campaignctl bin/hc-tui-attach /usr/local/bin/
RUN chmod +x /usr/local/bin/fuzzagent-mcp /usr/local/bin/campaignctl /usr/local/bin/hc-tui-attach

# --- project Python environment (source stays mounted, deps are baked) ------
# `uv run` at runtime must not need the network: pin the project environment
# into the image and disable implicit syncs.
COPY pyproject.toml uv.lock README.md /opt/fuzzagent-build/
ENV UV_PROJECT_ENVIRONMENT=/opt/fuzzagent-build/.venv
RUN /root/.local/bin/uv sync --directory /opt/fuzzagent-build --frozen --no-install-project
ENV UV_NO_SYNC=1
ENV PATH="/opt/fuzzagent-build/.venv/bin:${PATH}"
# We Must mapping our source code to this path in the container (DEV Phase)
WORKDIR /root/FuzzAgent

# Agent snapshots commit the output tree. Without an identity, git commit
# fails and the process dies on `git rev-parse HEAD`.
RUN git config --global user.name "fuzzagent" && \
    git config --global user.email "fuzzagent@localhost"

#ENTRYPOINT ["python", "main.py"]
