# 示例简化 Dockerfile：发布目标 runtime
# 全局构建参数（合成参数见 build-params.json）
ARG BASE_REGISTRY=registry.example.com
ARG BASE_IMAGE=team/builder
ARG BASE_TAG=v3

# 1) 间接可变标签：builder 用 tag 引用，但审批清单里只有该 tag 对应的 digest 记录
FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG} AS builder
RUN make /out

# 2) 已固定内容的运行时基座，且审批清单同源同摘要
FROM registry.example.com/runtime/alpine@sha256:1111111111111111111111111111111111111111111111111111111111111111 AS runtime
COPY --from=builder /out /out
# 3) COPY --from 直接拉外部工具，仍是可变标签
COPY --from=registry.example.com/tools/jq:1.7 /bin/jq /bin/jq

# 4) 测试阶段：可达但仅供测试，不参与门禁
FROM flaky/testrunner:nightly AS unit-test
RUN npm test

# 5) 发布聚合阶段（真正的发布目标）
FROM runtime AS release
COPY --from=unit-test /reports /reports
COPY --from=certs-stage /etc/ssl /etc/ssl

# 6) 未使用阶段：其中的外部材料不影响门禁
FROM untrusted/volatile:latest AS legacy
RUN echo no-target-references-this-stage

# 7) 同名材料不同来源：实际引用 registry.example.com，审批摘要却属于 mirror.corp.internal
FROM registry.example.com/infra/cacerts:2026 AS certs-stage
