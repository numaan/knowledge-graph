# Static, read-only MCP server. Build from this folder:
#   docker build -t kg-mcp:0.1.0 mcp/
FROM golang:1.26.5-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /kg-mcp .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /kg-mcp /kg-mcp
ENV MCP_HTTP_ADDR=:8000
EXPOSE 8000
ENTRYPOINT ["/kg-mcp"]
