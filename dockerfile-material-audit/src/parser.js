/**
 * 简化 Dockerfile 解析器（只识别 FROM、ARG、COPY --from）。
 *
 * 输出保留原始行号，指令结构：
 *   { kind: 'ARG', line, name, default: string|null }
 *   { kind: 'FROM', line, image: string, alias: string|null, platform: string|null }
 *   { kind: 'COPY_FROM', line, from: string }
 *
 * 其余指令（RUN/ENV/WORKDIR 等）被忽略但行号仍计入；
 * 支持反斜杠折行（折行后逻辑行的行号取起始物理行）；
 * 行首 # 为注释，空行跳过。
 */

const ARG_RE = /^ARG\s+(.+)$/i;
const FROM_RE = /^FROM\s+(.+)$/i;
const COPY_RE = /^COPY\s+(.+)$/i;

/**
 * @param {string} content
 * @returns {Array<object>} 指令记录（按文件顺序）
 */
export function parseDockerfile(content) {
  const physicalLines = String(content).split(/\r?\n/);

  // 先把折行合并成逻辑行，记录起始行号
  const logical = [];
  let buffer = '';
  let startLine = 0;
  for (let i = 0; i < physicalLines.length; i += 1) {
    const lineNo = i + 1;
    const line = physicalLines[i];
    const isContinuation = /\\\s*$/.test(line);
    if (buffer === '' && !isContinuation) {
      logical.push({ line: lineNo, text: line });
    } else if (buffer === '' && isContinuation) {
      startLine = lineNo;
      buffer = line.replace(/\\\s*$/, ' ');
    } else if (isContinuation) {
      buffer += line.replace(/\\\s*$/, ' ');
    } else {
      buffer += line;
      logical.push({ line: startLine, text: buffer });
      buffer = '';
    }
  }
  if (buffer !== '') {
    logical.push({ line: startLine, text: buffer });
  }

  const instructions = [];
  for (const { line, text } of logical) {
    const trimmed = text.trim();
    if (trimmed === '' || trimmed.startsWith('#')) {
      continue;
    }

    const argMatch = trimmed.match(ARG_RE);
    if (argMatch) {
      const body = argMatch[1].trim();
      const eq = body.indexOf('=');
      if (eq === -1) {
        instructions.push({ kind: 'ARG', line, name: body.trim(), default: null });
      } else {
        const name = body.slice(0, eq).trim();
        const def = body.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
        instructions.push({ kind: 'ARG', line, name, default: def });
      }
      continue;
    }

    const fromMatch = trimmed.match(FROM_RE);
    if (fromMatch) {
      const tokens = fromMatch[1].trim().split(/\s+/);
      let platform = null;
      const rest = [];
      for (let i = 0; i < tokens.length; i += 1) {
        if (tokens[i] === '--platform' && i + 1 < tokens.length) {
          platform = tokens[i + 1];
          i += 1;
        } else if (tokens[i].startsWith('--platform=')) {
          platform = tokens[i].slice('--platform='.length);
        } else {
          rest.push(tokens[i]);
        }
      }
      // 形如：FROM image AS alias
      const image = rest[0];
      const asIndex = rest.findIndex((t) => t.toUpperCase() === 'AS');
      const alias = asIndex !== -1 && asIndex + 1 < rest.length ? rest[asIndex + 1] : null;
      instructions.push({ kind: 'FROM', line, image, alias, platform });
      continue;
    }

    const copyMatch = trimmed.match(COPY_RE);
    if (copyMatch) {
      const tokens = copyMatch[1].trim().split(/\s+/);
      const fromToken = tokens.find((t) => t.startsWith('--from='));
      if (fromToken) {
        instructions.push({ kind: 'COPY_FROM', line, from: fromToken.slice('--from='.length) });
      }
    }
  }

  return instructions;
}
