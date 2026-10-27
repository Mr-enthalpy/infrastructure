# Broker-verified Portal administrator attestations

Portal administrator actions that change membership state are authorized by the
Broker, not by the Portal database.  A Portal account with `role = admin` is
only a UI role.  Approve, reject, suspend, resume, and revoke requests must also
carry a Broker-issued attestation proving that a fresh password + TOTP check
happened at the Broker immediately before the mutation.

## Why this exists

The Portal service owns and can write `portal.db`.  If the Portal process is
compromised, an attacker can change that database, including its own `role`
field, password hash, and TOTP secret.  The Broker therefore keeps a separate
credential record in its root-owned state database:

- `portal_admin_credentials` stores the administrator username, an Argon2id
  password hash, and an encrypted TOTP secret.
- `portal_admin_attestations` stores only SHA-256 token digests, bound to the
  administrator, Portal user/session, exact action, exact target, expiry, and
  nonce.

The TOTP secret is encrypted with the key in
`server.admin_auth_key_file` (default
`/etc/citespec-resource/broker-admin.key`).  That key is `root:root 0600`; the
Portal service cannot read it.  Broker credentials and their TOTP enrollment are
deliberately separate from the Portal login/reauth TOTP.

## Provisioning

Run `deploy/install_server.sh`; it creates the key with 32 random bytes only
when it is absent, sets `root:root 0600`, and preserves the existing key on
reinstall.  Never truncate or replace an existing key: enrolled TOTP secrets
would become undecryptable and every administrator would have to enroll again.

The Broker credential username must match the Portal account username: the
Portal form submits the authenticated Portal account name to the Broker.  Use a
separate password and a separate TOTP enrollment from the Portal login/reauth
credential.

Then enroll the Broker credential from a root CLI session:

```bash
sudo citespec-resource admin portal-auth enroll --username labadmin
```

The command prompts for the Broker administrator password without echo and
prints the TOTP secret and `otpauth://` URI exactly once.  Store that enrollment
in the administrator's authenticator as a separate entry from the Portal login.
Do not reuse the Portal TOTP secret.

Useful commands:

```bash
sudo citespec-resource admin portal-auth list
sudo citespec-resource admin portal-auth remove --username labadmin
```

`list` never returns password hashes or TOTP secrets.  Removing a credential
makes Portal admin mutations for that Broker account fail closed.  If the
encryption key is lost, existing TOTP secrets cannot be decrypted; enroll the
administrator again to replace the credential and TOTP secret.

## Runtime flow

1. The administrator submits the Portal admin form with password and TOTP.
2. The Portal calls the Broker's `portal_admin_attest` command with the exact
   action and target.
3. The Broker verifies its own credential and TOTP counter, then issues an
   opaque random token valid for 120 seconds (configurable 30–600 seconds).
4. The Portal immediately submits the mutation with that token.
5. The Broker atomically consumes the token, checking administrator, Portal
   session, action, target, expiry, and one-use state before any mutation.
6. The Broker records `portal_admin_attested`, denial events, and the mutation
   with the attested administrator and nonce.  Passwords, TOTP codes, and token
   values are never written to the audit log.

A token used for one action cannot be replayed for another action, another
target, another Portal session, or after expiry.  Concurrent attempts to use the
same TOTP counter are serialized by SQLite; only one succeeds.

## Residual risk

A fully compromised Portal process still receives the Broker password and TOTP
that an administrator types into the Portal form.  It can therefore request an
attestation for a different action or target while the administrator is
authenticating.  The Portal also supplies the Portal user/session identity used
for binding, so the Broker verifies that identity as an assertion from the
Portal, not as independent proof of the human session.  The attestation prevents
token forgery, replay, and reuse; it does not prove that the administrator saw
the exact action and target submitted by the Portal.  Until an out-of-band
confirmation (or a CLI-only mutation path) is implemented, Portal admin
mutations are not a replacement for terminal-based administration, and the
Portal must remain disabled unless the remaining gates in this document and
`docs/acceptance.md` pass.

## Operational gates

Do not start `citespec-resource-portal.service` until:

- the Broker key and administrator enrollment are in place;
- P0-01 negative tests pass;
- the required P1 fixes and live network/TLS gates have passed;
- DNS, VPN, nginx, certificate, and firewall acceptance is complete.

Back up the root-owned configuration directory, which includes this key, and the
Broker state database.  Never publish the key, TOTP enrollment, passwords, host
aliases, private addresses, or administrator identities in a public report.
