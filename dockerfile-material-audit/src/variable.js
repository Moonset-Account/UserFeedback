/**
 * Dockerfile 变量替换（简化版，覆盖审计所需的两种语法）：
 *   $VAR        非花括号形式，变量名为字母/数字/下划线的最长匹配
 *   ${VAR}      花括号形式
 *   ${VAR:-df}  带默认值：变量未定义或为空时使用默认值（默认值内不再二次展开）
 *
 * 与 Docker 的差异（有意简化）：不支持 ${VAR:+...}、${VAR?err}、转义 \$。
 */

const BRACED_RE = /\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}/g;
const PLAIN_RE = /\$([A-Za-z_][A-Za-z0-9_]*)/g;

/**
 * @param {string} input
 * @param {Record<string,string>} vars 已确定的变量值（ARG 默认值 + 构建参数覆盖后的结果）
 * @returns {{ value: string, unresolved: string[] }} unresolved 为去重后的未解析变量名
 */
export function substitute(input, vars) {
  const unresolvedSet = new Set();

  const value = input.replace(BRACED_RE, (match, name, defVal) => {
    if (Object.prototype.hasOwnProperty.call(vars, name) && vars[name] !== '') {
      return vars[name];
    }
    if (defVal !== undefined) {
      return defVal;
    }
    unresolvedSet.add(name);
    return match;
  }).replace(PLAIN_RE, (match, name) => {
    // 花括号形式已被前一步消费，剩余的 $VAR 才会走到这里
    if (Object.prototype.hasOwnProperty.call(vars, name) && vars[name] !== '') {
      return vars[name];
    }
    unresolvedSet.add(name);
    return match;
  });

  return { value, unresolved: [...unresolvedSet] };
}
