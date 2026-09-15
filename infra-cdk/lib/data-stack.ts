import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

export interface DataStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
}

/** Postgres the control plane writes to, the image registry, and the secret that joins them. */
export class DataStack extends cdk.Stack {
  readonly db: rds.DatabaseInstance;
  readonly dbSecret: secretsmanager.ISecret;
  readonly dbSecurityGroup: ec2.SecurityGroup;
  readonly repo: ecr.Repository;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    this.dbSecurityGroup = new ec2.SecurityGroup(this, "DbSg", {
      vpc: props.vpc,
      description: "RDS Postgres for the banking control plane",
      allowAllOutbound: false,
    });

    this.db = new rds.DatabaseInstance(this, "Postgres", {
      engine: rds.DatabaseInstanceEngine.postgres({ version: rds.PostgresEngineVersion.VER_16 }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [this.dbSecurityGroup],
      credentials: rds.Credentials.fromGeneratedSecret("trail"),
      databaseName: "trail",
      allocatedStorage: 20,
      multiAz: false, // ponytail: single AZ; the SLO in the ADR decides when this flips
      publiclyAccessible: false,
      deletionProtection: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      backupRetention: cdk.Duration.days(1),
    });
    this.dbSecret = this.db.secret!;

    this.repo = new ecr.Repository(this, "AgentRepo", {
      repositoryName: "banking-agent",
      imageScanOnPush: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
    });

    new cdk.CfnOutput(this, "DbEndpoint", { value: this.db.dbInstanceEndpointAddress });
    new cdk.CfnOutput(this, "DbSecretArn", { value: this.dbSecret.secretArn });
    new cdk.CfnOutput(this, "RepoUri", { value: this.repo.repositoryUri });
  }
}
