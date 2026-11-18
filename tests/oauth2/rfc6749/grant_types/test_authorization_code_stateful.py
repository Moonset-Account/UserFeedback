# -*- coding: utf-8 -*-
"""Stateful combination tests for authorization code token exchange.

These tests replace the always-True ``MagicMock`` validator double used in
``test_authorization_code.py`` with a *stateful* ``RequestValidator`` double
that keeps real per-request state (registered clients, issued authorization
codes, PKCE bindings, saved tokens) and decides every callback from that
state.  Each token-exchange attempt is recorded as an ordered list of
validator callbacks (the observable *stages*), so a failing sequence can be
pinned to the last legitimate stage reached and the first extraneous side
effect (``save_token`` / ``invalidate_authorization_code``) that must never
happen on a rejected exchange.

Covered combinations (driven by tools/local_verify.py as C01-C04):

* C01: client authentication, redirect_uri confirmation, code ownership and
  PKCE plain/S256 as an ordered stage model for a single client.
* C02: two clients and multiple codes interleaved; the same verifier string
  bound to different (client, challenge_method) pairs; missing
  challenge/verifier rejections.
* C03: retry after a failed validation and replay after a successful
  exchange; ordering of save_token vs invalidate_authorization_code and
  callbacks that must not fire.
* C04: deterministic (seeded) token generator, replayability of the token
  sequence, and isolation of validator state across requests.

Only synthetic clients, codes, verifiers and tokens are used.
"""
import base64
import hashlib
import itertools
import json

from oauthlib.common import Request
from oauthlib.oauth2.rfc6749.grant_types import AuthorizationCodeGrant
from oauthlib.oauth2.rfc6749.tokens import BearerToken

from tests.unittest import TestCase

# ---------------------------------------------------------------------------
# Synthetic fixture identities (no real credentials)
# ---------------------------------------------------------------------------
ALICE = 'client-alice'          # confidential client
BOB = 'client-bob'              # public client, PKCE required
ALICE_SECRET = 'alice-secret'   # synthetic fixture secret, never logged
ALICE_CB = 'https://alice.example/cb'
BOB_CB = 'https://bob.example/cb'
VERIFIER_SHARED = 'shared-verifier-0123456789abcdef'
VERIFIER_PLAIN = 'alice-plain-verifier'
VERIFIER_BOB = 'bob-only-verifier'

# Observable pipeline stages of validate_token_request + create_token_response,
# in the order the grant type invokes them.
STAGE_ORDER = (
    'client_authentication_required',
    'authenticate_client',
    'authenticate_client_id',
    'validate_grant_type',
    'validate_code',
    'get_code_challenge',
    'get_code_challenge_method',
    'is_pkce_required',
    'get_default_redirect_uri',
    'confirm_redirect_uri',
    'save_token',
    'invalidate_authorization_code',
)
# Callbacks that mutate server state; never legitimate on a failed exchange.
SIDE_EFFECT_STAGES = ('save_token', 'invalidate_authorization_code')

# Expected full callback sequences.
STAGES_SUCCESS_NO_PKCE = [
    'client_authentication_required', 'authenticate_client',
    'validate_grant_type', 'validate_code', 'get_code_challenge',
    'is_pkce_required', 'confirm_redirect_uri',
    'save_token', 'invalidate_authorization_code',
]
STAGES_SUCCESS_PKCE = [
    'client_authentication_required', 'authenticate_client',
    'validate_grant_type', 'validate_code', 'get_code_challenge',
    'get_code_challenge_method', 'confirm_redirect_uri',
    'save_token', 'invalidate_authorization_code',
]
STAGES_SUCCESS_PKCE_PUBLIC = [
    'client_authentication_required', 'authenticate_client_id',
    'validate_grant_type', 'validate_code', 'get_code_challenge',
    'get_code_challenge_method', 'confirm_redirect_uri',
    'save_token', 'invalidate_authorization_code',
]
STAGES_AUTH_FAILED = ['client_authentication_required', 'authenticate_client']
STAGES_CODE_REJECTED = [
    'client_authentication_required', 'authenticate_client',
    'validate_grant_type', 'validate_code',
]
STAGES_CODE_REJECTED_PUBLIC = [
    'client_authentication_required', 'authenticate_client_id',
    'validate_grant_type', 'validate_code',
]


def s256(verifier):
    """BASE64URL-ENCODE(SHA256(verifier)) per RFC 7636."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip('=')


# ---------------------------------------------------------------------------
# Stateful RequestValidator double
# ---------------------------------------------------------------------------
class ClientRecord:
    def __init__(self, client_id, secret, redirect_uris, pkce_required):
        self.client_id = client_id
        self.secret = secret
        self.redirect_uris = tuple(redirect_uris)
        self.pkce_required = pkce_required
        self.grant_types = ('authorization_code',)


class AuthenticatedClient:
    """What authenticate_client* must set on ``request.client``."""

    def __init__(self, client_id):
        self.client_id = client_id


class StatefulRequestValidator:
    """RequestValidator double backed by real, inspectable state.

    Every callback answers from the registered clients / issued codes instead
    of returning canned values, and appends a non-sensitive entry to
    ``self.log`` so tests can observe exactly which stages ran, in which
    order, and with what outcome.  Secrets are never written to the log.
    """

    def __init__(self):
        self.clients = {}
        self.codes = {}    # code -> binding record
        self.tokens = {}   # access_token -> saved token record
        self.log = []      # ordered [{'event': name, 'detail': {...}}]

    # -- fixture helpers (not oauthlib callbacks) --
    def register_client(self, client_id, secret=None, redirect_uris=(),
                        pkce_required=False):
        self.clients[client_id] = ClientRecord(
            client_id, secret, redirect_uris, pkce_required)

    def issue_code(self, client_id, redirect_uri, challenge=None,
                   challenge_method=None, code=None):
        """Simulate the authorization endpoint issuing a code."""
        assert client_id in self.clients
        code = code or 'code-%d' % (len(self.codes) + 1)
        self.codes[code] = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'challenge': challenge,
            'challenge_method': challenge_method,
            'invalidated': False,
        }
        return code

    def callback_count(self, event):
        return sum(1 for e in self.log if e['event'] == event)

    def _record(self, event, **detail):
        self.log.append({'event': event, 'detail': detail})

    # -- oauthlib RequestValidator callbacks used by the grant --
    def client_authentication_required(self, request):
        record = self.clients.get(request.client_id)
        # Unknown clients are treated as confidential so authentication is
        # attempted (and fails) rather than silently skipped.
        required = record.secret is not None if record else True
        self._record('client_authentication_required',
                     client_id=request.client_id, required=required)
        return required

    def authenticate_client(self, request):
        record = self.clients.get(request.client_id)
        ok = (record is not None and record.secret is not None
              and request.client_secret == record.secret)
        self._record('authenticate_client',
                     client_id=request.client_id, ok=ok)
        if ok:
            request.client = AuthenticatedClient(record.client_id)
        return ok

    def authenticate_client_id(self, client_id, request):
        record = self.clients.get(client_id)
        ok = record is not None and record.secret is None
        self._record('authenticate_client_id', client_id=client_id, ok=ok)
        if ok:
            request.client = AuthenticatedClient(client_id)
        return ok

    def validate_grant_type(self, client_id, grant_type, client, request):
        record = self.clients.get(client_id)
        ok = record is not None and grant_type in record.grant_types
        self._record('validate_grant_type', client_id=client_id,
                     grant_type=grant_type, ok=ok)
        return ok

    def validate_code(self, client_id, code, client, request):
        record = self.codes.get(code)
        ok = (record is not None and not record['invalidated']
              and record['client_id'] == client_id)
        self._record('validate_code', client_id=client_id, code=code, ok=ok)
        return ok

    def get_code_challenge(self, code, request):
        record = self.codes.get(code)
        challenge = record['challenge'] if record else None
        self._record('get_code_challenge', code=code,
                     has_challenge=challenge is not None)
        return challenge

    def get_code_challenge_method(self, code, request):
        record = self.codes.get(code)
        method = record['challenge_method'] if record else None
        self._record('get_code_challenge_method', code=code, method=method)
        return method

    def is_pkce_required(self, client_id, request):
        record = self.clients.get(client_id)
        required = bool(record and record.pkce_required)
        self._record('is_pkce_required', client_id=client_id,
                     required=required)
        return required

    def get_default_redirect_uri(self, client_id, request):
        record = self.clients.get(client_id)
        uri = record.redirect_uris[0] if record and record.redirect_uris else None
        self._record('get_default_redirect_uri', client_id=client_id)
        return uri

    def confirm_redirect_uri(self, client_id, code, redirect_uri, client,
                             request):
        record = self.codes.get(code)
        client_record = self.clients.get(client_id)
        ok = (record is not None
              and record['redirect_uri'] == redirect_uri
              and client_record is not None
              and redirect_uri in client_record.redirect_uris)
        self._record('confirm_redirect_uri', client_id=client_id, code=code,
                     ok=ok)
        return ok

    def save_token(self, token, request):
        self._record('save_token', client_id=request.client_id,
                     code=request.code,
                     access_token=token['access_token'])
        self.tokens[token['access_token']] = {
            'client_id': request.client_id,
            'code': request.code,
            'token': dict(token),
        }

    def invalidate_authorization_code(self, client_id, code, request):
        self._record('invalidate_authorization_code', client_id=client_id,
                     code=code)
        record = self.codes.get(code)
        if record is not None:
            record['invalidated'] = True


# ---------------------------------------------------------------------------
# Exchange harness: run one token request and analyse the observed stages
# ---------------------------------------------------------------------------
def deterministic_token_generator(seed):
    """Seeded token generator: stable, unique, non-random token values."""
    counter = itertools.count(1)

    def generate(request):
        return 'tok-%s-%d' % (seed, next(counter))

    return generate


def build_environment(seed):
    validator = StatefulRequestValidator()
    validator.register_client(ALICE, secret=ALICE_SECRET,
                              redirect_uris=(ALICE_CB,), pkce_required=False)
    validator.register_client(BOB, secret=None,
                              redirect_uris=(BOB_CB,), pkce_required=True)
    grant = AuthorizationCodeGrant(request_validator=validator)
    bearer = BearerToken(
        request_validator=validator,
        token_generator=deterministic_token_generator(seed + '-access'),
        refresh_token_generator=deterministic_token_generator(seed + '-refresh'),
        expires_in=1800,
    )
    return validator, grant, bearer


def make_token_request(client_id, code, secret=None, redirect_uri=None,
                       code_verifier=None, scopes=('read',)):
    request = Request('https://as.example/token')
    request.grant_type = 'authorization_code'
    request.client_id = client_id
    request.client_secret = secret
    request.code = code
    request.redirect_uri = redirect_uri
    request.code_verifier = code_verifier
    request.scopes = list(scopes)
    return request


def analyze_entries(entries, outcome):
    """Split an attempt's callback log into observable model facts.

    Returns (stages, last_valid_stage, first_extra_side_effect): on a failed
    exchange no side-effect callback is legitimate, so the first one observed
    is reported as the first extraneous side effect; on success every stage
    in STAGE_ORDER is legitimate.
    """
    stages = [e['event'] for e in entries]
    legit = set(STAGE_ORDER)
    if outcome != 'success':
        legit -= set(SIDE_EFFECT_STAGES)
    last_valid = next((s for s in reversed(stages) if s in legit), None)
    first_extra = next((s for s in stages if s not in legit), None)
    return stages, last_valid, first_extra


def run_exchange(grant, bearer, validator, **params):
    """Run one token exchange and capture the observable state transitions."""
    request = make_token_request(**params)
    start = len(validator.log)
    headers, body, status = grant.create_token_response(request, bearer)
    entries = validator.log[start:]
    payload = json.loads(body) if body else {}
    outcome = 'success' if status == 200 else 'error'
    stages, last_valid, first_extra = analyze_entries(entries, outcome)
    return {
        'outcome': outcome,
        'status': status,
        'error': payload.get('error'),
        'error_description': payload.get('error_description'),
        'stages': stages,
        'last_valid_stage': last_valid,
        'first_extra_side_effect': first_extra,
        'token': payload if outcome == 'success' else None,
        'request': request,
    }


def attempt_digest(result):
    """Compact, secret-free summary of one exchange attempt."""
    return {
        'outcome': result['outcome'],
        'error': result['error'],
        'terminal_stage': result['stages'][-1] if result['stages'] else None,
        'last_valid_stage': result['last_valid_stage'],
        'first_extra_side_effect': result['first_extra_side_effect'],
    }


# ---------------------------------------------------------------------------
# Assertion bookkeeping shared by the verify entry point and unittest
# ---------------------------------------------------------------------------
def _trim(value, limit=160):
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + '...'


class Check:
    def __init__(self):
        self.assertions = 0
        self.failures = []

    def expect(self, condition, label, expected=None, observed=None):
        self.assertions += 1
        if not condition:
            self.failures.append({
                'label': label,
                'expected': _trim(expected),
                'observed': _trim(observed),
            })


# ---------------------------------------------------------------------------
# C01: single-client stage model
# ---------------------------------------------------------------------------
def run_case_c01():
    seed = 'C01'
    chk = Check()
    validator, grant, bearer = build_environment(seed)
    attempts = []

    # 1. Happy path without PKCE: full ordered stage sequence, side effects
    #    exactly once and in order (save_token before invalidate).
    code_a1 = validator.issue_code(ALICE, ALICE_CB)
    r = run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret=ALICE_SECRET, code=code_a1, redirect_uri=ALICE_CB)
    attempts.append(r)
    chk.expect(r['outcome'] == 'success', 'C01.1 plain exchange succeeds',
               'success', r['outcome'])
    chk.expect(r['stages'] == STAGES_SUCCESS_NO_PKCE,
               'C01.1 exact stage sequence', STAGES_SUCCESS_NO_PKCE, r['stages'])
    chk.expect(r['first_extra_side_effect'] is None,
               'C01.1 no extraneous side effect', None,
               r['first_extra_side_effect'])
    chk.expect(r['stages'].index('save_token')
               < r['stages'].index('invalidate_authorization_code'),
               'C01.1 save_token precedes invalidate_authorization_code')
    chk.expect(validator.codes[code_a1]['invalidated'] is True,
               'C01.1 code invalidated after success')
    saved = validator.tokens.get(r['token']['access_token'])
    chk.expect(saved is not None and saved['client_id'] == ALICE,
               'C01.1 token saved for the authenticated client')
    chk.expect(saved is not None and saved['code'] == code_a1,
               'C01.1 token saved against the redeemed code')

    # 2. Client authentication failure stops at authenticate_client.
    code_a2 = validator.issue_code(ALICE, ALICE_CB)
    r = run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret='wrong-secret', code=code_a2,
                     redirect_uri=ALICE_CB)
    attempts.append(r)
    chk.expect(r['error'] == 'invalid_client',
               'C01.2 wrong secret rejected as invalid_client',
               'invalid_client', r['error'])
    chk.expect(r['stages'] == STAGES_AUTH_FAILED,
               'C01.2 stops at authenticate_client',
               STAGES_AUTH_FAILED, r['stages'])
    chk.expect(r['last_valid_stage'] == 'authenticate_client',
               'C01.2 last legitimate stage is authenticate_client')
    chk.expect('validate_code' not in r['stages'],
               'C01.2 code validation never runs after auth failure')
    chk.expect(r['first_extra_side_effect'] is None,
               'C01.2 no side effect after auth failure')
    chk.expect(validator.codes[code_a2]['invalidated'] is False,
               'C01.2 failed authentication must not consume the code')

    # 3. Code ownership: alice presents a code issued to bob.
    code_b1 = validator.issue_code(BOB, BOB_CB,
                                   challenge=s256(VERIFIER_SHARED),
                                   challenge_method='S256')
    r = run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret=ALICE_SECRET, code=code_b1, redirect_uri=BOB_CB,
                     code_verifier=VERIFIER_SHARED)
    attempts.append(r)
    chk.expect(r['error'] == 'invalid_grant',
               'C01.3 foreign code rejected as invalid_grant',
               'invalid_grant', r['error'])
    chk.expect(r['stages'] == STAGES_CODE_REJECTED,
               'C01.3 stops at validate_code', STAGES_CODE_REJECTED,
               r['stages'])
    chk.expect('get_code_challenge' not in r['stages'],
               'C01.3 PKCE lookup never runs for a foreign code')
    chk.expect(validator.codes[code_b1]['invalidated'] is False,
               'C01.3 foreign code left untouched')

    # 4. redirect_uri mismatch is caught by confirm_redirect_uri.
    code_a3 = validator.issue_code(ALICE, ALICE_CB)
    r = run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret=ALICE_SECRET, code=code_a3,
                     redirect_uri='https://alice.example/other')
    attempts.append(r)
    chk.expect(r['error'] == 'invalid_request'
               and r['error_description'] == 'Mismatching redirect URI.',
               'C01.4 redirect mismatch reported',
               'Mismatching redirect URI.', r['error_description'])
    chk.expect(r['stages'][-1] == 'confirm_redirect_uri',
               'C01.4 stops at confirm_redirect_uri',
               'confirm_redirect_uri', r['stages'][-1])
    chk.expect('save_token' not in r['stages'],
               'C01.4 no token saved on redirect mismatch')
    chk.expect(validator.codes[code_a3]['invalidated'] is False,
               'C01.4 code survives redirect mismatch')

    # 5. PKCE plain: matching verifier redeems the code.
    code_a4 = validator.issue_code(ALICE, ALICE_CB, challenge=VERIFIER_PLAIN,
                                   challenge_method='plain')
    r = run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret=ALICE_SECRET, code=code_a4, redirect_uri=ALICE_CB,
                     code_verifier=VERIFIER_PLAIN)
    attempts.append(r)
    chk.expect(r['outcome'] == 'success',
               'C01.5 plain PKCE exchange succeeds', 'success', r['outcome'])
    chk.expect(r['stages'] == STAGES_SUCCESS_PKCE,
               'C01.5 exact PKCE stage sequence', STAGES_SUCCESS_PKCE,
               r['stages'])

    # 6. PKCE plain: wrong verifier fails at the challenge comparison.
    code_a5 = validator.issue_code(ALICE, ALICE_CB, challenge=VERIFIER_PLAIN,
                                   challenge_method='plain')
    r = run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret=ALICE_SECRET, code=code_a5, redirect_uri=ALICE_CB,
                     code_verifier='not-the-verifier')
    attempts.append(r)
    chk.expect(r['error'] == 'invalid_grant',
               'C01.6 wrong verifier rejected as invalid_grant',
               'invalid_grant', r['error'])
    chk.expect(r['last_valid_stage'] == 'get_code_challenge_method',
               'C01.6 last legitimate stage is the challenge method lookup',
               'get_code_challenge_method', r['last_valid_stage'])
    chk.expect(r['first_extra_side_effect'] is None,
               'C01.6 no side effect on verifier mismatch')
    chk.expect(validator.codes[code_a5]['invalidated'] is False,
               'C01.6 code survives verifier mismatch')

    # 7. PKCE S256: matching verifier redeems the code.
    code_a6 = validator.issue_code(ALICE, ALICE_CB,
                                   challenge=s256(VERIFIER_SHARED),
                                   challenge_method='S256')
    r = run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret=ALICE_SECRET, code=code_a6, redirect_uri=ALICE_CB,
                     code_verifier=VERIFIER_SHARED)
    attempts.append(r)
    chk.expect(r['outcome'] == 'success',
               'C01.7 S256 PKCE exchange succeeds', 'success', r['outcome'])
    chk.expect(r['stages'] == STAGES_SUCCESS_PKCE,
               'C01.7 exact S256 stage sequence', STAGES_SUCCESS_PKCE,
               r['stages'])
    chk.expect(validator.codes[code_a6]['invalidated'] is True,
               'C01.7 S256 code invalidated after success')

    return {
        'seed': seed,
        'assertions': chk.assertions,
        'failures': chk.failures,
        'summary': {
            'clients': [ALICE, BOB],
            'codes_issued': len(validator.codes),
            'tokens_saved': sorted(validator.tokens),
            'attempts': [attempt_digest(a) for a in attempts],
        },
    }


# ---------------------------------------------------------------------------
# C02: interleaved clients, codes and verifier bindings
# ---------------------------------------------------------------------------
def run_case_c02():
    seed = 'C02'
    chk = Check()
    validator, grant, bearer = build_environment(seed)
    attempts = []

    # The same verifier string is bound to two different (client, method)
    # pairs: alice/plain and bob/S256.
    code_a = validator.issue_code(ALICE, ALICE_CB, challenge=VERIFIER_SHARED,
                                  challenge_method='plain')
    code_b = validator.issue_code(BOB, BOB_CB,
                                  challenge=s256(VERIFIER_SHARED),
                                  challenge_method='S256')

    # 1. bob redeems his S256-bound code with the shared verifier.
    r1 = run_exchange(grant, bearer, validator, client_id=BOB, code=code_b,
                      redirect_uri=BOB_CB, code_verifier=VERIFIER_SHARED)
    attempts.append(r1)
    chk.expect(r1['outcome'] == 'success',
               'C02.1 bob redeems S256-bound code', 'success', r1['outcome'])
    chk.expect(r1['stages'] == STAGES_SUCCESS_PKCE_PUBLIC,
               'C02.1 public client stage sequence',
               STAGES_SUCCESS_PKCE_PUBLIC, r1['stages'])

    # 2. alice redeems her plain-bound code with the same verifier string.
    r2 = run_exchange(grant, bearer, validator, client_id=ALICE,
                      secret=ALICE_SECRET, code=code_a, redirect_uri=ALICE_CB,
                      code_verifier=VERIFIER_SHARED)
    attempts.append(r2)
    chk.expect(r2['outcome'] == 'success',
               'C02.2 alice redeems plain-bound code with same verifier',
               'success', r2['outcome'])
    chk.expect(r1['token']['access_token'] != r2['token']['access_token'],
               'C02.2 tokens are not shared across requests')

    # 3. Cross-client: bob presents a fresh code issued to alice.
    code_a2 = validator.issue_code(ALICE, ALICE_CB,
                                   challenge=VERIFIER_SHARED,
                                   challenge_method='plain')
    r3 = run_exchange(grant, bearer, validator, client_id=BOB, code=code_a2,
                      redirect_uri=ALICE_CB, code_verifier=VERIFIER_SHARED)
    attempts.append(r3)
    chk.expect(r3['error'] == 'invalid_grant',
               'C02.3 bob cannot redeem alice\'s code', 'invalid_grant',
               r3['error'])
    chk.expect(r3['stages'] == STAGES_CODE_REJECTED_PUBLIC,
               'C02.3 stops at validate_code for bob',
               STAGES_CODE_REJECTED_PUBLIC, r3['stages'])
    chk.expect(validator.codes[code_a2]['invalidated'] is False,
               'C02.3 alice\'s code not consumed by bob\'s attempt')

    # 4. Cross-client the other way: alice presents bob's fresh code.
    code_b2 = validator.issue_code(BOB, BOB_CB, challenge=VERIFIER_BOB,
                                   challenge_method='plain')
    r4 = run_exchange(grant, bearer, validator, client_id=ALICE,
                      secret=ALICE_SECRET, code=code_b2, redirect_uri=BOB_CB,
                      code_verifier=VERIFIER_BOB)
    attempts.append(r4)
    chk.expect(r4['error'] == 'invalid_grant',
               'C02.4 alice cannot redeem bob\'s code', 'invalid_grant',
               r4['error'])
    chk.expect(r4['stages'] == STAGES_CODE_REJECTED,
               'C02.4 stops at validate_code for alice',
               STAGES_CODE_REJECTED, r4['stages'])

    # 5. Missing verifier while a challenge is bound: contract rejection.
    r5 = run_exchange(grant, bearer, validator, client_id=BOB, code=code_b2,
                      redirect_uri=BOB_CB)
    attempts.append(r5)
    chk.expect(r5['error'] == 'invalid_request'
               and r5['error_description'] == 'Code verifier required.',
               'C02.5 missing verifier rejected per contract',
               'Code verifier required.', r5['error_description'])
    chk.expect(r5['stages'][-1] == 'get_code_challenge',
               'C02.5 stops right after the challenge lookup',
               'get_code_challenge', r5['stages'][-1])
    chk.expect(r5['first_extra_side_effect'] is None,
               'C02.5 no side effect on missing verifier')

    # 6. Retry with the correct verifier: the failed attempt must not have
    #    consumed the code.
    r6 = run_exchange(grant, bearer, validator, client_id=BOB, code=code_b2,
                      redirect_uri=BOB_CB, code_verifier=VERIFIER_BOB)
    attempts.append(r6)
    chk.expect(r6['outcome'] == 'success',
               'C02.6 retry with correct verifier succeeds', 'success',
               r6['outcome'])
    chk.expect(validator.codes[code_b2]['invalidated'] is True,
               'C02.6 code invalidated only by the successful retry')

    # 7. PKCE-required client, code issued without challenge, no verifier.
    code_b3 = validator.issue_code(BOB, BOB_CB)
    r7 = run_exchange(grant, bearer, validator, client_id=BOB, code=code_b3,
                      redirect_uri=BOB_CB)
    attempts.append(r7)
    chk.expect(r7['error'] == 'invalid_request'
               and r7['error_description'] == 'Code verifier required.',
               'C02.7 PKCE-required client without challenge/verifier '
               'rejected per contract', 'Code verifier required.',
               r7['error_description'])
    chk.expect('is_pkce_required' in r7['stages']
               and r7['stages'][-1] == 'is_pkce_required',
               'C02.7 stops at the is_pkce_required check',
               'is_pkce_required', r7['stages'][-1])

    # 8. PKCE-required client, no stored challenge, verifier supplied.
    code_b4 = validator.issue_code(BOB, BOB_CB)
    r8 = run_exchange(grant, bearer, validator, client_id=BOB, code=code_b4,
                      redirect_uri=BOB_CB, code_verifier=VERIFIER_BOB)
    attempts.append(r8)
    chk.expect(r8['error'] == 'invalid_grant'
               and r8['error_description'] == 'Challenge not found',
               'C02.8 verifier without stored challenge is invalid_grant',
               'Challenge not found', r8['error_description'])
    chk.expect(r8['first_extra_side_effect'] is None,
               'C02.8 no side effect without a stored challenge')
    chk.expect(validator.codes[code_b4]['invalidated'] is False,
               'C02.8 code survives the contract rejection')

    return {
        'seed': seed,
        'assertions': chk.assertions,
        'failures': chk.failures,
        'summary': {
            'clients': [ALICE, BOB],
            'shared_verifier_bindings': [
                {'client': ALICE, 'method': 'plain'},
                {'client': BOB, 'method': 'S256'},
            ],
            'codes_issued': len(validator.codes),
            'tokens_saved': sorted(validator.tokens),
            'attempts': [attempt_digest(a) for a in attempts],
        },
    }


# ---------------------------------------------------------------------------
# C03: failure retry and post-success replay
# ---------------------------------------------------------------------------
def run_case_c03():
    seed = 'C03'
    chk = Check()
    validator, grant, bearer = build_environment(seed)
    attempts = []

    code = validator.issue_code(ALICE, ALICE_CB, challenge=VERIFIER_PLAIN,
                                challenge_method='plain')

    # 1. Failing attempt: wrong verifier.
    r1 = run_exchange(grant, bearer, validator, client_id=ALICE,
                      secret=ALICE_SECRET, code=code, redirect_uri=ALICE_CB,
                      code_verifier='wrong-verifier')
    attempts.append(r1)
    chk.expect(r1['error'] == 'invalid_grant',
               'C03.1 wrong verifier rejected', 'invalid_grant', r1['error'])
    chk.expect(r1['first_extra_side_effect'] is None,
               'C03.1 failed attempt produced no side effect')
    chk.expect(validator.codes[code]['invalidated'] is False,
               'C03.1 code still redeemable after failed attempt')
    chk.expect(len(validator.tokens) == 0,
               'C03.1 no token saved by failed attempt')

    # 2. Retry with the correct verifier succeeds; side effects fire exactly
    #    once and in the contracted order.
    r2 = run_exchange(grant, bearer, validator, client_id=ALICE,
                      secret=ALICE_SECRET, code=code, redirect_uri=ALICE_CB,
                      code_verifier=VERIFIER_PLAIN)
    attempts.append(r2)
    chk.expect(r2['outcome'] == 'success',
               'C03.2 retry with correct verifier succeeds', 'success',
               r2['outcome'])
    chk.expect(r2['stages'].index('save_token')
               < r2['stages'].index('invalidate_authorization_code'),
               'C03.2 save_token recorded before invalidate')
    chk.expect(validator.callback_count('save_token') == 1,
               'C03.2 save_token called exactly once so far')
    chk.expect(validator.callback_count('invalidate_authorization_code') == 1,
               'C03.2 invalidate called exactly once so far')
    saved = validator.tokens.get(r2['token']['access_token'])
    chk.expect(saved is not None and saved['code'] == code,
               'C03.2 saved token bound to the redeemed code')

    # 3. Replay of the same code must be denied at validate_code and must not
    #    trigger save_token / invalidate_authorization_code again.
    r3 = run_exchange(grant, bearer, validator, client_id=ALICE,
                      secret=ALICE_SECRET, code=code, redirect_uri=ALICE_CB,
                      code_verifier=VERIFIER_PLAIN)
    attempts.append(r3)
    chk.expect(r3['error'] == 'invalid_grant',
               'C03.3 replayed code denied as invalid_grant', 'invalid_grant',
               r3['error'])
    chk.expect(r3['stages'] == STAGES_CODE_REJECTED,
               'C03.3 replay stops at validate_code', STAGES_CODE_REJECTED,
               r3['stages'])
    chk.expect('confirm_redirect_uri' not in r3['stages'],
               'C03.3 redirect confirmation never runs on replay')
    chk.expect('save_token' not in r3['stages'],
               'C03.3 save_token must not fire on replay')
    chk.expect('invalidate_authorization_code' not in r3['stages'],
               'C03.3 invalidate must not fire on replay')
    chk.expect(validator.callback_count('save_token') == 1,
               'C03.3 still exactly one saved token')
    chk.expect(validator.callback_count('invalidate_authorization_code') == 1,
               'C03.3 still exactly one invalidation')
    chk.expect(len(validator.tokens) == 1,
               'C03.3 replay did not mint a second token')

    return {
        'seed': seed,
        'assertions': chk.assertions,
        'failures': chk.failures,
        'summary': {
            'code': code,
            'tokens_saved': sorted(validator.tokens),
            'callback_totals': {
                'save_token': validator.callback_count('save_token'),
                'invalidate_authorization_code':
                    validator.callback_count('invalidate_authorization_code'),
            },
            'attempts': [attempt_digest(a) for a in attempts],
        },
    }


# ---------------------------------------------------------------------------
# C04: deterministic tokens, replayability, cross-request isolation
# ---------------------------------------------------------------------------
def scripted_sequence(seed):
    """Fixed two-client script: success, success, then an auth failure."""
    validator, grant, bearer = build_environment(seed)
    code_a = validator.issue_code(ALICE, ALICE_CB)
    code_b = validator.issue_code(BOB, BOB_CB,
                                  challenge=s256(VERIFIER_SHARED),
                                  challenge_method='S256')
    code_a2 = validator.issue_code(ALICE, ALICE_CB)
    results = [
        run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret=ALICE_SECRET, code=code_a, redirect_uri=ALICE_CB),
        run_exchange(grant, bearer, validator, client_id=BOB, code=code_b,
                     redirect_uri=BOB_CB, code_verifier=VERIFIER_SHARED),
        run_exchange(grant, bearer, validator, client_id=ALICE,
                     secret='bad-secret', code=code_a2,
                     redirect_uri=ALICE_CB),
    ]
    return validator, results, (code_a, code_b, code_a2)


def run_case_c04():
    seed = 'C04SEED'
    chk = Check()

    validator1, results1, codes1 = scripted_sequence(seed)
    validator2, results2, _ = scripted_sequence(seed)

    tokens1 = [r['token']['access_token'] if r['token'] else None
               for r in results1]
    tokens2 = [r['token']['access_token'] if r['token'] else None
               for r in results2]

    # Determinism / replayability of the seeded generator.
    chk.expect(tokens1 == tokens2,
               'C04.1 seeded generator reproduces the exact token sequence',
               tokens2, tokens1)
    chk.expect(results1[0]['outcome'] == 'success'
               and results1[1]['outcome'] == 'success',
               'C04.1 scripted successes observed')
    chk.expect(results1[2]['error'] == 'invalid_client',
               'C04.1 scripted auth failure observed', 'invalid_client',
               results1[2]['error'])

    # No token or state is shared between requests.
    chk.expect(tokens1[0] != tokens1[1],
               'C04.2 distinct tokens per request', 'distinct', tokens1)
    chk.expect(results1[0]['token']['refresh_token']
               != results1[1]['token']['refresh_token'],
               'C04.2 distinct refresh tokens per request')
    saved_a = validator1.tokens.get(tokens1[0])
    saved_b = validator1.tokens.get(tokens1[1])
    chk.expect(saved_a is not None and saved_a['client_id'] == ALICE
               and saved_a['code'] == codes1[0],
               'C04.2 first token bound to alice and her code only')
    chk.expect(saved_b is not None and saved_b['client_id'] == BOB
               and saved_b['code'] == codes1[1],
               'C04.2 second token bound to bob and his code only')
    chk.expect(len(validator1.tokens) == 2,
               'C04.2 failed attempt saved no token', 2,
               len(validator1.tokens))
    chk.expect(validator1.codes[codes1[0]]['invalidated'] is True
               and validator1.codes[codes1[1]]['invalidated'] is True
               and validator1.codes[codes1[2]]['invalidated'] is False,
               'C04.2 invalidation state is per-code, not cross-request')

    # Failure records keep the last legitimate stage and the first
    # extraneous side effect (none, for a correct implementation).
    chk.expect(results1[2]['last_valid_stage'] == 'authenticate_client',
               'C04.3 failure record keeps last legitimate stage',
               'authenticate_client', results1[2]['last_valid_stage'])
    chk.expect(results1[2]['first_extra_side_effect'] is None,
               'C04.3 failure record shows no extraneous side effect')
    chk.expect(results1[0]['last_valid_stage']
               == 'invalidate_authorization_code',
               'C04.3 success record ends at invalidate',
               'invalidate_authorization_code',
               results1[0]['last_valid_stage'])

    # Model self-check: the analyser must flag an injected side effect.
    fake_entries = [{'event': 'validate_code'}, {'event': 'save_token'}]
    _, last_valid, first_extra = analyze_entries(fake_entries, 'error')
    chk.expect(last_valid == 'validate_code',
               'C04.4 analyser keeps last legitimate stage',
               'validate_code', last_valid)
    chk.expect(first_extra == 'save_token',
               'C04.4 analyser flags first extraneous side effect',
               'save_token', first_extra)

    return {
        'seed': seed,
        'assertions': chk.assertions,
        'failures': chk.failures,
        'summary': {
            'token_sequence': tokens1,
            'replayed_sequence_matches': tokens1 == tokens2,
            'tokens_saved': sorted(validator1.tokens),
            'attempts': [attempt_digest(r) for r in results1],
        },
    }


# ---------------------------------------------------------------------------
# unittest entry points (same assertions as tools/local_verify.py)
# ---------------------------------------------------------------------------
class AuthorizationCodeStatefulExchangeTest(TestCase):

    def _run_case(self, case_fn):
        result = case_fn()
        self.assertTrue(result['assertions'] > 0,
                        'case ran zero assertions')
        self.assertEqual(result['failures'], [])

    def test_c01_single_client_stage_model(self):
        self._run_case(run_case_c01)

    def test_c02_interleaved_clients_and_bindings(self):
        self._run_case(run_case_c02)

    def test_c03_retry_and_replay(self):
        self._run_case(run_case_c03)

    def test_c04_deterministic_tokens_and_isolation(self):
        self._run_case(run_case_c04)
