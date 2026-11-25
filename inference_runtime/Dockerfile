FROM golang:1.26.3-alpine@sha256:f44b851aa23dfa219d18db6eab743203245429d355cb619cf96a2ffe2a84ba7a AS build
WORKDIR /src
COPY go.mod *.go ./
ENV GOTOOLCHAIN=local CGO_ENABLED=0 GOPROXY=off
RUN go test ./... && go build -mod=readonly -trimpath -buildvcs=false -ldflags="-s -w -buildid=" -o /gateway .

# Official ggml-org server-cuda, linux/amd64, b12679, CUDA 12.8.1.
FROM ghcr.io/ggml-org/llama.cpp@sha256:3f825497190fed44861c374495ad662c995c9f3efc375bcfea4f560734b6a6a3
COPY --from=build /gateway /usr/local/bin/aob-inference
USER 10001:10001
ENV PORT=8080 PORT_HEALTH=8080 MODEL_SOURCE=cache
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=2s --start-period=181s CMD curl -fsS "http://127.0.0.1:${PORT_HEALTH}/readyz" || exit 1
ENTRYPOINT ["/usr/local/bin/aob-inference"]
