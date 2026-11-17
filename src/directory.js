// 内部包目录处理：按 (生态, 原始显示名) 检测重复目录项，并建立按生态分组的索引。
// 重复目录项必须原样保留参与匹配，同时单独形成诊断，不得静默去重。

/**
 * @param {Array} entries 形如 [{ ecosystem, name, path?, metadata? }]
 * @returns {{ grouped: Map, duplicates: Array, diagnostics: Array }}
 */
export function indexDirectory(entries) {
  const grouped = new Map();
  const seen = new Map();
  const duplicates = [];
  const diagnostics = [];

  for (const entry of entries || []) {
    const ecosystem = String(entry.ecosystem);
    const name = String(entry.name);
    const key = `${ecosystem}\u0000${name}`;

    if (!grouped.has(ecosystem)) grouped.set(ecosystem, []);
    grouped.get(ecosystem).push({ ...entry, ecosystem, name });

    const prior = seen.get(key);
    if (prior) {
      const dup = {
        code: 'DUPLICATE_DIRECTORY_ENTRY',
        ecosystem,
        name,
        firstPath: prior.path ?? null,
        duplicatePath: entry.path ?? null,
        message: `内部目录中生态 ${ecosystem} 的包 "${name}" 出现重复条目`,
      };
      duplicates.push(dup);
      diagnostics.push(dup);
    } else {
      seen.set(key, entry);
    }
  }

  return { grouped, duplicates, diagnostics };
}
