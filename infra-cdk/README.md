# infra-cdk

The AWS deployment, in CDK (TypeScript). The region is fixed to `sa-east-1`; [ADR 0004](../docs/adr/0004-agentcore-runtime-sa-east-1.md) explains why, and why the default model is a native in-region model ID rather than a cross-region inference profile.

![AWS deployment](../docs/assets/aws-deployment.png)

## Stacks

| Stack | File | What it creates |
|-------|------|-----------------|
| `BankingAgentNetwork` | `lib/network-stack.ts` | VPC with public and private (egress) subnets in `sa-east-1a/b/c`, one NAT gateway, ECR and CloudWatch Logs interface endpoints, S3 gateway endpoint |
| `BankingAgentData` | `lib/data-stack.ts` | RDS Postgres 16 (`db.t4g.micro`, private, database `trail`, generated secret) and the `banking-agent` ECR repository |
| `BankingAgentIdentity` | `lib/identity-stack.ts` | Cognito user pool (no self sign-up) and a public SPA client (no secret, `USER_PASSWORD_AUTH` + SRP) |
| `BankingAgentRuntime` | `lib/agent-stack.ts` | AgentCore Runtime in VPC mode, Cognito JWT authorizer, `Authorization` header allowlisted, app environment, ADOT tracing, generated HMAC confirmation key, Bedrock invoke and RDS access |
| `BankingAgentOps` | `lib/ops-stack.ts` | `t4g.nano` Amazon Linux 2023 arm64 bastion in a private subnet, reachable only through SSM (no key pair, no port 22), with a 5432 path to RDS |
| `BankingAgentWeb` | `lib/web-stack.ts` | SPA on S3 (private bucket + OAC) behind CloudFront; `/api/invocations` is rewritten by a CloudFront Function to the AgentCore data plane |

**Demo baseline.** RDS, Cognito, S3 and ECR use `removalPolicy: DESTROY` and RDS has `deletionProtection: false` and a single AZ, to keep a demo cheap to create and destroy. Harden all of that before holding real data.

## Deploy

Prerequisites: an AWS account with `cdk bootstrap` done in `sa-east-1`, Docker with `buildx`, and Bedrock model access for the model you pick.

```sh
cd infra-cdk && npm ci

# 1. network, database, registry, identity, bastion
npx cdk deploy BankingAgentNetwork BankingAgentData BankingAgentIdentity BankingAgentOps

# 2. the image (arm64, with the aws extras), from the repository root
make push-aws AWS_ACCOUNT_ID=<account id> IMAGE_TAG=$(git rev-parse --short HEAD)

# 3. the runtime — model defaults to openai.gpt-oss-120b-1:0
npx cdk deploy BankingAgentRuntime -c imageTag=<tag> [-c model=<bedrock model id>]
```

Create a Cognito user, get an access token, and run one turn against the runtime:

```sh
export TRAIL_BEARER_TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id <ClientId> --auth-parameters USERNAME=<user>,PASSWORD=<password> \
  --query AuthenticationResult.AccessToken --output text)
uv run trail invoke --runtime-arn <RuntimeArn> --message "Qual é o meu saldo?"
```

The operator commands (`trail intents`, `trail reconcile`, `trail step-up`) reach RDS through the bastion: see `docs/runbook.md` §9 and `make ops-tunnel`.

### Web UI (`BankingAgentWeb`)

The stack is only instantiated when `-c runtimeArn=…` is passed, so a plain `cdk synth` stays clean for the others. The ARN has to be a literal string at synth time (not a CDK token), because the CloudFront Function rewrites `/api/invocations` to `/runtimes/<url-escaped arn>/invocations?qualifier=DEFAULT`. That keeps the UI same-origin, exactly as nginx does under compose and the Vite proxy does in development.

```sh
npx cdk deploy BankingAgentWeb -c runtimeArn=<RuntimeArn>
make ui-aws COGNITO_CLIENT_ID=<ClientId> WEB_BUCKET=<WebBucket> DISTRIBUTION_ID=<DistributionId>
```

**Not validated end to end:** the CloudFront → AgentCore data plane path with a bearer token. `trail invoke` is the verified way to talk to the deployed runtime.

## Idle cost

`cost/idle_cost.py` prices the stack with zero traffic from the public AWS Price List (no credentials needed):

```sh
python3 cost/idle_cost.py                     # the full stack
python3 cost/idle_cost.py --scenario network  # just the VPC
```

At the September 2026 Price List, the full stack idles at about US$239/month. The three interface endpoints across three AZs are the largest line (~US$138), ahead of the NAT gateway (~US$68).
