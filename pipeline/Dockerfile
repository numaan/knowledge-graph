# kg-pipeline: Dagster user code with Tree-sitter, the ODM parser and SCIP indexers.
#   docker build -t kg-pipeline:0.1.0 .
#   docker build --build-arg WITH_JAVA=0 --build-arg WITH_DOTNET=0 -t kg-pipeline:0.1.0-slim .
ARG PYTHON_IMAGE=python:3.13.18-slim-bookworm

FROM golang:1.26.10-bookworm AS go-tools
RUN GOBIN=/out go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.9

FROM node:22.23.4-bookworm-slim AS node

FROM ${PYTHON_IMAGE}
ARG WITH_JAVA=1
ARG WITH_DOTNET=1
ENV PATH=/usr/local/go/bin:/opt/dotnet:$PATH DOTNET_ROOT=/opt/dotnet DOTNET_CLI_TELEMETRY_OPTOUT=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && if [ "$WITH_JAVA" = "1" ]; then apt-get install -y --no-install-recommends openjdk-17-jdk-headless maven; fi \
 && if [ "$WITH_DOTNET" = "1" ]; then apt-get install -y --no-install-recommends libicu72; fi \
 && rm -rf /var/lib/apt/lists/*

# Node.js for scip-python and scip-typescript
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && npm install -g @sourcegraph/scip-python@0.6.8 @sourcegraph/scip-typescript@0.6.0 \
 && npm cache clean --force

# Go toolchain (scip-go loads packages with `go list`)
COPY --from=go-tools /usr/local/go /usr/local/go
COPY --from=go-tools /out/scip-go /usr/local/bin/scip-go

# Java: scip-java as a standalone launcher
RUN if [ "$WITH_JAVA" = "1" ]; then \
      curl -fsSLo /tmp/coursier https://github.com/coursier/coursier/releases/download/v2.1.26/coursier \
      && chmod +x /tmp/coursier \
      && /tmp/coursier bootstrap --standalone -o /usr/local/bin/scip-java org.scip-code:scip-java:0.13.2 \
           --main org.scip_code.scip_java.ScipJava \
      && rm /tmp/coursier; \
    fi

# C#: .NET 8 SDK and scip-dotnet
RUN if [ "$WITH_DOTNET" = "1" ]; then \
      curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
      && bash /tmp/dotnet-install.sh --channel 8.0 --install-dir /opt/dotnet \
      && /opt/dotnet/dotnet tool install --tool-path /usr/local/bin scip-dotnet \
      && rm /tmp/dotnet-install.sh; \
    fi

WORKDIR /app
COPY pyproject.toml ./
COPY kg_pipeline ./kg_pipeline
RUN pip install --no-cache-dir ".[dagster]"

# /opt/dagster/dagster_home: the Helm chart sets DAGSTER_HOME there; run pods write compute logs under it
RUN useradd --create-home --uid 10001 kg && git config --system --add safe.directory '*' \
 && mkdir -p /opt/dagster/dagster_home && chown -R 10001 /opt/dagster
USER 10001
EXPOSE 3030
CMD ["dagster", "api", "grpc", "--module-name", "kg_pipeline.definitions", "--host", "0.0.0.0", "--port", "3030"]
