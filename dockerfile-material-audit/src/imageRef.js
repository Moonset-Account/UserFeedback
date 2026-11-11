/**
 * 容器镜像引用解析（纯文本规则，不访问任何注册表）。
 *
 * 支持的形态：
 *   ubuntu                              -> registry=docker.io, repository=library/ubuntu
 *   library/ubuntu:22.04
 *   nginx:1.27
 *   ghcr.io/org/app:tag
 *   registry.example.com:5000/ns/img@sha256:...
 *   localhost:5000/img@sha256:...
 *
 * 判定规则（与 Docker 语义一致）：
 *   - 第一段含 "."、":" 或为 "localhost" 时视为 registry host，否则归 docker.io。
 *   - 无 registry 且路径只有一段时，补 library/ 前缀（Docker Hub 官方镜像）。
 *   - digest 与 tag 可同时存在（img:tag@digest），带 digest 即视为内容固定。
 */

const DEFAULT_REGISTRY = 'docker.io';

/**
 * @param {string} raw 原始镜像引用（变量已解析后的字符串）
 * @returns {{
 *   raw: string,
 *   registry: string,
 *   repository: string,
 *   tag: string|null,
 *   digest: string|null,
 *   sourceKey: string,
 *   pinned: boolean,
 *   isLocalName: boolean
 * }}
 */
export function parseImageRef(raw) {
  const reference = String(raw).trim();
  if (!reference) {
    throw new Error('镜像引用为空');
  }

  let rest = reference;
  let digest = null;
  const atIndex = rest.indexOf('@');
  if (atIndex !== -1) {
    digest = rest.slice(atIndex + 1);
    rest = rest.slice(0, atIndex);
  }

  let tag = null;
  // tag 只取最后一个冒号（registry host 的端口冒号在第一段，不受影响）
  const colonIndex = rest.lastIndexOf(':');
  const slashIndex = rest.indexOf('/');
  if (colonIndex !== -1 && colonIndex > slashIndex) {
    tag = rest.slice(colonIndex + 1);
    rest = rest.slice(0, colonIndex);
  }

  let registry = DEFAULT_REGISTRY;
  const firstSlash = rest.indexOf('/');
  const firstSegment = firstSlash === -1 ? rest : rest.slice(0, firstSlash);
  const looksLikeHost =
    firstSegment.includes('.') ||
    firstSegment.includes(':') ||
    firstSegment === 'localhost';

  if (firstSlash !== -1 && looksLikeHost) {
    registry = firstSegment;
    rest = rest.slice(firstSlash + 1);
  }

  let repository = rest;
  if (registry === DEFAULT_REGISTRY && !repository.includes('/')) {
    repository = `library/${repository}`;
  }

  return {
    raw: reference,
    registry,
    repository,
    tag,
    digest,
    // 来源身份：同源判定键（同名不同源靠 registry 区分）
    sourceKey: `${registry}/${repository}`,
    // 内容固定：只有带 @digest 才算
    pinned: digest !== null,
    isLocalName: !looksLikeHost && firstSlash === -1,
  };
}
