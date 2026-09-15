#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { AgentStack } from "../lib/agent-stack";
import { DataStack } from "../lib/data-stack";
import { IdentityStack } from "../lib/identity-stack";
import { NetworkStack } from "../lib/network-stack";
import { OpsStack } from "../lib/ops-stack";
import { WebStack } from "../lib/web-stack";

const app = new cdk.App();
// Region is fixed: data residency is about sa-east-1 (docs/adr/0004), so the stack
// never floats with whatever AWS_REGION the shell happens to have.
const env = { region: "sa-east-1", account: process.env.CDK_DEFAULT_ACCOUNT };

const network = new NetworkStack(app, "BankingAgentNetwork", { env });
const data = new DataStack(app, "BankingAgentData", { env, vpc: network.vpc });
const identity = new IdentityStack(app, "BankingAgentIdentity", { env });
new AgentStack(app, "BankingAgentRuntime", {
  env,
  vpc: network.vpc,
  db: data.db,
  dbSecret: data.dbSecret,
  dbSecurityGroup: data.dbSecurityGroup,
  repo: data.repo,
  userPool: identity.userPool,
  client: identity.client,
  imageTag: app.node.tryGetContext("imageTag") ?? "latest",
  model: app.node.tryGetContext("model") ?? "openai.gpt-oss-120b-1:0",
});
new OpsStack(app, "BankingAgentOps", { env, vpc: network.vpc, dbSecurityGroup: data.dbSecurityGroup });

const runtimeArn = app.node.tryGetContext("runtimeArn");
if (runtimeArn) {
  new WebStack(app, "BankingAgentWeb", { env, runtimeArn });
}
