export class TransactionError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'TransactionError';
    this.code = code;
    this.details = details;
  }

  toJSON() {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      details: this.details,
    };
  }
}

export function errorResult(error, extra = {}) {
  const err = error instanceof TransactionError
    ? error
    : new TransactionError(error?.code ?? 'Internal', error?.message ?? String(error), error?.details ?? {});
  return { ok: false, error: err.toJSON(), ...extra };
}
