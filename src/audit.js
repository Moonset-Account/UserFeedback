'use strict';

const fs = require('node:fs');

/** 追加式审计日志（JSON Lines）。 */
class AuditLog {
  constructor(filePath) {
    this.filePath = filePath;
  }

  write(entry) {
    fs.appendFileSync(this.filePath, JSON.stringify(entry) + '\n', 'utf8');
  }

  readEntries() {
    if (!fs.existsSync(this.filePath)) return [];
    return fs
      .readFileSync(this.filePath, 'utf8')
      .split('\n')
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  }
}

module.exports = { AuditLog };
