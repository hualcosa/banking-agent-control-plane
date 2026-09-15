import * as cdk from "aws-cdk-lib";
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface AgentStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  db: rds.DatabaseInstance;
  dbSecret: secretsmanager.ISecret;
  dbSecurityGroup: ec2.ISecurityGroup;
  repo: ecr.IRepository;
  userPool: cognito.IUserPool;
  client: cognito.IUserPoolClient;
  imageTag: string;
  model: string;
}

/** The agent, as a microVM per session, inside the VPC, behind Cognito. */
export class AgentStack extends cdk.Stack {
  readonly runtime: agentcore.Runtime;

  constructor(scope: Construct, id: string, props: AgentStackProps) {
    super(scope, id, props);

    const confirmationSecret = new secretsmanager.Secret(this, "ConfirmationSecret", {
      description: "keys the HMAC digest that seals a confirmed action",
      generateSecretString: { passwordLength: 48, excludePunctuation: true },
    });

    const issuer = `https://cognito-idp.${this.region}.amazonaws.com/${props.userPool.userPoolId}`;

    this.runtime = new agentcore.Runtime(this, "Runtime", {
      runtimeName: "banking_agent",
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(props.repo, props.imageTag),
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingVpc(this, {
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      }),
      authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingCognito(
        props.userPool,
        [props.client],
      ),
      requestHeaderConfiguration: { allowlistedHeaders: ["Authorization"] },
      tracingEnabled: true,
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: cdk.Duration.seconds(120),
        maxLifetime: cdk.Duration.hours(2),
      },
      environmentVariables: {
        TRAIL_IDENTITY_MODE: "jwt",
        TRAIL_JWT_ISSUER: issuer,
        TRAIL_JWT_CLIENT_ID: props.client.userPoolClientId,
        TRAIL_DATABASE_URL: `postgresql://trail@${props.db.dbInstanceEndpointAddress}:5432/trail`,
        TRAIL_DATABASE_SECRET_ARN: props.dbSecret.secretArn,
        TRAIL_CONFIRMATION_SECRET_ARN: confirmationSecret.secretArn,
        TRAIL_CONTROL_PLANE_STORE: "postgres",
        TRAIL_CHECKPOINTER: "postgres",
        TRAIL_LLM_PROVIDER: "bedrock_converse",
        TRAIL_MODEL: props.model,
        TRAIL_LLM_API_KEY: "not-used-with-bedrock",
        TRAIL_OTEL_MODE: "adot",
        TRAIL_SERVICE_NAME: "banking-agent",
        AWS_REGION: this.region,
      },
    });

    this.runtime.connections.allowTo(props.dbSecurityGroup, ec2.Port.tcp(5432), "control plane to Postgres");
    props.dbSecret.grantRead(this.runtime.role);
    confirmationSecret.grantRead(this.runtime.role);
    this.runtime.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
        ],
      }),
    );

    new cdk.CfnOutput(this, "RuntimeArn", { value: this.runtime.agentRuntimeArn });
    new cdk.CfnOutput(this, "RuntimeId", { value: this.runtime.agentRuntimeId });
    new cdk.CfnOutput(this, "LogGroupName", { value: this.runtime.applicationLogGroup.logGroupName });
  }
}
