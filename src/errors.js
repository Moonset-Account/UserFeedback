'use strict';

/**
 * Base error for every failure raised by the auth challenge layer.
 */
export class AuthClientError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'AuthClientError';
    this.details = details;
  }
}

/** Server returned a 401 challenge that cannot be parsed or is missing an id. */
export class MalformedChallengeError extends AuthClientError {
  constructor(message, details = {}) {
    super(message, details);
    this.name = 'MalformedChallengeError';
  }
}

/** The same challenge id has already been negotiated once in this request chain. */
export class ChallengeAlreadyNegotiatedError extends AuthClientError {
  constructor(challengeId, details = {}) {
    super(`challenge ${challengeId} was already negotiated and must not be negotiated again`, details);
    this.name = 'ChallengeAlreadyNegotiatedError';
    this.challengeId = challengeId;
  }
}

/** Credential lookup returned no credential usable for the requested origin/realm. */
export class NoCredentialAvailableError extends AuthClientError {
  constructor(message, details = {}) {
    super(message, details);
    this.name = 'NoCredentialAvailableError';
  }
}

/** A credential was returned but its origin or realm does not match the request. */
export class CredentialMismatchError extends AuthClientError {
  constructor(message, details = {}) {
    super(message, details);
    this.name = 'CredentialMismatchError';
  }
}

/**
 * Request body bytes were already sent and the caller did not provide both a
 * reopenable content source and a server-acknowledged idempotency key, so the
 * client stops and asks the caller to reconfirm before any resend.
 */
export class ReconfirmRequiredError extends AuthClientError {
  constructor(message, details = {}) {
    super(message, details);
    this.name = 'ReconfirmRequiredError';
    this.reconfirmRequired = true;
  }
}

/** Redirect loop / invalid redirect response. */
export class RedirectError extends AuthClientError {
  constructor(message, details = {}) {
    super(message, details);
    this.name = 'RedirectError';
  }
}
