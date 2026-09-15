/**
 * Who the browser says it is, in the two shapes the service accepts.
 *
 * `hmac` is the compose stack: the same signed header the CLI sends, computed
 * here with WebCrypto from the demo secret the operator types once. `bearer`
 * is production: a Cognito access token, obtained with USER_PASSWORD_AUTH
 * against the user pool the AgentCore authorizer trusts. Neither is persisted:
 * a reload asks again, which for a one-hour token is the honest behaviour.
 */
export type Session =
  | { kind: "hmac"; header: string; customerId: string }
  | { kind: "bearer"; token: string; customerId: string };

const DOMAIN = "trail.identity.v1:";

export async function signHmac(customerId: string, secret: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(DOMAIN + customerId));
  const hex = Array.from(new Uint8Array(mac), (b) => b.toString(16).padStart(2, "0")).join(
    "",
  );
  return `${customerId}:${hex}`;
}

export async function loginCognito(
  region: string,
  clientId: string,
  username: string,
  password: string,
): Promise<{ token: string; customerId: string }> {
  const response = await fetch(`https://cognito-idp.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
    },
    body: JSON.stringify({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: clientId,
      AuthParameters: { USERNAME: username, PASSWORD: password },
    }),
  });
  const body = await response.json();
  if (!response.ok || !body.AuthenticationResult?.AccessToken) {
    throw new Error(body.message ?? `Cognito answered ${response.status}`);
  }
  const token: string = body.AuthenticationResult.AccessToken;
  const payload = token.split(".")[1];
  if (!payload) throw new Error("Cognito token had no payload");
  const claims = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
  return { token, customerId: String(claims.sub) };
}

export function authHeaders(session: Session | null): Record<string, string> {
  if (!session) return {};
  return session.kind === "hmac"
    ? { "X-Trail-Identity": session.header }
    : { Authorization: `Bearer ${session.token}` };
}
