/**
 * Sign-in before the chat shell loads.
 *
 * Cognito when `VITE_COGNITO_CLIENT_ID` is set at build time; otherwise HMAC
 * with the compose demo defaults. Session stays in memory only — a reload asks
 * again.
 */

import { useState, type FormEvent } from "react";

import { loginCognito, signHmac, type Session } from "../auth";

const COGNITO_CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID as string | undefined;
const COGNITO_REGION =
  (import.meta.env.VITE_COGNITO_REGION as string | undefined) ?? "sa-east-1";
const COGNITO_MODE = Boolean(COGNITO_CLIENT_ID);

export function Login({ onSession }: { onSession: (session: Session) => void }) {
  const [customerId, setCustomerId] = useState("cust_123");
  const [secret, setSecret] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (COGNITO_MODE) {
        const { token, customerId: sub } = await loginCognito(
          COGNITO_REGION,
          COGNITO_CLIENT_ID!,
          username.trim(),
          password,
        );
        onSession({ kind: "bearer", token, customerId: sub });
      } else {
        const header = await signHmac(customerId.trim(), secret);
        onSession({ kind: "hmac", header, customerId: customerId.trim() });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell">
      <div className="shell__main">
        <header className="topbar">
          <h1 className="topbar__title">Banking Agent Control Plane</h1>
        </header>
        <form className="composer login" onSubmit={(event) => void submit(event)}>
          {COGNITO_MODE ? (
            <>
              <label className="sr-only" htmlFor="login-username">
                Username
              </label>
              <input
                id="login-username"
                className="composer__input"
                type="text"
                autoComplete="username"
                placeholder="Username"
                value={username}
                disabled={busy}
                onChange={(event) => setUsername(event.target.value)}
              />
              <label className="sr-only" htmlFor="login-password">
                Password
              </label>
              <input
                id="login-password"
                className="composer__input"
                type="password"
                autoComplete="current-password"
                placeholder="Password"
                value={password}
                disabled={busy}
                onChange={(event) => setPassword(event.target.value)}
              />
            </>
          ) : (
            <>
              <label className="sr-only" htmlFor="login-customer-id">
                Customer id
              </label>
              <input
                id="login-customer-id"
                className="composer__input"
                type="text"
                autoComplete="username"
                placeholder="Customer id"
                value={customerId}
                disabled={busy}
                onChange={(event) => setCustomerId(event.target.value)}
              />
              <label className="sr-only" htmlFor="login-secret">
                Identity secret
              </label>
              <input
                id="login-secret"
                className="composer__input"
                type="password"
                autoComplete="current-password"
                placeholder="Identity secret"
                value={secret}
                disabled={busy}
                onChange={(event) => setSecret(event.target.value)}
              />
            </>
          )}
          <div className="composer__pill">
            <button
              type="submit"
              className="composer__send"
              disabled={
                busy ||
                (COGNITO_MODE
                  ? !username.trim() || !password
                  : !customerId.trim() || !secret)
              }
              aria-label={busy ? "Signing in" : "Sign in"}
            >
              {busy ? (
                <span className="composer__spinner" aria-hidden="true" />
              ) : (
                "Sign in"
              )}
            </button>
          </div>
          {error ? (
            <p className="fatal" role="alert">
              {error}
            </p>
          ) : null}
        </form>
      </div>
    </div>
  );
}
