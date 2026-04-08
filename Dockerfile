FROM python:3.13-slim-trixie AS python
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python AS builder

WORKDIR /var/ravnar

COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync \
    --no-progress --link-mode=copy --compile-bytecode \
    --frozen --python=$(which python) --no-default-groups --extra=serve --no-install-project

# The README is required to build the package as it is considered metadata
COPY README.md ./
COPY src ./src
ARG VERSION
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    UV_DYNAMIC_VERSIONING_BYPASS=${VERSION} \
    uv pip install \
    --no-progress --link-mode=copy --compile-bytecode \
    --no-deps .

FROM python AS runtime

RUN groupadd --gid 1000 huginn && \
    useradd --shell "$(which bash)" --uid 1000 --gid 1000 huginn
USER huginn

WORKDIR /var/ravnar/plugins
ENV RAVNARPATH="/var/ravnar/plugins"

COPY config-docker.yml /etc/ravnar/config.yml

WORKDIR /var/ravnar
COPY --from=builder --chown=huginn:huginn /var/ravnar/.venv /var/ravnar/.venv
ENV PATH="/var/ravnar/.venv/bin:${PATH}"

ENTRYPOINT ["ravnar"]
CMD ["serve"]

HEALTHCHECK \
    --start-period=30s \
    --start-interval=1s \
    --interval=30s \
    --timeout=5s \
    --retries=3 \
    CMD ["ravnar", "health"]
